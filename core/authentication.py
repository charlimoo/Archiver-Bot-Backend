import secrets

from django.conf import settings
from django.core.signing import BadSignature, SignatureExpired
from rest_framework import authentication, exceptions

from .models import TelegramUser
from .security import read_admin_token, read_user_token


class AdminTokenAuthentication(authentication.BaseAuthentication):
    keyword = "Admin"

    def authenticate(self, request):
        from django.contrib.auth import get_user_model

        header = authentication.get_authorization_header(request).decode().split()
        if not header or header[0] != self.keyword:
            return None
        if len(header) != 2:
            raise exceptions.AuthenticationFailed("Invalid authorization header.")
        try:
            user_id = read_admin_token(header[1])
            user = get_user_model().objects.get(pk=user_id, is_active=True, is_staff=True)
        except SignatureExpired as exc:
            raise exceptions.AuthenticationFailed("Admin session expired.") from exc
        except (BadSignature, KeyError, get_user_model().DoesNotExist) as exc:
            raise exceptions.AuthenticationFailed("Invalid admin session.") from exc
        return user, header[1]


class TelegramPrincipal:
    def __init__(self, telegram_user: TelegramUser):
        self.telegram_user = telegram_user
        self.pk = telegram_user.pk

    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def is_staff(self) -> bool:
        return False


class TelegramTokenAuthentication(authentication.BaseAuthentication):
    keyword = "Bearer"

    def authenticate(self, request):
        header = authentication.get_authorization_header(request).decode().split()
        if not header or header[0] != self.keyword:
            return None
        if len(header) != 2:
            raise exceptions.AuthenticationFailed("Invalid authorization header.")
        try:
            telegram_id = read_user_token(header[1])
            user = TelegramUser.objects.get(pk=telegram_id, is_active=True)
        except SignatureExpired as exc:
            raise exceptions.AuthenticationFailed("Session expired.") from exc
        except (BadSignature, KeyError, TelegramUser.DoesNotExist) as exc:
            raise exceptions.AuthenticationFailed("Invalid session.") from exc
        return TelegramPrincipal(user), header[1]


def require_service_token(request) -> None:
    received = request.headers.get("X-Service-Token", "")
    if not secrets.compare_digest(received, settings.SERVICE_AUTH_SECRET):
        raise exceptions.AuthenticationFailed("Invalid service credentials.")
