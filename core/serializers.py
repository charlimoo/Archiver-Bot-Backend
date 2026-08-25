from rest_framework import serializers

from .models import (
    AccessRequirement,
    ArchivedMessage,
    ArchiveRule,
    AuditEvent,
    AutomationRule,
    BanListEntry,
    Chat,
    ChatMember,
    ConnectedAccount,
    FileRecord,
    GroupSettings,
    Job,
    TelegramUser,
    Topic,
)


class AccessRequirementSerializer(serializers.ModelSerializer):
    class Meta:
        model = AccessRequirement
        fields = ["id", "type", "telegram_id", "username", "label", "active", "weight"]
        read_only_fields = ["id"]


class TelegramUserSerializer(serializers.ModelSerializer):
    display_name = serializers.CharField(read_only=True)

    class Meta:
        model = TelegramUser
        fields = [
            "telegram_id",
            "username",
            "first_name",
            "last_name",
            "language_code",
            "display_name",
            "is_active",
            "archive_chat_id",
            "default_destination",
            "consented_at",
            "created_at",
        ]
        read_only_fields = ["telegram_id", "created_at"]

    def validate_default_destination(self, chat):
        request = self.context.get("request")
        if request and getattr(request.user, "telegram_user", None):
            if chat and chat.owner_id != request.user.telegram_user.pk:
                raise serializers.ValidationError("Chat does not belong to this user.")
        return chat


class ConnectedAccountSerializer(serializers.ModelSerializer):
    class Meta:
        model = ConnectedAccount
        fields = ["phone_hint", "is_connected", "last_synced_at", "updated_at"]
        read_only_fields = fields


class TopicSerializer(serializers.ModelSerializer):
    class Meta:
        model = Topic
        fields = ["id", "chat", "thread_id", "name"]
        read_only_fields = ["id"]

    def validate_chat(self, chat):
        if chat.owner_id != self.context["request"].user.telegram_user.pk:
            raise serializers.ValidationError("Chat does not belong to this user.")
        return chat


class ChatSerializer(serializers.ModelSerializer):
    topics = TopicSerializer(many=True, read_only=True)

    class Meta:
        model = Chat
        fields = [
            "id",
            "telegram_id",
            "type",
            "title",
            "username",
            "is_forum",
            "is_archive",
            "bot_is_admin",
            "topics",
            "updated_at",
        ]
        read_only_fields = ["id", "updated_at"]


class ArchiveRuleSerializer(serializers.ModelSerializer):
    first_message_link = serializers.CharField(write_only=True, required=False, allow_blank=True)
    last_message_link = serializers.CharField(write_only=True, required=False, allow_blank=True)

    class Meta:
        model = ArchiveRule
        fields = [
            "id",
            "name",
            "source_chat",
            "source_topic",
            "destination_chat",
            "destination_topic",
            "enabled",
            "live",
            "content_types",
            "first_message_id",
            "last_message_id",
            "first_message_link",
            "last_message_link",
            "delay_seconds",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate(self, attrs):
        for link_field, id_field in (
            ("first_message_link", "first_message_id"),
            ("last_message_link", "last_message_id"),
        ):
            link = attrs.pop(link_field, "")
            if link:
                message_id = link.rstrip("/").rsplit("/", 1)[-1]
                if not message_id.isdigit():
                    raise serializers.ValidationError(
                        {link_field: "Enter a Telegram message link ending in a message ID."}
                    )
                attrs[id_field] = int(message_id)
        owner = self.context["request"].user.telegram_user
        for field in ("source_chat", "destination_chat"):
            chat = attrs.get(field) or getattr(self.instance, field, None)
            if chat and chat.owner_id != owner.pk:
                raise serializers.ValidationError({field: "Chat does not belong to this user."})
        for field, chat_field in (
            ("source_topic", "source_chat"),
            ("destination_topic", "destination_chat"),
        ):
            topic = attrs.get(field)
            chat = attrs.get(chat_field) or getattr(self.instance, chat_field, None)
            if topic and chat and topic.chat_id != chat.id:
                raise serializers.ValidationError(
                    {field: "Topic does not belong to the selected chat."}
                )
        return attrs


class JobSerializer(serializers.ModelSerializer):
    class Meta:
        model = Job
        fields = [
            "id",
            "type",
            "status",
            "progress",
            "total",
            "payload",
            "error",
            "started_at",
            "finished_at",
            "scheduled_for",
            "created_at",
        ]
        read_only_fields = [
            "id",
            "status",
            "progress",
            "total",
            "error",
            "started_at",
            "finished_at",
            "created_at",
        ]

    def validate_scheduled_for(self, value):
        from django.utils import timezone

        if value and value <= timezone.now():
            raise serializers.ValidationError("Scheduled time must be in the future.")
        return value


class ArchivedMessageSerializer(serializers.ModelSerializer):
    chat_title = serializers.CharField(source="chat.title", read_only=True)
    topic_name = serializers.CharField(source="topic.name", read_only=True)

    class Meta:
        model = ArchivedMessage
        fields = [
            "id",
            "chat",
            "chat_title",
            "topic",
            "topic_name",
            "telegram_message_id",
            "sender_telegram_id",
            "text",
            "content_type",
            "file_name",
            "file_size",
            "message_date",
            "message_link",
        ]


class ChatMemberSerializer(serializers.ModelSerializer):
    display_name = serializers.SerializerMethodField()

    class Meta:
        model = ChatMember
        fields = [
            "id",
            "telegram_user_id",
            "username",
            "first_name",
            "last_name",
            "display_name",
            "is_bot",
            "status",
        ]

    def get_display_name(self, member):
        return (
            " ".join(filter(None, (member.first_name, member.last_name)))
            or member.username
            or str(member.telegram_user_id)
        )


class FileRecordSerializer(serializers.ModelSerializer):
    class Meta:
        model = FileRecord
        fields = [
            "id",
            "file_unique_id",
            "file_size",
            "file_name",
            "status",
            "duplicate_of",
            "created_at",
        ]


class GroupSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = GroupSettings
        fields = [
            "helper_enabled",
            "welcome_enabled",
            "welcome_message",
            "goodbye_enabled",
            "goodbye_message",
            "verification_enabled",
        ]


class BanListEntrySerializer(serializers.ModelSerializer):
    class Meta:
        model = BanListEntry
        fields = [
            "id",
            "group",
            "telegram_user_id",
            "reason",
            "expires_at",
            "created_by",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]

    def validate_group(self, group):
        request = self.context.get("request")
        if request and getattr(request.user, "telegram_user", None):
            if group.owner_id != request.user.telegram_user.pk:
                raise serializers.ValidationError("Group does not belong to this user.")
        return group


class AutomationRuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = AutomationRule
        fields = [
            "id",
            "group",
            "match_type",
            "pattern",
            "action",
            "response",
            "duration_seconds",
            "enabled",
        ]
        read_only_fields = ["id"]

    def validate_group(self, group):
        request = self.context.get("request")
        if request and getattr(request.user, "telegram_user", None):
            if group.owner_id != request.user.telegram_user.pk:
                raise serializers.ValidationError("Group does not belong to this user.")
        return group


class AuditEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = AuditEvent
        fields = [
            "id",
            "actor_telegram_id",
            "target_telegram_id",
            "chat_telegram_id",
            "kind",
            "subject",
            "details",
            "created_at",
        ]
        read_only_fields = fields
