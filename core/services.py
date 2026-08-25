from django.db import transaction

from .models import ArchivedMessage, FileRecord


@transaction.atomic
def register_file(
    *, owner, file_unique_id: str, file_size: int, file_name: str = "", mapping=None
) -> FileRecord:
    canonical = (
        FileRecord.objects.select_for_update()
        .filter(owner=owner, file_unique_id=file_unique_id, file_size=file_size)
        .exclude(status=FileRecord.Status.FAILED)
        .order_by("created_at")
        .first()
    )
    status = FileRecord.Status.DUPLICATE if canonical else FileRecord.Status.SAVED
    return FileRecord.objects.create(
        owner=owner,
        mapping=mapping,
        file_unique_id=file_unique_id,
        file_size=file_size,
        file_name=file_name,
        status=status,
        duplicate_of=canonical,
    )


def index_archived_message(
    *,
    owner,
    chat,
    telegram_message_id: int,
    sender_telegram_id: int | None,
    text: str,
    content_type: str,
    file_name: str = "",
    file_size: int = 0,
    message_date=None,
    topic=None,
) -> ArchivedMessage:
    if chat.username:
        message_link = f"https://t.me/{chat.username}/{telegram_message_id}"
    elif chat.type in {chat.Type.GROUP, chat.Type.SUPERGROUP, chat.Type.CHANNEL}:
        internal_id = str(chat.telegram_id).removeprefix("-100").lstrip("-")
        message_link = f"https://t.me/c/{internal_id}/{telegram_message_id}"
    else:
        message_link = ""
    indexed, _ = ArchivedMessage.objects.update_or_create(
        owner=owner,
        chat=chat,
        telegram_message_id=telegram_message_id,
        defaults={
            "topic": topic,
            "sender_telegram_id": sender_telegram_id,
            "text": text,
            "content_type": content_type,
            "file_name": file_name,
            "file_size": file_size,
            "message_date": message_date,
            "message_link": message_link,
        },
    )
    return indexed
