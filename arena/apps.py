from django.apps import AppConfig


class ArenaConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'arena'

    def ready(self):
        from . import accounts  # noqa: F401  登入時記下登入紀錄（user_logged_in 訊號）
