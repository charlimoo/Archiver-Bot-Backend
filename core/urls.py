from django.urls import path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register("chats", views.ChatViewSet, basename="chat")
router.register("topics", views.TopicViewSet, basename="topic")
router.register("archive-rules", views.ArchiveRuleViewSet, basename="archive-rule")
router.register("jobs", views.JobViewSet, basename="job")
router.register("files", views.FileRecordViewSet, basename="file")
router.register("search", views.ArchivedMessageViewSet, basename="search")
router.register("ban-list", views.BanListViewSet, basename="ban-list")
router.register("automation-rules", views.AutomationRuleViewSet, basename="automation-rule")
router.register(
    "admin/access-requirements",
    views.AdminAccessRequirementViewSet,
    basename="admin-access-requirement",
)

urlpatterns = [
    path("health/", views.health),
    path("auth/telegram/", views.telegram_login),
    path("auth/admin/", views.admin_login),
    path("me/", views.me),
    path("connected-account/", views.connected_account),
    path("groups/<int:chat_id>/settings/", views.group_settings),
    path("audit-events/", views.user_audit_events),
    path("admin/stats/", views.admin_stats),
    path("admin/users/", views.admin_users),
    path("admin/jobs/", views.admin_jobs),
    path("admin/audit/", views.admin_audit),
    path("admin/users/<int:telegram_id>/", views.admin_user_detail),
    path("admin/users/<int:telegram_id>/simulate/", views.admin_simulate_user),
    path("admin/jobs/<int:job_id>/retry/", views.admin_retry_job),
    path("admin/jobs/<int:job_id>/cancel/", views.admin_cancel_job),
    path("service/users/upsert/", views.service_upsert_user),
    path("service/users/status/", views.service_user_status),
    path("service/access-requirements/", views.service_access_requirements),
    path("service/access-requirements/complete/", views.service_complete_requirement),
    path("service/admin-archive-topic/", views.service_admin_archive_topic),
    path("service/chats/upsert/", views.service_upsert_chat),
    path("service/sessions/", views.service_store_session),
    path("service/files/", views.service_register_file),
    path("service/live-rules/", views.service_live_rules),
    path("service/ban-list/", views.service_ban_entry),
    path("service/ban-list/delete/", views.service_ban_entry_delete),
    path("service/group-decision/", views.service_group_decision),
    path("service/audit/", views.service_audit),
] + router.urls
