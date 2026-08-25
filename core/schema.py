from drf_spectacular.extensions import OpenApiAuthenticationExtension


class TelegramTokenAuthenticationScheme(OpenApiAuthenticationExtension):
    target_class = "core.authentication.TelegramTokenAuthentication"
    name = "telegramToken"

    def get_security_definition(self, auto_schema):
        return {"type": "http", "scheme": "bearer", "bearerFormat": "signed Telegram session"}


class AdminTokenAuthenticationScheme(OpenApiAuthenticationExtension):
    target_class = "core.authentication.AdminTokenAuthentication"
    name = "adminToken"

    def get_security_definition(self, auto_schema):
        return {
            "type": "apiKey",
            "in": "header",
            "name": "Authorization",
            "description": "Use the `Admin <token>` authorization scheme.",
        }
