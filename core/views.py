from datetime import timedelta
from random import choices

from django.contrib.auth import authenticate
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action, api_view, authentication_classes, permission_classes
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny, IsAdminUser
from rest_framework.response import Response

from .authentication import require_service_token
from .models import (
    AccessRequirement,
    AdminArchiveTopic,
    ArchivedMessage,
    ArchiveRule,
    AuditEvent,
    AutomationRule,
    BanListEntry,
    Chat,
    ConnectedAccount,
    FileRecord,
    GroupSettings,
    Job,
    RequirementCompletion,
    TelegramUser,
    Topic,
)
from .permissions import IsTelegramUser
from .security import (
    encrypt_session,
    issue_admin_token,
    issue_user_token,
    validate_telegram_init_data,
)
from .serializers import (
    AccessRequirementSerializer,
    AdminJobSerializer,
    ArchivedMessageSerializer,
    ArchiveRuleSerializer,
    AuditEventSerializer,
    AutomationRuleSerializer,
    BanListEntrySerializer,
    ChatMemberSerializer,
    ChatSerializer,
    ConnectedAccountSerializer,
    FileRecordSerializer,
    GroupSettingsSerializer,
    JobSerializer,
    TelegramUserSerializer,
    TopicSerializer,
)
from .services import register_file
from .tasks import cancel_job, process_job

object_schema = extend_schema(
    request=OpenApiTypes.OBJECT,
    responses=OpenApiTypes.OBJECT,
)


@object_schema
@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
def health(request):
    return Response({"status": "ok", "time": timezone.now()})


@object_schema
@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def telegram_login(request):
    try:
        payload = validate_telegram_init_data(request.data.get("init_data", ""))
    except ValueError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    user, _ = TelegramUser.objects.update_or_create(
        telegram_id=payload["id"],
        defaults={
            "username": payload.get("username", ""),
            "first_name": payload.get("first_name", ""),
            "last_name": payload.get("last_name", ""),
            "language_code": payload.get("language_code", ""),
        },
    )
    return Response({"token": issue_user_token(user.pk), "user": TelegramUserSerializer(user).data})


@object_schema
@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def admin_login(request):
    user = authenticate(
        username=request.data.get("username", ""), password=request.data.get("password", "")
    )
    if not user or not user.is_staff:
        return Response(
            {"detail": "Invalid administrator credentials."}, status=status.HTTP_401_UNAUTHORIZED
        )
    return Response({"token": issue_admin_token(user.pk), "username": user.get_username()})


@object_schema
@api_view(["GET", "PATCH"])
@permission_classes([IsTelegramUser])
def me(request):
    user = request.user.telegram_user
    if request.method == "PATCH":
        serializer = TelegramUserSerializer(
            user,
            data=request.data,
            partial=True,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
    return Response(TelegramUserSerializer(user).data)


@object_schema
@api_view(["GET"])
@permission_classes([IsTelegramUser])
def connected_account(request):
    account, _ = ConnectedAccount.objects.get_or_create(user=request.user.telegram_user)
    return Response(ConnectedAccountSerializer(account).data)


class OwnedViewSet(viewsets.ModelViewSet):
    permission_classes = [IsTelegramUser]
    lookup_value_regex = r"\d+"

    @property
    def owner(self):
        return self.request.user.telegram_user


class ChatPagination(PageNumberPagination):
    page_size = 100
    page_size_query_param = "page_size"
    max_page_size = 500


class ChatViewSet(OwnedViewSet):
    serializer_class = ChatSerializer
    pagination_class = ChatPagination

    def get_queryset(self):
        queryset = Chat.objects.filter(owner=self.owner).prefetch_related("topics")
        query = self.request.query_params.get("q")
        if query:
            queryset = queryset.filter(Q(title__icontains=query) | Q(username__icontains=query))
        return queryset

    def perform_create(self, serializer):
        serializer.save(owner=self.owner)

    @action(detail=True, methods=["get"])
    def members(self, request, pk=None):
        chat = self.get_object()
        members = chat.members.order_by("first_name", "last_name", "telegram_user_id")
        return Response(ChatMemberSerializer(members, many=True).data)

    @action(detail=True, methods=["post"], url_path="sync-members")
    def sync_members(self, request, pk=None):
        chat = self.get_object()
        job = Job.objects.create(
            owner=self.owner,
            type=Job.Type.MEMBER_SYNC,
            payload={"chat_id": chat.pk},
        )
        transaction.on_commit(lambda: process_job.delay(job.pk))
        return Response(JobSerializer(job).data, status=status.HTTP_202_ACCEPTED)


class TopicViewSet(OwnedViewSet):
    serializer_class = TopicSerializer

    def get_queryset(self):
        return Topic.objects.filter(chat__owner=self.owner)


class ArchiveRuleViewSet(OwnedViewSet):
    serializer_class = ArchiveRuleSerializer

    def get_queryset(self):
        return ArchiveRule.objects.filter(owner=self.owner).select_related(
            "source_chat", "destination_chat"
        )

    def perform_create(self, serializer):
        serializer.save(owner=self.owner)

    @action(detail=True, methods=["post"])
    def run(self, request, pk=None):
        rule = self.get_object()
        job = Job.objects.create(
            owner=self.owner, type=Job.Type.HISTORY_IMPORT, payload={"rule_id": rule.pk}
        )
        scheduled_for = request.data.get("scheduled_for")
        if scheduled_for:
            serializer = JobSerializer(job, data={"scheduled_for": scheduled_for}, partial=True)
            serializer.is_valid(raise_exception=True)
            job = serializer.save()
        transaction.on_commit(
            lambda: process_job.apply_async(args=[job.pk], eta=job.scheduled_for)
        )
        return Response(JobSerializer(job).data, status=status.HTTP_202_ACCEPTED)


class JobViewSet(OwnedViewSet):
    serializer_class = JobSerializer
    http_method_names = ["get", "post", "delete", "head", "options"]

    def get_queryset(self):
        return Job.objects.filter(owner=self.owner).order_by("-created_at")

    def perform_create(self, serializer):
        job = serializer.save(owner=self.owner)
        transaction.on_commit(
            lambda: process_job.apply_async(args=[job.pk], eta=job.scheduled_for)
        )

    def perform_destroy(self, instance):
        cancel_job(instance)

    @action(detail=False, methods=["post"], url_path="sync-chats")
    def sync_chats(self, request):
        job = Job.objects.create(owner=self.owner, type=Job.Type.CHAT_SYNC)
        transaction.on_commit(lambda: process_job.delay(job.pk))
        return Response(JobSerializer(job).data, status=status.HTTP_202_ACCEPTED)

    @action(detail=False, methods=["post"], url_path="setup-archive")
    def setup_archive(self, request):
        existing = Chat.objects.filter(owner=self.owner, is_archive=True).first()
        if existing:
            return Response(
                {"detail": "Archive group already exists.", "chat_id": existing.pk},
                status=status.HTTP_409_CONFLICT,
            )
        job = Job.objects.create(owner=self.owner, type=Job.Type.ARCHIVE_SETUP)
        transaction.on_commit(lambda: process_job.delay(job.pk))
        return Response(JobSerializer(job).data, status=status.HTTP_202_ACCEPTED)

    @action(detail=False, methods=["post"], url_path="index-history")
    def index_history(self, request):
        job = Job.objects.create(
            owner=self.owner,
            type=Job.Type.HISTORY_INDEX,
            payload={"limit_per_chat": int(request.data.get("limit_per_chat", 0))},
        )
        transaction.on_commit(lambda: process_job.delay(job.pk))
        return Response(JobSerializer(job).data, status=status.HTTP_202_ACCEPTED)


class FileRecordViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    serializer_class = FileRecordSerializer
    permission_classes = [IsTelegramUser]
    lookup_value_regex = r"\d+"

    def get_queryset(self):
        queryset = FileRecord.objects.filter(owner=self.request.user.telegram_user)
        status_filter = self.request.query_params.get("status")
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        return queryset.order_by("-created_at")


class ArchivedMessageViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    serializer_class = ArchivedMessageSerializer
    permission_classes = [IsTelegramUser]
    lookup_value_regex = r"\d+"

    def get_queryset(self):
        queryset = ArchivedMessage.objects.filter(
            owner=self.request.user.telegram_user
        ).select_related("chat", "topic")
        query = self.request.query_params.get("q", "").strip()
        exclude = self.request.query_params.get("exclude", "").strip()
        content_type = self.request.query_params.get("content_type", "").strip()
        min_size = self.request.query_params.get("min_size", "").strip()
        max_size = self.request.query_params.get("max_size", "").strip()
        chat_id = self.request.query_params.get("chat", "").strip()
        if query:
            queryset = queryset.filter(
                Q(text__icontains=query) | Q(file_name__icontains=query)
            )
        if exclude:
            for keyword in filter(None, (word.strip() for word in exclude.split(","))):
                queryset = queryset.exclude(
                    Q(text__icontains=keyword) | Q(file_name__icontains=keyword)
                )
        if content_type:
            queryset = queryset.filter(content_type=content_type)
        if min_size.isdigit():
            queryset = queryset.filter(file_size__gte=int(min_size))
        if max_size.isdigit():
            queryset = queryset.filter(file_size__lte=int(max_size))
        if chat_id.isdigit():
            queryset = queryset.filter(chat_id=int(chat_id))
        return queryset.order_by("-message_date", "-created_at")


class BanListViewSet(OwnedViewSet):
    serializer_class = BanListEntrySerializer

    def get_queryset(self):
        queryset = BanListEntry.objects.filter(group__owner=self.owner)
        group = self.request.query_params.get("group", "")
        return queryset.filter(group_id=int(group)) if group.isdigit() else queryset

    def perform_create(self, serializer):
        entry = serializer.save(created_by=self.owner.pk)
        AuditEvent.objects.create(
            actor_telegram_id=self.owner.pk,
            target_telegram_id=entry.telegram_user_id,
            chat_telegram_id=entry.group.telegram_id,
            kind="ban_list_added",
            subject=entry.reason,
        )

    def perform_destroy(self, instance):
        AuditEvent.objects.create(
            actor_telegram_id=self.owner.pk,
            target_telegram_id=instance.telegram_user_id,
            chat_telegram_id=instance.group.telegram_id,
            kind="ban_list_removed",
        )
        instance.delete()


class AutomationRuleViewSet(OwnedViewSet):
    serializer_class = AutomationRuleSerializer

    def get_queryset(self):
        queryset = AutomationRule.objects.filter(group__owner=self.owner)
        group = self.request.query_params.get("group", "")
        return queryset.filter(group_id=int(group)) if group.isdigit() else queryset

    def perform_create(self, serializer):
        rule = serializer.save()
        AuditEvent.objects.create(
            actor_telegram_id=self.owner.pk,
            chat_telegram_id=rule.group.telegram_id,
            kind="automation_rule_created",
            subject=rule.pattern,
            details={"action": rule.action, "match_type": rule.match_type},
        )


@object_schema
@api_view(["GET", "PATCH"])
@permission_classes([IsTelegramUser])
def group_settings(request, chat_id: int):
    chat = Chat.objects.get(pk=chat_id, owner=request.user.telegram_user)
    settings_object, _ = GroupSettings.objects.get_or_create(chat=chat)
    if request.method == "PATCH":
        serializer = GroupSettingsSerializer(settings_object, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        AuditEvent.objects.create(
            actor_telegram_id=request.user.telegram_user.pk,
            chat_telegram_id=chat.telegram_id,
            kind="group_settings_changed",
            details=request.data,
        )
    return Response(GroupSettingsSerializer(settings_object).data)


@object_schema
@api_view(["GET"])
@permission_classes([IsTelegramUser])
def user_audit_events(request):
    chat_ids = request.user.telegram_user.chats.values_list("telegram_id", flat=True)
    events = AuditEvent.objects.filter(chat_telegram_id__in=chat_ids)[:200]
    return Response(AuditEventSerializer(events, many=True).data)


@object_schema
@api_view(["GET"])
@permission_classes([IsAdminUser])
def admin_stats(request):
    now = timezone.now()
    return Response(
        {
            "users": TelegramUser.objects.count(),
            "active_users_30d": TelegramUser.objects.filter(
                updated_at__gte=now - timedelta(days=30)
            ).count(),
            "connected_accounts": ConnectedAccount.objects.filter(is_connected=True).count(),
            "chats": Chat.objects.count(),
            "archive_rules": ArchiveRule.objects.count(),
            "active_rules": ArchiveRule.objects.filter(enabled=True).count(),
            "indexed_messages": ArchivedMessage.objects.count(),
            "storage_bytes": FileRecord.objects.aggregate(total=Sum("file_size"))["total"] or 0,
            "jobs": dict(Job.objects.values_list("status").annotate(count=Count("id"))),
            "files": dict(FileRecord.objects.values_list("status").annotate(count=Count("id"))),
        }
    )


@object_schema
@api_view(["GET"])
@permission_classes([IsAdminUser])
def admin_users(request):
    users = list(
        TelegramUser.objects.select_related("connected_account").order_by("-created_at")[:500]
    )
    user_ids = [user.pk for user in users]

    def owner_counts(model):
        return dict(
            model.objects.filter(owner_id__in=user_ids)
            .values_list("owner_id")
            .annotate(count=Count("id"))
        )

    chat_counts = owner_counts(Chat)
    rule_counts = owner_counts(ArchiveRule)
    job_counts = owner_counts(Job)
    indexed_message_counts = owner_counts(ArchivedMessage)
    file_counts = owner_counts(FileRecord)
    return Response(
        [
            {
                **TelegramUserSerializer(user).data,
                "chat_count": chat_counts.get(user.pk, 0),
                "rule_count": rule_counts.get(user.pk, 0),
                "job_count": job_counts.get(user.pk, 0),
                "indexed_message_count": indexed_message_counts.get(user.pk, 0),
                "file_count": file_counts.get(user.pk, 0),
                "account_connected": getattr(user, "connected_account", None) is not None
                and user.connected_account.is_connected,
                "last_synced_at": getattr(
                    getattr(user, "connected_account", None), "last_synced_at", None
                ),
            }
            for user in users
        ]
    )


@object_schema
@api_view(["GET"])
@permission_classes([IsAdminUser])
def admin_jobs(request):
    return Response(
        AdminJobSerializer(
            Job.objects.select_related("owner").order_by("-created_at")[:500], many=True
        ).data
    )


@object_schema
@api_view(["GET"])
@permission_classes([IsAdminUser])
def admin_audit(request):
    return Response(AuditEventSerializer(AuditEvent.objects.all()[:500], many=True).data)


class AdminAccessRequirementViewSet(viewsets.ModelViewSet):
    serializer_class = AccessRequirementSerializer
    permission_classes = [IsAdminUser]
    lookup_value_regex = r"\d+"
    queryset = AccessRequirement.objects.all().order_by("-active", "label")


@object_schema
@api_view(["PATCH"])
@permission_classes([IsAdminUser])
def admin_user_detail(request, telegram_id: int):
    user = TelegramUser.objects.get(pk=telegram_id)
    if "is_active" in request.data:
        user.is_active = bool(request.data["is_active"])
        user.save(update_fields=["is_active", "updated_at"])
    return Response(TelegramUserSerializer(user).data)


@object_schema
@api_view(["POST"])
@permission_classes([IsAdminUser])
def admin_delete_user(request, telegram_id: int):
    """Permanently remove a Telegram user and every locally stored account record."""
    if str(request.data.get("confirmation", "")) != str(telegram_id):
        return Response(
            {"detail": "Type the Telegram user ID to confirm permanent deletion."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    user = TelegramUser.objects.get(pk=telegram_id)
    active_jobs = list(
        user.jobs.filter(status__in=[Job.Status.QUEUED, Job.Status.RUNNING])
    )
    deleted = {
        "connected_session": int(
            ConnectedAccount.objects.filter(user=user).exclude(encrypted_session="").exists()
        ),
        "chats": user.chats.count(),
        "archive_rules": user.archive_rules.count(),
        "indexed_messages": user.indexed_messages.count(),
        "files": user.files.count(),
        "jobs": user.jobs.count(),
    }

    for job in active_jobs:
        cancel_job(job)

    with transaction.atomic():
        AuditEvent.objects.filter(
            Q(actor_telegram_id=telegram_id)
            | Q(target_telegram_id=telegram_id)
        ).delete()
        user.delete()

    return Response(
        {
            "deleted": True,
            "telegram_id": telegram_id,
            "cancelled_jobs": len(active_jobs),
            "records": deleted,
        }
    )


@object_schema
@api_view(["POST"])
@permission_classes([IsAdminUser])
def admin_simulate_user(request, telegram_id: int):
    user = TelegramUser.objects.get(pk=telegram_id, is_active=True)
    AuditEvent.objects.create(
        actor_telegram_id=None,
        target_telegram_id=user.pk,
        kind="admin_simulation_started",
        subject=request.user.get_username(),
    )
    return Response({"token": issue_user_token(user.pk)})


@object_schema
@api_view(["POST"])
@permission_classes([IsAdminUser])
def admin_retry_job(request, job_id: int):
    job = Job.objects.get(pk=job_id)
    job.status = Job.Status.QUEUED
    job.error = ""
    job.progress = 0
    job.started_at = None
    job.finished_at = None
    job.save()
    transaction.on_commit(lambda: process_job.delay(job.pk))
    return Response(AdminJobSerializer(job).data, status=status.HTTP_202_ACCEPTED)


@object_schema
@api_view(["POST"])
@permission_classes([IsAdminUser])
def admin_cancel_job(request, job_id: int):
    job = Job.objects.get(pk=job_id)
    cancel_job(job)
    return Response(AdminJobSerializer(job).data)


@object_schema
@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def service_upsert_user(request):
    require_service_token(request)
    telegram_id = int(request.data["telegram_id"])
    fields = {
        key: request.data.get(key, "")
        for key in ("username", "first_name", "last_name", "language_code")
    }
    user, _ = TelegramUser.objects.update_or_create(telegram_id=telegram_id, defaults=fields)
    return Response(TelegramUserSerializer(user).data)


@object_schema
@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
def service_user_status(request):
    require_service_token(request)
    telegram_id = request.query_params.get("telegram_id", "")
    if not telegram_id.lstrip("-").isdigit():
        return Response(
            {"detail": "A valid telegram_id is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        user = TelegramUser.objects.get(pk=int(telegram_id))
    except TelegramUser.DoesNotExist:
        return Response({"detail": "User not found."}, status=status.HTTP_404_NOT_FOUND)
    account = ConnectedAccount.objects.filter(user=user).first()
    jobs = user.jobs.all()
    return Response(
        {
            "connected": bool(account and account.is_connected),
            "last_synced_at": account.last_synced_at if account else None,
            "archive_ready": bool(user.archive_chat_id),
            "chats": user.chats.count(),
            "rules": user.archive_rules.count(),
            "active_rules": user.archive_rules.filter(enabled=True).count(),
            "jobs": jobs.count(),
            "failed_jobs": jobs.filter(status=Job.Status.FAILED).count(),
        }
    )


@object_schema
@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
def service_access_requirements(request):
    require_service_token(request)
    requirements = list(AccessRequirement.objects.filter(active=True))
    channel_requirements = [
        requirement
        for requirement in requirements
        if requirement.type == AccessRequirement.Type.CHANNEL
    ]
    selected = []
    if channel_requirements:
        selected.append(
            choices(
                channel_requirements,
                weights=[requirement.weight for requirement in channel_requirements],
                k=1,
            )[0]
        )
    selected.extend(
        requirement
        for requirement in requirements
        if requirement.type == AccessRequirement.Type.BACKUP_BOT
    )
    user_id = request.query_params.get("telegram_id")
    completed_ids = set()
    if user_id and user_id.lstrip("-").isdigit():
        completed_ids = set(
            RequirementCompletion.objects.filter(user_id=int(user_id)).values_list(
                "requirement_id",
                flat=True,
            )
        )
    data = AccessRequirementSerializer(selected, many=True).data
    for item in data:
        item["completed"] = item["id"] in completed_ids
    return Response(data)


@object_schema
@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def service_complete_requirement(request):
    require_service_token(request)
    user = TelegramUser.objects.get(pk=int(request.data["telegram_id"]))
    requirement = AccessRequirement.objects.get(
        pk=int(request.data["requirement_id"]),
        active=True,
    )
    RequirementCompletion.objects.get_or_create(
        user=user,
        requirement=requirement,
    )
    return Response({"completed": True})


@object_schema
@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def service_admin_archive_topic(request):
    require_service_token(request)
    owner = TelegramUser.objects.get(pk=int(request.data["owner_telegram_id"]))
    source_chat_id = int(request.data["source_chat_telegram_id"])
    topic = AdminArchiveTopic.objects.filter(
        owner=owner,
        source_chat_telegram_id=source_chat_id,
    ).first()
    if topic is None and request.data.get("thread_id"):
        topic = AdminArchiveTopic.objects.create(
            owner=owner,
            source_chat_telegram_id=source_chat_id,
            thread_id=int(request.data["thread_id"]),
            name=request.data.get("name", "")[:255],
        )
    return Response(
        {
            "thread_id": topic.thread_id if topic else None,
            "name": topic.name if topic else "",
        }
    )


@object_schema
@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def service_upsert_chat(request):
    require_service_token(request)
    owner = TelegramUser.objects.get(pk=int(request.data["owner_telegram_id"]))
    chat, _ = Chat.objects.update_or_create(
        owner=owner,
        telegram_id=int(request.data["telegram_id"]),
        defaults={
            "type": request.data.get("type", Chat.Type.GROUP),
            "title": request.data.get("title", ""),
            "username": request.data.get("username", ""),
            "is_bot": bool(request.data.get("is_bot", False)),
            "is_forum": bool(request.data.get("is_forum", False)),
            "bot_is_admin": bool(request.data.get("bot_is_admin", False)),
        },
    )
    return Response(ChatSerializer(chat).data)


@object_schema
@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def service_store_session(request):
    require_service_token(request)
    user = TelegramUser.objects.get(pk=int(request.data["telegram_id"]))
    account, _ = ConnectedAccount.objects.get_or_create(user=user)
    account.encrypted_session = encrypt_session(request.data["session"])
    account.phone_hint = request.data.get("phone_hint", "")[-6:]
    account.is_connected = True
    account.save()
    return Response({"stored": True})


@object_schema
@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def service_register_file(request):
    require_service_token(request)
    user = TelegramUser.objects.get(pk=int(request.data["owner_telegram_id"]))
    record = register_file(
        owner=user,
        file_unique_id=request.data["file_unique_id"],
        file_size=int(request.data.get("file_size", 0)),
        file_name=request.data.get("file_name", ""),
    )
    return Response(FileRecordSerializer(record).data, status=status.HTTP_201_CREATED)


@object_schema
@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
def service_live_rules(request):
    require_service_token(request)
    owner_id = int(request.query_params["owner_telegram_id"])
    source_chat_id = int(request.query_params["source_chat_telegram_id"])
    rules = ArchiveRule.objects.filter(
        owner_id=owner_id,
        source_chat__telegram_id=source_chat_id,
        enabled=True,
        live=True,
    ).select_related("destination_chat", "destination_topic")
    return Response(
        [
            {
                "id": rule.id,
                "destination_chat_id": rule.destination_chat.telegram_id,
                "destination_thread_id": rule.destination_topic.thread_id
                if rule.destination_topic
                else None,
                "content_types": rule.content_types,
                "delay_seconds": rule.delay_seconds,
            }
            for rule in rules
        ]
    )


@object_schema
@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def service_ban_entry(request):
    require_service_token(request)
    owner_id = int(request.data["owner_telegram_id"])
    chat_id = int(request.data["chat_telegram_id"])
    chat = Chat.objects.get(owner_id=owner_id, telegram_id=chat_id)
    entry, _ = BanListEntry.objects.update_or_create(
        group=chat,
        telegram_user_id=int(request.data["telegram_user_id"]),
        defaults={
            "reason": request.data.get("reason", ""),
            "created_by": request.data.get("created_by"),
            "expires_at": request.data.get("expires_at"),
        },
    )
    return Response(BanListEntrySerializer(entry).data)


@object_schema
@api_view(["DELETE"])
@authentication_classes([])
@permission_classes([AllowAny])
def service_ban_entry_delete(request):
    require_service_token(request)
    deleted, _ = BanListEntry.objects.filter(
        group__owner_id=int(request.data["owner_telegram_id"]),
        group__telegram_id=int(request.data["chat_telegram_id"]),
        telegram_user_id=int(request.data["telegram_user_id"]),
    ).delete()
    return Response({"deleted": bool(deleted)})


@object_schema
@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def service_group_decision(request):
    require_service_token(request)
    chat_id = int(request.data["chat_telegram_id"])
    user_id = int(request.data.get("user_telegram_id", 0))
    text = request.data.get("text", "")
    file_unique_id = request.data.get("file_unique_id", "")
    now = timezone.now()
    chats = Chat.objects.filter(telegram_id=chat_id, group_settings__helper_enabled=True)
    banned = (
        BanListEntry.objects.filter(group__in=chats, telegram_user_id=user_id)
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
        .exists()
    )
    matched_rule = None
    for rule in AutomationRule.objects.filter(group__in=chats, enabled=True).order_by("id"):
        if (
            rule.match_type == AutomationRule.MatchType.EXACT
            and text.casefold() == rule.pattern.casefold()
        ) or (
            rule.match_type == AutomationRule.MatchType.CONTAINS
            and rule.pattern.casefold() in text.casefold()
        ) or (
            rule.match_type == AutomationRule.MatchType.FILE_UNIQUE_ID
            and file_unique_id == rule.pattern
        ):
            matched_rule = rule
            break
    settings_object = GroupSettings.objects.filter(chat__in=chats).first()
    return Response(
        {
            "banned": banned,
            "rule": AutomationRuleSerializer(matched_rule).data if matched_rule else None,
            "settings": GroupSettingsSerializer(settings_object).data if settings_object else None,
        }
    )


@object_schema
@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def service_audit(request):
    require_service_token(request)
    serializer = AuditEventSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    event = AuditEvent.objects.create(**request.data)
    return Response(AuditEventSerializer(event).data, status=status.HTTP_201_CREATED)
