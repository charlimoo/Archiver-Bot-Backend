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
from .tasks import cancel_job


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
    actions = ("cancel_selected_jobs",)

    @admin.action(description="Cancel selected queued or running jobs")
    def cancel_selected_jobs(self, request, queryset):
        cancelled = 0
        for job in queryset:
            if job.status in {Job.Status.QUEUED, Job.Status.RUNNING}:
                cancel_job(job)
                cancelled += 1
        self.message_user(request, f"Cancelled {cancelled} job(s).")


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
