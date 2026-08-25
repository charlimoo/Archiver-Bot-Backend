import pytest

from core.models import FileRecord, TelegramUser
from core.services import register_file


@pytest.mark.django_db
def test_duplicate_detection_is_scoped_to_owner():
    first_owner = TelegramUser.objects.create(telegram_id=1)
    second_owner = TelegramUser.objects.create(telegram_id=2)

    canonical = register_file(owner=first_owner, file_unique_id="same", file_size=100)
    duplicate = register_file(owner=first_owner, file_unique_id="same", file_size=100)
    other_users_copy = register_file(owner=second_owner, file_unique_id="same", file_size=100)

    assert canonical.status == FileRecord.Status.SAVED
    assert duplicate.status == FileRecord.Status.DUPLICATE
    assert duplicate.duplicate_of == canonical
    assert other_users_copy.status == FileRecord.Status.SAVED
