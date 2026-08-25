from django.db import models


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class TelegramUser(TimeStampedModel):
    telegram_id = models.BigIntegerField(primary_key=True)
    username = models.CharField(max_length=64, blank=True)
    first_name = models.CharField(max_length=255, blank=True)
    last_name = models.CharField(max_length=255, blank=True)
    language_code = models.CharField(max_length=16, blank=True)
    is_active = models.BooleanField(default=True)
    archive_chat_id = models.BigIntegerField(null=True, blank=True)
    default_destination = models.ForeignKey(
        "Chat",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="default_for_users",
    )
    consented_at = models.DateTimeField(null=True, blank=True)

    @property
    def display_name(self) -> str:
        return (
            " ".join(filter(None, (self.first_name, self.last_name)))
            or self.username
            or str(self.telegram_id)
        )

    def __str__(self) -> str:
        return self.display_name


class ConnectedAccount(TimeStampedModel):
    user = models.OneToOneField(
        TelegramUser, on_delete=models.CASCADE, related_name="connected_account"
    )
    encrypted_session = models.TextField(blank=True)
    phone_hint = models.CharField(max_length=32, blank=True)
    is_connected = models.BooleanField(default=False)
    last_synced_at = models.DateTimeField(null=True, blank=True)


class Chat(TimeStampedModel):
    class Type(models.TextChoices):
        PRIVATE = "private", "Private"
        GROUP = "group", "Group"
        SUPERGROUP = "supergroup", "Supergroup"
        CHANNEL = "channel", "Channel"

    owner = models.ForeignKey(TelegramUser, on_delete=models.CASCADE, related_name="chats")
    telegram_id = models.BigIntegerField()
    type = models.CharField(max_length=16, choices=Type.choices)
    title = models.CharField(max_length=255, blank=True)
    username = models.CharField(max_length=64, blank=True)
    is_forum = models.BooleanField(default=False)
    is_archive = models.BooleanField(default=False)
    bot_is_admin = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["owner", "telegram_id"], name="unique_owner_chat")
        ]
        ordering = ["title", "telegram_id"]


class Topic(TimeStampedModel):
    chat = models.ForeignKey(Chat, on_delete=models.CASCADE, related_name="topics")
    thread_id = models.BigIntegerField()
    name = models.CharField(max_length=255)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["chat", "thread_id"], name="unique_chat_topic")
        ]


class ArchiveRule(TimeStampedModel):
    owner = models.ForeignKey(TelegramUser, on_delete=models.CASCADE, related_name="archive_rules")
    name = models.CharField(max_length=255)
    source_chat = models.ForeignKey(Chat, on_delete=models.CASCADE, related_name="source_rules")
    source_topic = models.ForeignKey(
        Topic, on_delete=models.SET_NULL, null=True, blank=True, related_name="source_rules"
    )
    destination_chat = models.ForeignKey(
        Chat, on_delete=models.CASCADE, related_name="destination_rules"
    )
    destination_topic = models.ForeignKey(
        Topic, on_delete=models.SET_NULL, null=True, blank=True, related_name="destination_rules"
    )
    enabled = models.BooleanField(default=True)
    live = models.BooleanField(default=False)
    content_types = models.JSONField(default=list, blank=True)
    first_message_id = models.BigIntegerField(null=True, blank=True)
    last_message_id = models.BigIntegerField(null=True, blank=True)
    delay_seconds = models.PositiveIntegerField(default=1)

    def clean(self) -> None:
        from django.core.exceptions import ValidationError

        if self.source_chat_id and self.source_chat.owner_id != self.owner_id:
            raise ValidationError("Source chat must belong to the rule owner.")
        if self.destination_chat_id and self.destination_chat.owner_id != self.owner_id:
            raise ValidationError("Destination chat must belong to the rule owner.")


class MessageMapping(TimeStampedModel):
    rule = models.ForeignKey(ArchiveRule, on_delete=models.CASCADE, related_name="message_mappings")
    source_message_id = models.BigIntegerField()
    destination_message_id = models.BigIntegerField()
    original_sender_id = models.BigIntegerField(null=True, blank=True)
    last_source_edit_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["rule", "source_message_id"], name="unique_rule_message"
            )
        ]


class FileRecord(TimeStampedModel):
    class Status(models.TextChoices):
        SAVED = "saved", "Saved"
        DUPLICATE = "duplicate", "Duplicate"
        MOVED = "moved", "Moved"
        FAILED = "failed", "Failed"

    owner = models.ForeignKey(TelegramUser, on_delete=models.CASCADE, related_name="files")
    mapping = models.OneToOneField(
        MessageMapping, on_delete=models.SET_NULL, null=True, blank=True, related_name="file"
    )
    file_unique_id = models.CharField(max_length=255, db_index=True)
    file_size = models.BigIntegerField()
    file_name = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.SAVED)
    duplicate_of = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="duplicates"
    )

    class Meta:
        indexes = [models.Index(fields=["owner", "file_unique_id", "file_size"])]


class Job(TimeStampedModel):
    class Type(models.TextChoices):
        HISTORY_IMPORT = "history_import", "History import"
        DUPLICATE_SCAN = "duplicate_scan", "Duplicate scan"
        LIVE_ARCHIVE = "live_archive", "Live archive"
        CHAT_SYNC = "chat_sync", "Chat sync"
        MEMBER_SYNC = "member_sync", "Member sync"
        ARCHIVE_SETUP = "archive_setup", "Archive setup"
        HISTORY_INDEX = "history_index", "History index"

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    owner = models.ForeignKey(TelegramUser, on_delete=models.CASCADE, related_name="jobs")
    type = models.CharField(max_length=32, choices=Type.choices)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.QUEUED)
    progress = models.PositiveIntegerField(default=0)
    total = models.PositiveIntegerField(default=0)
    payload = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True)
    celery_task_id = models.CharField(max_length=255, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    scheduled_for = models.DateTimeField(null=True, blank=True)


class AccessRequirement(TimeStampedModel):
    class Type(models.TextChoices):
        CHANNEL = "channel", "Required channel"
        BACKUP_BOT = "backup_bot", "Backup bot"

    type = models.CharField(max_length=16, choices=Type.choices)
    telegram_id = models.BigIntegerField(null=True, blank=True)
    username = models.CharField(max_length=64, blank=True)
    label = models.CharField(max_length=255)
    active = models.BooleanField(default=True)
    weight = models.PositiveIntegerField(default=1)


class RequirementCompletion(TimeStampedModel):
    user = models.ForeignKey(
        TelegramUser,
        on_delete=models.CASCADE,
        related_name="requirement_completions",
    )
    requirement = models.ForeignKey(
        AccessRequirement,
        on_delete=models.CASCADE,
        related_name="completions",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "requirement"],
                name="unique_requirement_completion",
            )
        ]


class GroupSettings(TimeStampedModel):
    chat = models.OneToOneField(Chat, on_delete=models.CASCADE, related_name="group_settings")
    helper_enabled = models.BooleanField(default=True)
    welcome_enabled = models.BooleanField(default=False)
    welcome_message = models.TextField(default="Welcome, {name}!")
    goodbye_enabled = models.BooleanField(default=False)
    goodbye_message = models.TextField(default="Goodbye, {name}.")
    verification_enabled = models.BooleanField(default=False)


class BanListEntry(TimeStampedModel):
    group = models.ForeignKey(Chat, on_delete=models.CASCADE, related_name="ban_entries")
    telegram_user_id = models.BigIntegerField()
    reason = models.CharField(max_length=500, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    created_by = models.BigIntegerField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["group", "telegram_user_id"], name="unique_group_ban")
        ]


class AutomationRule(TimeStampedModel):
    class MatchType(models.TextChoices):
        CONTAINS = "contains", "Contains text"
        EXACT = "exact", "Exact text"
        FILE_UNIQUE_ID = "file_unique_id", "Telegram file unique ID"

    class Action(models.TextChoices):
        DELETE = "delete", "Delete"
        KICK = "kick", "Kick"
        MUTE = "mute", "Mute"
        REPLY = "reply", "Reply"

    group = models.ForeignKey(Chat, on_delete=models.CASCADE, related_name="automation_rules")
    match_type = models.CharField(
        max_length=16, choices=MatchType.choices, default=MatchType.CONTAINS
    )
    pattern = models.CharField(max_length=500)
    action = models.CharField(max_length=16, choices=Action.choices)
    response = models.TextField(blank=True)
    duration_seconds = models.PositiveIntegerField(default=0)
    enabled = models.BooleanField(default=True)


class AuditEvent(models.Model):
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    actor_telegram_id = models.BigIntegerField(null=True, blank=True)
    target_telegram_id = models.BigIntegerField(null=True, blank=True)
    chat_telegram_id = models.BigIntegerField(null=True, blank=True)
    kind = models.CharField(max_length=64, db_index=True)
    subject = models.CharField(max_length=255, blank=True)
    details = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]


class ArchivedMessage(TimeStampedModel):
    owner = models.ForeignKey(
        TelegramUser,
        on_delete=models.CASCADE,
        related_name="indexed_messages",
    )
    chat = models.ForeignKey(Chat, on_delete=models.CASCADE, related_name="indexed_messages")
    topic = models.ForeignKey(
        Topic,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="indexed_messages",
    )
    telegram_message_id = models.BigIntegerField()
    sender_telegram_id = models.BigIntegerField(null=True, blank=True)
    text = models.TextField(blank=True)
    content_type = models.CharField(max_length=32)
    file_name = models.CharField(max_length=255, blank=True)
    file_size = models.BigIntegerField(default=0)
    message_date = models.DateTimeField(null=True, blank=True)
    message_link = models.URLField(max_length=500, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["owner", "chat", "telegram_message_id"],
                name="unique_indexed_message",
            )
        ]
        indexes = [
            models.Index(fields=["owner", "file_size"]),
            models.Index(fields=["owner", "content_type"]),
        ]


class ChatMember(TimeStampedModel):
    chat = models.ForeignKey(Chat, on_delete=models.CASCADE, related_name="members")
    telegram_user_id = models.BigIntegerField()
    username = models.CharField(max_length=64, blank=True)
    first_name = models.CharField(max_length=255, blank=True)
    last_name = models.CharField(max_length=255, blank=True)
    is_bot = models.BooleanField(default=False)
    status = models.CharField(max_length=32, default="member")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["chat", "telegram_user_id"],
                name="unique_chat_member",
            )
        ]


class AdminArchiveTopic(TimeStampedModel):
    owner = models.ForeignKey(
        TelegramUser,
        on_delete=models.CASCADE,
        related_name="admin_archive_topics",
    )
    source_chat_telegram_id = models.BigIntegerField()
    thread_id = models.BigIntegerField()
    name = models.CharField(max_length=255)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["owner", "source_chat_telegram_id"],
                name="unique_admin_archive_topic",
            )
        ]
