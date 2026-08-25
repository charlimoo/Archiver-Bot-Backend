import asyncio
from types import SimpleNamespace

import pytest
from django.test import override_settings
from telethon.tl import functions

from core.tasks import _add_bot_as_admin, resolve_input_entity


class FakeClient:
    def __init__(self):
        self.requests = []
        self.resolved_username = None

    async def get_input_entity(self, username):
        self.resolved_username = username
        return "bot-entity"

    async def __call__(self, request):
        self.requests.append(request)


@override_settings(BOT_USERNAME="archiverdupbot")
def test_add_bot_as_admin_invites_and_promotes_bot():
    client = FakeClient()

    asyncio.run(_add_bot_as_admin(client, "archive-channel"))

    assert client.resolved_username == "@archiverdupbot"
    assert len(client.requests) == 2

    invite, promote = client.requests
    assert isinstance(invite, functions.channels.InviteToChannelRequest)
    assert invite.channel == "archive-channel"
    assert invite.users == ["bot-entity"]

    assert isinstance(promote, functions.channels.EditAdminRequest)
    assert promote.channel == "archive-channel"
    assert promote.user_id == "bot-entity"
    assert promote.admin_rights.manage_topics is True
    assert promote.admin_rights.delete_messages is True
    assert promote.rank == "Archiver"


@override_settings(BOT_USERNAME="")
def test_add_bot_as_admin_requires_a_bot_username():
    with pytest.raises(RuntimeError, match="bot username is not configured"):
        asyncio.run(_add_bot_as_admin(FakeClient(), "archive-channel"))


class EntityClient:
    def __init__(self, dialogs):
        self.dialogs = dialogs
        self.lookup_count = 0

    async def get_input_entity(self, telegram_id):
        self.lookup_count += 1
        raise ValueError(f"No cached entity for {telegram_id}")

    async def iter_dialogs(self):
        for dialog in self.dialogs:
            yield dialog


def test_resolve_input_entity_refreshes_dialog_cache():
    client = EntityClient(
        [SimpleNamespace(id=6791003484, input_entity="private-user-with-access-hash")]
    )

    entity = asyncio.run(resolve_input_entity(client, 6791003484))

    assert entity == "private-user-with-access-hash"
    assert client.lookup_count == 1


def test_resolve_input_entity_explains_inaccessible_chat():
    client = EntityClient([])

    with pytest.raises(RuntimeError, match="Sync chats and try again"):
        asyncio.run(resolve_input_entity(client, 6791003484))
