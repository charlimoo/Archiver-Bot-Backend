from django.contrib import admin

from .models import (
    AccessRequirement,
    AdminArchiveTopic,
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
    MessageMapping,
    RequirementCompletion,
    TelegramUser,
    Topic,
)


@admin.register(TelegramUser)
class TelegramUserAdmin(admin.ModelAdmin):
    list_display = ("telegram_id", "display_name", "username", "is_active", "created_at")
    search_fields = ("telegram_id", "username", "first_name", "last_name")
    list_filter = ("is_active",)


@admin.register(Job)
class JobAdmin(admin.ModelAdmin):
    list_display = ("id", "owner", "type", "status", "progress", "total", "created_at")
    list_filter = ("type", "status")
    readonly_fields = ("created_at", "updated_at", "started_at", "finished_at")


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "kind",
        "actor_telegram_id",
        "target_telegram_id",
        "chat_telegram_id",
    )
    list_filter = ("kind",)
    search_fields = ("subject", "actor_telegram_id", "target_telegram_id", "chat_telegram_id")
    readonly_fields = ("created_at",)


admin.site.register(
    [
        ConnectedAccount,
        Chat,
        Topic,
        ArchiveRule,
        MessageMapping,
        FileRecord,
        AccessRequirement,
        GroupSettings,
        BanListEntry,
        AutomationRule,
        AdminArchiveTopic,
        ArchivedMessage,
        ChatMember,
        RequirementCompletion,
    ]
)
