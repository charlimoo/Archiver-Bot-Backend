import asyncio
import logging
import tempfile
from functools import partial
from pathlib import Path

from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from telethon import TelegramClient, events
from telethon.errors import ChatForwardsRestrictedError
from telethon.sessions import StringSession

from core.models import ArchiveRule, Chat, ConnectedAccount, FileRecord, MessageMapping
from core.security import decrypt_session
from core.services import index_archived_message, register_file
from core.tasks import (
    _content_type,
    _create_topic,
    _destination_topic,
    forward_message,
    resolve_input_entity,
)

logger = logging.getLogger(__name__)


def _rule_accepts(rule: ArchiveRule, message) -> bool:
    if rule.source_topic_id:
        top_id = getattr(message.reply_to, "reply_to_top_id", None)
        if top_id != rule.source_topic.thread_id:
            return False
    if not rule.content_types:
        return True
    if message.photo:
        content_type = "pictures"
    elif message.video:
        content_type = "videos"
    elif message.audio:
        content_type = "music"
    elif message.voice:
        content_type = "voice"
    elif message.file:
        content_type = "files"
    else:
        content_type = "messages"
    return content_type in rule.content_types


@sync_to_async
def _rules(owner_id: int, source_chat_id: int) -> list[ArchiveRule]:
    return list(
        ArchiveRule.objects.filter(
            owner_id=owner_id,
            source_chat__telegram_id=source_chat_id,
            enabled=True,
            live=True,
        ).select_related(
            "owner",
            "source_chat",
            "source_topic",
            "destination_chat",
            "destination_topic",
        )
    )


@sync_to_async
def _mapping_exists(rule_id: int, message_id: int) -> bool:
    return MessageMapping.objects.filter(
        rule_id=rule_id,
        source_message_id=message_id,
    ).exists()


@sync_to_async
def _store_mapping(rule: ArchiveRule, source, destination):
    mapping, _ = MessageMapping.objects.update_or_create(
        rule=rule,
        source_message_id=source.id,
        defaults={
            "destination_message_id": destination.id,
            "original_sender_id": source.sender_id,
            "last_source_edit_at": source.edit_date,
        },
    )
    document = getattr(source.media, "document", None)
    record = None
    if document:
        record = register_file(
            owner=rule.owner,
            file_unique_id=f"mtproto:{document.id}",
            file_size=document.size or 0,
            file_name=getattr(source.file, "name", "") or "",
            mapping=mapping,
        )
    return mapping, record


async def _send(client: TelegramClient, rule: ArchiveRule, message):
    destination_topic = await _destination_topic(client, rule)
    source_entity = await resolve_input_entity(client, rule.source_chat.telegram_id)
    destination_entity = await resolve_input_entity(client, rule.destination_chat.telegram_id)
    try:
        return await forward_message(
            client,
            source_chat_id=source_entity,
            destination_chat_id=destination_entity,
            destination_thread_id=(destination_topic.thread_id if destination_topic else None),
            message=message,
        )
    except ChatForwardsRestrictedError:
        attribution = f"Originally sent by {message.sender_id or 'unknown'}\n\n"
        if not message.media:
            return await client.send_message(
                destination_entity,
                attribution + (message.message or ""),
                reply_to=destination_topic.thread_id if destination_topic else None,
            )
        with tempfile.TemporaryDirectory(prefix="archiver-live-") as directory:
            downloaded = await client.download_media(message, file=directory)
            if not downloaded:
                raise RuntimeError(f"Could not download message {message.id}") from None
            return await client.send_file(
                destination_entity,
                Path(downloaded),
                caption=attribution + (message.message or ""),
                reply_to=destination_topic.thread_id if destination_topic else None,
            )


async def _new_message(owner_id: int, client: TelegramClient, event) -> None:
    if not event.chat_id:
        return
    for rule in await _rules(owner_id, event.chat_id):
        if not _rule_accepts(rule, event.message):
            continue
        if await _mapping_exists(rule.id, event.message.id):
            continue
        if rule.delay_seconds:
            await asyncio.sleep(rule.delay_seconds)
        try:
            file_name = getattr(event.message.file, "name", "") or ""
            file_size = getattr(event.message.file, "size", 0) or 0
            await sync_to_async(index_archived_message)(
                owner=rule.owner,
                chat=rule.source_chat,
                topic=rule.source_topic,
                telegram_message_id=event.message.id,
                sender_telegram_id=event.message.sender_id,
                text=event.message.message or "",
                content_type=_content_type(event.message),
                file_name=file_name,
                file_size=file_size,
                message_date=event.message.date,
            )
            sent = await _send(client, rule, event.message)
            _, record = await _store_mapping(rule, event.message, sent)
            if record and record.status == FileRecord.Status.DUPLICATE:
                archive = await sync_to_async(
                    Chat.objects.filter(owner=rule.owner, is_archive=True).first
                )()
                if archive:
                    topic = await _create_topic(
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
                        destination_thread_id=topic.thread_id,
                        message=event.message,
                    )
                    await client.delete_messages(
                        rule.destination_chat.telegram_id,
                        [sent.id],
                    )
                    record.status = FileRecord.Status.MOVED
                    await sync_to_async(record.save)(
                        update_fields=["status", "updated_at"]
                    )
        except Exception:
            logger.exception(
                "Live archive failed for rule %s message %s", rule.id, event.message.id
            )


@sync_to_async
def _edited_mappings(owner_id: int, chat_id: int, message_id: int):
    return list(
        MessageMapping.objects.filter(
            rule__owner_id=owner_id,
            rule__source_chat__telegram_id=chat_id,
            rule__enabled=True,
            rule__live=True,
            source_message_id=message_id,
        ).select_related("rule__destination_chat")
    )


async def _edited_message(owner_id: int, client: TelegramClient, event) -> None:
    if not event.chat_id:
        return
    for mapping in await _edited_mappings(owner_id, event.chat_id, event.message.id):
        destination = mapping.rule.destination_chat.telegram_id
        try:
            if not event.message.media:
                await client.edit_message(
                    destination,
                    mapping.destination_message_id,
                    event.message.message or "",
                )
            else:
                await client.send_message(
                    destination,
                    "The source message was edited. Open the source to review its "
                    "updated media or caption.",
                    reply_to=mapping.destination_message_id,
                )
            mapping.last_source_edit_at = event.message.edit_date
            await sync_to_async(mapping.save)(update_fields=["last_source_edit_at", "updated_at"])
        except Exception:
            logger.exception("Could not synchronize edit for mapping %s", mapping.id)


async def _run() -> None:
    if not settings.TELEGRAM_API_ID or not settings.TELEGRAM_API_HASH:
        raise CommandError("TELEGRAM_API_ID and TELEGRAM_API_HASH are required.")
    accounts = await sync_to_async(list)(
        ConnectedAccount.objects.filter(is_connected=True).select_related("user")
    )
    if not accounts:
        raise CommandError("No connected Telegram accounts are available.")

    clients = []
    for account in accounts:
        client = TelegramClient(
            StringSession(decrypt_session(account.encrypted_session)),
            settings.TELEGRAM_API_ID,
            settings.TELEGRAM_API_HASH,
        )
        client.add_event_handler(
            partial(_new_message, account.user_id, client),
            events.NewMessage,
        )
        client.add_event_handler(
            partial(_edited_message, account.user_id, client),
            events.MessageEdited,
        )
        await client.connect()
        if not await client.is_user_authorized():
            logger.error("Skipping expired Telegram session for user %s", account.user_id)
            await client.disconnect()
            continue
        clients.append((account.pk, account.user_id, client))
        logger.info("Listening for Telegram user %s", account.user_id)
    await asyncio.gather(
        *(client.run_until_disconnected() for _, _, client in clients),
        *(
            _disconnect_removed_account(account_id, user_id, client)
            for account_id, user_id, client in clients
        ),
    )


async def _disconnect_removed_account(account_id: int, user_id: int, client) -> None:
    """Stop an in-memory Telegram client when its encrypted session is deleted."""
    while client.is_connected():
        await asyncio.sleep(5)
        try:
            still_connected = await sync_to_async(
                ConnectedAccount.objects.filter(
                    pk=account_id,
                    user_id=user_id,
                    is_connected=True,
                ).exists
            )()
        except Exception:
            logger.exception("Could not verify Telegram session %s", account_id)
            continue
        if not still_connected:
            logger.info("Disconnecting deleted Telegram session for user %s", user_id)
            await client.disconnect()
            return


class Command(BaseCommand):
    help = "Run live archive listeners for all connected Telegram accounts."

    def handle(self, *args, **options):
        asyncio.run(_run())
