from rest_framework.permissions import BasePermission


class IsTelegramUser(BasePermission):
    def has_permission(self, request, view) -> bool:
        return bool(getattr(request.user, "telegram_user", None))
