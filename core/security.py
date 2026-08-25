import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime
from urllib.parse import parse_qsl

from cryptography.fernet import Fernet
from django.conf import settings
from django.core import signing


def validate_telegram_init_data(init_data: str) -> dict:
    values = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = values.pop("hash", "")
    if not received_hash or not settings.BOT_TOKEN:
        raise ValueError("Telegram authentication is not configured.")

    data_check_string = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret_key = hmac.new(b"WebAppData", settings.BOT_TOKEN.encode(), hashlib.sha256).digest()
    calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated_hash, received_hash):
        raise ValueError("Invalid Telegram signature.")

    auth_date = int(values.get("auth_date", "0"))
    age = datetime.now(UTC).timestamp() - auth_date
    if age < 0 or age > settings.TELEGRAM_AUTH_MAX_AGE_SECONDS:
        raise ValueError("Telegram authentication data has expired.")

    try:
        return json.loads(values["user"])
    except (KeyError, json.JSONDecodeError) as exc:
        raise ValueError("Telegram user data is missing or invalid.") from exc


def issue_user_token(telegram_id: int) -> str:
    return signing.dumps({"telegram_id": telegram_id}, salt="telegram-user")


def read_user_token(token: str) -> int:
    payload = signing.loads(token, salt="telegram-user", max_age=24 * 60 * 60)
    return int(payload["telegram_id"])


def issue_admin_token(user_id: int) -> str:
    return signing.dumps({"user_id": user_id}, salt="admin-user")


def read_admin_token(token: str) -> int:
    payload = signing.loads(token, salt="admin-user", max_age=8 * 60 * 60)
    return int(payload["user_id"])


def _fernet() -> Fernet:
    if settings.SESSION_ENCRYPTION_KEY:
        key = settings.SESSION_ENCRYPTION_KEY.encode()
    else:
        digest = hashlib.sha256(settings.SECRET_KEY.encode()).digest()
        key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def encrypt_session(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt_session(value: str) -> str:
    return _fernet().decrypt(value.encode()).decode()
