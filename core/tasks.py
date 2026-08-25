import asyncio
import tempfile
from pathlib import Path

from asgiref.sync import sync_to_async
from celery import shared_task
from django.conf import settings
from django.utils import timezone
from telethon import TelegramClient
from telethon.errors import ChatForwardsRestrictedError, FloodWaitError
from telethon.helpers import generate_random_long
from telethon.sessions import StringSession
from telethon.tl import functions
from telethon.tl.types import ChatAdminRights
from telethon.utils import get_peer_id

from .models import (
    ArchiveRule,
    Chat,
    ChatMember,
    ConnectedAccount,
    FileRecord,
    Job,
    MessageMapping,
    TelegramUser,
    Topic,
)
from .security import decrypt_session
from .services import index_archived_message, register_file


def _content_type(message) -> str:
    if message.photo:
        return "pictures"
    if message.video:
        return "videos"
    if message.audio:
        return "music"
    if message.voice:
        return "voice"
    if message.file:
        return "files"
    if any(
        entity.__class__.__name__ in {"MessageEntityUrl", "MessageEntityTextUrl"}
        for entity in (message.entities or [])
    ):
        return "links"
    if message.message:
        return "messages"
    return "other"


def _file_metadata(message) -> tuple[str, int]:
    return (
        getattr(message.file, "name", "") or "",
        getattr(message.file, "size", 0) or 0,
    )


async def resolve_input_entity(client: TelegramClient, telegram_id: int):
    """Resolve a peer, refreshing Telethon's in-memory entity cache when needed."""
    try:
        return await client.get_input_entity(telegram_id)
    except ValueError as original_error:
        async for dialog in client.iter_dialogs():
            if dialog.id == telegram_id:
                return dialog.input_entity
        raise RuntimeError(
            f"Telegram chat {telegram_id} is no longer accessible. Sync chats and try again."
        ) from original_error


async def forward_message(
    client: TelegramClient,
    *,
    source_chat_id: int,
    destination_chat_id: int,
    message,
    destination_thread_id: int | None = None,
):
    source = await resolve_input_entity(client, source_chat_id)
    destination = await resolve_input_entity(client, destination_chat_id)
    if not destination_thread_id:
        return await client.forward_messages(
            destination,
            message,
            from_peer=source,
        )
    result = await client(
        functions.messages.ForwardMessagesRequest(
            from_peer=source,
            id=[message.id],
            to_peer=destination,
            random_id=[generate_random_long()],
            top_msg_id=destination_thread_id,
        )
    )
    for update in result.updates:
        sent = getattr(update, "message", None)
        if sent is not None:
            return sent
    raise RuntimeError("Telegram did not return the forwarded message.")


async def _create_topic(client: TelegramClient, chat: Chat, name: str) -> Topic:
    existing = await sync_to_async(Topic.objects.filter(chat=chat, name=name).first)()
    if existing:
        return existing
    result = await client(
        functions.messages.CreateForumTopicRequest(
            peer=await resolve_input_entity(client, chat.telegram_id),
            title=name[:128],
            random_id=generate_random_long(),
        )
    )
    thread_id = next(
        (
            update.message.id
            for update in result.updates
            if getattr(update, "message", None) is not None
        ),
        None,
    )
    if not thread_id:
        raise RuntimeError("Telegram did not return the created topic ID.")
    topic, _ = await sync_to_async(Topic.objects.update_or_create)(
        chat=chat,
        thread_id=thread_id,
        defaults={"name": name[:255]},
    )
    return topic


async def _add_bot_as_admin(client: TelegramClient, channel) -> None:
    if not settings.BOT_USERNAME:
        raise RuntimeError("The Telegram bot username is not configured.")

    bot = await client.get_input_entity(f"@{settings.BOT_USERNAME}")
    await client(
        functions.channels.InviteToChannelRequest(
            channel=channel,
            users=[bot],
        )
    )
    await client(
        functions.channels.EditAdminRequest(
            channel=channel,
            user_id=bot,
            admin_rights=ChatAdminRights(
                change_info=True,
                delete_messages=True,
                ban_users=True,
                invite_users=True,
                pin_messages=True,
                manage_call=True,
                other=True,
                manage_topics=True,
            ),
            rank="Archiver",
        )
    )


async def _destination_topic(client: TelegramClient, rule: ArchiveRule) -> Topic | None:
    if rule.destination_topic:
        return rule.destination_topic
    if not rule.destination_chat.is_archive:
        return None
    topic_name = (
        f"{rule.owner.display_name} - {rule.source_chat.title or rule.source_chat.telegram_id} "
        f"- {rule.source_chat.telegram_id}"
    )
    return await _create_topic(client, rule.destination_chat, topic_name)


async def _client_for(job: Job) -> TelegramClient:
    if not settings.TELEGRAM_API_ID or not settings.TELEGRAM_API_HASH:
        raise RuntimeError("Telegram user-client credentials are not configured.")
    account = await sync_to_async(ConnectedAccount.objects.get)(
        user=job.owner,
        is_connected=True,
    )
    client = TelegramClient(
        StringSession(decrypt_session(account.encrypted_session)),
        settings.TELEGRAM_API_ID,
        settings.TELEGRAM_API_HASH,
    )
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError("The connected Telegram session has expired.")
    return client


async def _sync_chats(job: Job) -> None:
    client = await _client_for(job)
    try:
        dialogs = [dialog async for dialog in client.iter_dialogs()]
        job.total = len(dialogs)
        await sync_to_async(job.save)(update_fields=["total", "updated_at"])
        for index, dialog in enumerate(dialogs, 1):
            entity = dialog.entity
            if dialog.is_user:
                chat_type = Chat.Type.PRIVATE
            elif dialog.is_channel and getattr(entity, "broadcast", False):
                chat_type = Chat.Type.CHANNEL
            elif dialog.is_channel:
                chat_type = Chat.Type.SUPERGROUP
            else:
                chat_type = Chat.Type.GROUP
            chat, _ = await sync_to_async(Chat.objects.update_or_create)(
                owner=job.owner,
                telegram_id=dialog.id,
                defaults={
                    "type": chat_type,
                    "title": dialog.name or "",
                    "username": getattr(entity, "username", "") or "",
                    "is_bot": bool(dialog.is_user and getattr(entity, "bot", False)),
                    "is_forum": bool(getattr(entity, "forum", False)),
                },
            )
            if chat.is_forum:
                offset_date = None
                offset_id = 0
                offset_topic = 0
                while True:
                    forum = await client(
                        functions.messages.GetForumTopicsRequest(
                            peer=entity,
                            offset_date=offset_date,
                            offset_id=offset_id,
                            offset_topic=offset_topic,
                            limit=100,
                        )
                    )
                    for forum_topic in forum.topics:
                        await sync_to_async(Topic.objects.update_or_create)(
                            chat=chat,
                            thread_id=forum_topic.id,
                            defaults={"name": forum_topic.title},
                        )
                    if len(forum.topics) < 100:
                        break
                    last_topic = forum.topics[-1]
                    offset_date = last_topic.date
                    offset_id = last_topic.top_message
                    offset_topic = last_topic.id
            job.progress = index
            await sync_to_async(job.save)(update_fields=["progress", "updated_at"])
    finally:
        await client.disconnect()


async def _setup_archive(job: Job) -> None:
    client = await _client_for(job)
    try:
        result = await client(
            functions.channels.CreateChannelRequest(
                title=settings.BOT_NAME,
                about="Private archive managed by ArchiverBot",
                megagroup=True,
                forum=False,
            )
        )
        channel = result.chats[0]
        await client(
            functions.channels.TogglePreHistoryHiddenRequest(
                channel=channel,
                enabled=True,
            )
        )
        await client(
            functions.channels.ToggleForumRequest(
                channel=channel,
                enabled=True,
                tabs=True,
            )
        )
        await _add_bot_as_admin(client, channel)
        telegram_id = get_peer_id(channel)
        chat, _ = await sync_to_async(Chat.objects.update_or_create)(
            owner=job.owner,
            telegram_id=telegram_id,
            defaults={
                "type": Chat.Type.SUPERGROUP,
                "title": settings.BOT_NAME,
                "is_bot": False,
                "is_forum": True,
                "is_archive": True,
                "bot_is_admin": True,
            },
        )
        await sync_to_async(TelegramUser.objects.filter(pk=job.owner_id).update)(
            archive_chat_id=telegram_id,
            default_destination=chat,
        )
        for topic_name in ("Saved", "Failed", "Moved"):
            await _create_topic(client, chat, topic_name)
        job.total = 1
        job.progress = 1
        await sync_to_async(job.save)(update_fields=["total", "progress", "updated_at"])
    finally:
        await client.disconnect()


async def _sync_members(job: Job) -> None:
    chat = await sync_to_async(Chat.objects.get)(
        pk=job.payload["chat_id"],
        owner=job.owner,
    )
    client = await _client_for(job)
    try:
        entity = await resolve_input_entity(client, chat.telegram_id)
        participants = [
            participant async for participant in client.iter_participants(entity)
        ]
        job.total = len(participants)
        await sync_to_async(job.save)(update_fields=["total", "updated_at"])
        for index, participant in enumerate(participants, 1):
            await sync_to_async(ChatMember.objects.update_or_create)(
                chat=chat,
                telegram_user_id=participant.id,
                defaults={
                    "username": participant.username or "",
                    "first_name": participant.first_name or "",
                    "last_name": participant.last_name or "",
                    "is_bot": bool(participant.bot),
                    "status": "member",
                },
            )
            job.progress = index
            await sync_to_async(job.save)(update_fields=["progress", "updated_at"])
    finally:
        await client.disconnect()


async def _index_history(job: Job) -> None:
    client = await _client_for(job)
    try:
        chats = await sync_to_async(list)(Chat.objects.filter(owner=job.owner))
        limit_per_chat = int(job.payload.get("limit_per_chat", 0)) or None
        processed = 0
        for chat in chats:
            try:
                entity = await resolve_input_entity(client, chat.telegram_id)
            except RuntimeError:
                continue
            async for message in client.iter_messages(
                entity,
                reverse=True,
                limit=limit_per_chat,
            ):
                file_name, file_size = _file_metadata(message)
                topic = None
                top_id = getattr(message.reply_to, "reply_to_top_id", None)
                if top_id:
                    topic = await sync_to_async(
                        Topic.objects.filter(chat=chat, thread_id=top_id).first
                    )()
                await sync_to_async(index_archived_message)(
                    owner=job.owner,
                    chat=chat,
                    topic=topic,
                    telegram_message_id=message.id,
                    sender_telegram_id=message.sender_id,
                    text=message.message or "",
                    content_type=_content_type(message),
                    file_name=file_name,
                    file_size=file_size,
                    message_date=message.date,
                )
                processed += 1
                job.progress = processed
                await sync_to_async(job.save)(update_fields=["progress", "updated_at"])
        job.total = processed
        await sync_to_async(job.save)(update_fields=["total", "updated_at"])
    finally:
        await client.disconnect()


async def _import_history(job: Job) -> None:
    rule = await sync_to_async(
        ArchiveRule.objects.select_related(
            "owner",
            "source_chat",
            "destination_chat",
            "source_topic",
            "destination_topic",
        ).get
    )(pk=job.payload["rule_id"], owner=job.owner)
    client = await _client_for(job)
    try:
        source_entity = await resolve_input_entity(client, rule.source_chat.telegram_id)
        destination_entity = await resolve_input_entity(
            client, rule.destination_chat.telegram_id
        )
        minimum = (rule.first_message_id - 1) if rule.first_message_id else 0
        maximum = (rule.last_message_id + 1) if rule.last_message_id else 0
        messages = [
            message
            async for message in client.iter_messages(
                source_entity,
                min_id=minimum,
                max_id=maximum,
                reverse=True,
                reply_to=rule.source_topic.thread_id if rule.source_topic else None,
            )
        ]
        job.total = len(messages)
        await sync_to_async(job.save)(update_fields=["total", "updated_at"])
        destination_topic = await _destination_topic(client, rule)
        for index, message in enumerate(messages, 1):
            await sync_to_async(job.refresh_from_db)(fields=["status"])
            if job.status == Job.Status.CANCELLED:
                break
            content_type = _content_type(message)
            if rule.content_types and content_type not in rule.content_types:
                job.progress = index
                await sync_to_async(job.save)(update_fields=["progress", "updated_at"])
                continue
            file_name, file_size = _file_metadata(message)
            source_topic = rule.source_topic
            await sync_to_async(index_archived_message)(
                owner=job.owner,
                chat=rule.source_chat,
                topic=source_topic,
                telegram_message_id=message.id,
                sender_telegram_id=message.sender_id,
                text=message.message or "",
                content_type=content_type,
                file_name=file_name,
                file_size=file_size,
                message_date=message.date,
            )
            try:
                sent = await forward_message(
                    client,
                    source_chat_id=source_entity,
                    destination_chat_id=destination_entity,
                    destination_thread_id=(
                        destination_topic.thread_id if destination_topic else None
                    ),
                    message=message,
                )
            except ChatForwardsRestrictedError as exc:
                prefix = f"Originally sent by {message.sender_id or 'unknown'}\n\n"
                if message.media:
                    with tempfile.TemporaryDirectory(prefix="archiver-") as directory:
                        downloaded = await client.download_media(message, file=directory)
                        if not downloaded:
                            raise RuntimeError(f"Could not download message {message.id}") from exc
                        sent = await client.send_file(
                            destination_entity,
                            Path(downloaded),
                            caption=prefix + (message.message or ""),
                            reply_to=(
                                destination_topic.thread_id if destination_topic else None
                            ),
                        )
                else:
                    sent = await client.send_message(
                        destination_entity,
                        prefix + (message.message or ""),
                        reply_to=(destination_topic.thread_id if destination_topic else None),
                    )
            except FloodWaitError as exc:
                await asyncio.sleep(exc.seconds)
                sent = await forward_message(
                    client,
                    source_chat_id=source_entity,
                    destination_chat_id=destination_entity,
                    destination_thread_id=(
                        destination_topic.thread_id if destination_topic else None
                    ),
                    message=message,
                )

            mapping, _ = await sync_to_async(MessageMapping.objects.update_or_create)(
                rule=rule,
                source_message_id=message.id,
                defaults={
                    "destination_message_id": sent.id,
                    "original_sender_id": message.sender_id,
                    "last_source_edit_at": message.edit_date,
                },
            )
            document = getattr(message.media, "document", None)
            if document:
                record = await sync_to_async(register_file)(
                    owner=job.owner,
                    file_unique_id=f"mtproto:{document.id}",
                    file_size=document.size or 0,
                    file_name=file_name,
                    mapping=mapping,
                )
                if record.status == FileRecord.Status.DUPLICATE:
                    archive = await sync_to_async(
                        Chat.objects.filter(
                            owner=job.owner,
                            is_archive=True,
                        ).first
                    )()
                    if archive:
                        duplicate_topic = await _create_topic(
                            client,
                            archive,
                            (
                                f"Duplicates - "
                                f"{rule.source_chat.title or rule.source_chat.telegram_id} - "
                                f"{rule.source_chat.telegram_id}"
                            ),
                        )
                        await forward_message(
                            client,
                            source_chat_id=rule.source_chat.telegram_id,
                            destination_chat_id=archive.telegram_id,
                            destination_thread_id=duplicate_topic.thread_id,
                            message=message,
                        )
                        await client.delete_messages(
                            destination_entity,
                            [sent.id],
                        )
                        record.status = FileRecord.Status.MOVED
                        await sync_to_async(record.save)(
                            update_fields=["status", "updated_at"]
                        )
            job.progress = index
            await sync_to_async(job.save)(update_fields=["progress", "updated_at"])
            if rule.delay_seconds:
                await asyncio.sleep(rule.delay_seconds)
    finally:
        await client.disconnect()


@shared_task(
    bind=True,
    autoretry_for=(OSError,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=5,
)
def process_job(self, job_id: int) -> None:
    job = Job.objects.select_related("owner").get(pk=job_id)
    if job.status == Job.Status.CANCELLED:
        return
    job.status = Job.Status.RUNNING
    job.started_at = timezone.now()
    job.celery_task_id = self.request.id or ""
    job.error = ""
    job.save(update_fields=["status", "started_at", "celery_task_id", "error", "updated_at"])
    try:
        if job.type == Job.Type.CHAT_SYNC:
            asyncio.run(_sync_chats(job))
        elif job.type == Job.Type.HISTORY_IMPORT:
            asyncio.run(_import_history(job))
        elif job.type == Job.Type.MEMBER_SYNC:
            asyncio.run(_sync_members(job))
        elif job.type == Job.Type.ARCHIVE_SETUP:
            asyncio.run(_setup_archive(job))
        elif job.type == Job.Type.HISTORY_INDEX:
            asyncio.run(_index_history(job))
        else:
            raise ValueError(f"Unsupported job type: {job.type}")
        job.refresh_from_db()
        if job.status != Job.Status.CANCELLED:
            job.status = Job.Status.COMPLETED
            job.finished_at = timezone.now()
            job.save(update_fields=["status", "finished_at", "updated_at"])
    except Exception as exc:
        job.status = Job.Status.FAILED
        job.error = str(exc)[:4000]
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "error", "finished_at", "updated_at"])
        raise
