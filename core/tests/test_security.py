import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest
from django.test import override_settings

from core.security import validate_telegram_init_data


def make_init_data(token: str, user: dict, auth_date: int | None = None) -> str:
    values = {
        "auth_date": str(auth_date or int(time.time())),
        "query_id": "test-query",
        "user": json.dumps(user, separators=(",", ":")),
    }
    check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


@override_settings(BOT_TOKEN="test-token")
def test_validates_signed_telegram_payload():
    user = {"id": 42, "first_name": "Ada"}
    assert validate_telegram_init_data(make_init_data("test-token", user)) == user


@override_settings(BOT_TOKEN="test-token")
def test_rejects_tampered_telegram_payload():
    with pytest.raises(ValueError, match="signature"):
        validate_telegram_init_data(make_init_data("wrong-token", {"id": 42}))
