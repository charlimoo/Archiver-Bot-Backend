import pytest
from django.test import override_settings
from rest_framework.test import APIClient

from core.models import (
    AccessRequirement,
    ArchivedMessage,
    AutomationRule,
    Chat,
    GroupSettings,
    TelegramUser,
)
from core.security import issue_admin_token, issue_user_token


@pytest.mark.django_db
def test_user_cannot_see_another_users_chats():
    current = TelegramUser.objects.create(telegram_id=10, first_name="Current")
    other = TelegramUser.objects.create(telegram_id=20, first_name="Other")
    own_chat = Chat.objects.create(
        owner=current, telegram_id=-1001, type=Chat.Type.GROUP, title="Mine"
    )
    Chat.objects.create(owner=other, telegram_id=-1002, type=Chat.Type.GROUP, title="Not mine")
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_user_token(current.pk)}")

    response = client.get("/api/chats/")

    assert response.status_code == 200
    assert [item["id"] for item in response.data["results"]] == [own_chat.pk]


@pytest.mark.django_db
def test_chat_list_supports_large_pages_and_bot_classification():
    owner = TelegramUser.objects.create(telegram_id=12)
    Chat.objects.bulk_create(
        [
            Chat(
                owner=owner,
                telegram_id=index,
                type=Chat.Type.PRIVATE,
                title=f"Chat {index}",
                is_bot=index == 100,
            )
            for index in range(101)
        ]
    )
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_user_token(owner.pk)}")

    response = client.get("/api/chats/?page_size=500")

    assert response.status_code == 200
    assert len(response.data["results"]) == 101
    assert next(item for item in response.data["results"] if item["telegram_id"] == 100)[
        "is_bot"
    ] is True


@pytest.mark.django_db
@override_settings(SERVICE_AUTH_SECRET="service-secret")
def test_service_endpoint_rejects_wrong_token():
    client = APIClient()
    response = client.post(
        "/api/service/users/upsert/",
        {"telegram_id": 10},
        format="json",
        HTTP_X_SERVICE_TOKEN="wrong",
    )
    assert response.status_code == 403


@pytest.mark.django_db
@override_settings(SERVICE_AUTH_SECRET="service-secret")
def test_service_can_upsert_user():
    client = APIClient()
    response = client.post(
        "/api/service/users/upsert/",
        {"telegram_id": 10, "first_name": "Ada"},
        format="json",
        HTTP_X_SERVICE_TOKEN="service-secret",
    )
    assert response.status_code == 200
    assert response.data["display_name"] == "Ada"


@pytest.mark.django_db
@override_settings(SERVICE_AUTH_SECRET="service-secret")
def test_service_can_read_user_setup_status():
    user = TelegramUser.objects.create(telegram_id=11, first_name="Grace")
    Chat.objects.create(owner=user, telegram_id=-11, type=Chat.Type.SUPERGROUP)
    client = APIClient()

    response = client.get(
        "/api/service/users/status/?telegram_id=11",
        HTTP_X_SERVICE_TOKEN="service-secret",
    )

    assert response.status_code == 200
    assert response.data == {
        "connected": False,
        "last_synced_at": None,
        "archive_ready": False,
        "chats": 1,
        "rules": 0,
        "active_rules": 0,
        "jobs": 0,
        "failed_jobs": 0,
    }


@pytest.mark.django_db
def test_staff_can_use_admin_dashboard_api(django_user_model):
    django_user_model.objects.create_user(
        username="operator",
        password="correct-horse",
        is_staff=True,
    )
    client = APIClient()
    login = client.post(
        "/api/auth/admin/",
        {"username": "operator", "password": "correct-horse"},
        format="json",
    )
    assert login.status_code == 200

    client.credentials(HTTP_AUTHORIZATION=f"Admin {login.data['token']}")
    response = client.get("/api/admin/stats/")
    assert response.status_code == 200
    assert response.data["users"] == 0


@pytest.mark.django_db
def test_default_destination_and_search_are_owner_scoped():
    owner = TelegramUser.objects.create(telegram_id=101)
    other = TelegramUser.objects.create(telegram_id=202)
    own_chat = Chat.objects.create(owner=owner, telegram_id=-1, type=Chat.Type.GROUP)
    other_chat = Chat.objects.create(owner=other, telegram_id=-2, type=Chat.Type.GROUP)
    ArchivedMessage.objects.create(
        owner=owner,
        chat=own_chat,
        telegram_message_id=1,
        text="keep quarterly report",
        content_type="files",
        file_name="report.pdf",
        file_size=2048,
    )
    ArchivedMessage.objects.create(
        owner=other,
        chat=other_chat,
        telegram_message_id=2,
        text="keep private report",
        content_type="files",
        file_size=4096,
    )
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_user_token(owner.pk)}")

    rejected = client.patch("/api/me/", {"default_destination": other_chat.pk}, format="json")
    accepted = client.patch("/api/me/", {"default_destination": own_chat.pk}, format="json")
    results = client.get("/api/search/?q=report&exclude=private&min_size=1000")

    assert rejected.status_code == 400
    assert accepted.status_code == 200
    assert accepted.data["default_destination"] == own_chat.pk
    assert [item["telegram_message_id"] for item in results.data["results"]] == [1]


@pytest.mark.django_db
@override_settings(SERVICE_AUTH_SECRET="service-secret")
def test_backup_requirement_completion_and_file_rule_decision():
    owner = TelegramUser.objects.create(telegram_id=303)
    requirement = AccessRequirement.objects.create(
        type=AccessRequirement.Type.BACKUP_BOT,
        username="backup_bot",
        label="Start backup",
    )
    chat = Chat.objects.create(owner=owner, telegram_id=-303, type=Chat.Type.SUPERGROUP)
    GroupSettings.objects.create(chat=chat)
    rule = AutomationRule.objects.create(
        group=chat,
        match_type=AutomationRule.MatchType.FILE_UNIQUE_ID,
        pattern="gif-unique-id",
        action=AutomationRule.Action.MUTE,
    )
    client = APIClient()
    headers = {"HTTP_X_SERVICE_TOKEN": "service-secret"}

    before = client.get(f"/api/service/access-requirements/?telegram_id={owner.pk}", **headers)
    completed = client.post(
        "/api/service/access-requirements/complete/",
        {"telegram_id": owner.pk, "requirement_id": requirement.pk},
        format="json",
        **headers,
    )
    after = client.get(f"/api/service/access-requirements/?telegram_id={owner.pk}", **headers)
    decision = client.post(
        "/api/service/group-decision/",
        {
            "chat_telegram_id": chat.telegram_id,
            "user_telegram_id": 999,
            "file_unique_id": "gif-unique-id",
        },
        format="json",
        **headers,
    )

    assert before.data[0]["completed"] is False
    assert completed.status_code == 200
    assert after.data[0]["completed"] is True
    assert decision.data["rule"]["id"] == rule.pk


@pytest.mark.django_db
def test_admin_can_simulate_an_active_user(django_user_model):
    admin = django_user_model.objects.create_user(username="admin", is_staff=True)
    target = TelegramUser.objects.create(telegram_id=404)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Admin {issue_admin_token(admin.pk)}")

    response = client.post(f"/api/admin/users/{target.pk}/simulate/")
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['token']}")
    me = client.get("/api/me/")

    assert response.status_code == 200
    assert me.data["telegram_id"] == target.pk
