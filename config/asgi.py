"""
ASGI config for config project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/5.2/howto/deployment/asgi/

真人對戰的 WebSocket（2026-11-05）：一般網頁照舊交給 Django；ws/ 交給 arena.realtime，
AuthMiddlewareStack 讓連線知道是哪個帳號，AllowedHostsOriginValidator 擋掉別的網站打開的連線。
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

django_application = get_asgi_application()  # 要先載入 Django，下面才能匯入 arena

from channels.auth import AuthMiddlewareStack  # noqa: E402
from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from channels.security.websocket import AllowedHostsOriginValidator  # noqa: E402

from arena import realtime  # noqa: E402

application = ProtocolTypeRouter({
    'http': django_application,
    'websocket': AllowedHostsOriginValidator(AuthMiddlewareStack(URLRouter(realtime.websocket_urlpatterns))),
})
