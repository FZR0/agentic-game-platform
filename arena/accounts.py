"""帳號：註冊、登入、登入紀錄與線上訪客（使用者要求，2026-11-05；docs/交接.md 4.10）。

- 任何人都可以註冊（使用者決定）；所有頁面與 API 都要登入（settings 的 LoginRequiredMiddleware），
  否則網址傳出去，別人就能用研究用伺服器。
- 註冊、登入不經過 AI 助理：密碼不能交給模型，而且這是安全檢查，不是遊戲的請求。
- 每次登入記一筆 LoginRecord（IP、瀏覽器）；PresenceMiddleware 記每個瀏覽器最後活動的時間，後台看得到目前在線的人。
"""
import ipaddress
import time

from django.contrib.auth import login
from django.contrib.auth.decorators import login_not_required
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.signals import user_logged_in
from django.contrib.auth.views import LoginView
from django.dispatch import receiver
from django.shortcuts import redirect, render
from django.utils import timezone

from .models import LoginRecord, Presence

LOOPBACK = {"127.0.0.1", "::1"}
PRESENCE_EVERY = 30  # 同一個瀏覽器幾秒內不重寫，免得每個請求都寫一次資料庫
_last_write = {}  # {session_key: 上次寫入的 time.monotonic()}；只在這個程序裡，重新啟動後第一個請求會再寫一次


def client_ip(request):
    """使用者的 IP。經過 Cloudflare 通道時，連到網站的是本機的 cloudflared，真正的位址在 CF-Connecting-IP。
    只有連線本身來自本機時才採用這個標頭，否則任何人都能自己填一個假的 IP。"""
    ip = request.META.get("REMOTE_ADDR", "")
    if ip in LOOPBACK:
        ip = request.META.get("HTTP_CF_CONNECTING_IP", "").strip() or ip
    try:
        return str(ipaddress.ip_address(ip))
    except ValueError:
        return None


def _agent(request):
    return request.META.get("HTTP_USER_AGENT", "")[:300]


@receiver(user_logged_in)
def record_login(sender, request, user, **kwargs):
    if request is not None:  # 測試的 force_login 沒有 request
        LoginRecord.objects.create(user=user, ip=client_ip(request), user_agent=_agent(request))


class PresenceMiddleware:
    """記下每個瀏覽器最後一次活動的時間、IP、網址與帳號。沒登入的訪客（例如在登入頁）也記，所以先建立 session。"""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        session = request.session
        if not session.session_key:
            session.save()
        key = session.session_key
        now = time.monotonic()
        if now - _last_write.get(key, -PRESENCE_EVERY) >= PRESENCE_EVERY:
            _last_write[key] = now
            user = request.user if request.user.is_authenticated else None
            Presence.objects.update_or_create(session_key=key, defaults={
                "user": user, "ip": client_ip(request), "path": request.path[:200], "user_agent": _agent(request),
                "last_seen": timezone.now()})
        return response


def _page_context():
    from .operations import _asset_version  # operations 匯入 models；放在這裡避免啟動時循環匯入
    return {"asset_version": _asset_version("arena/style.css")}


class SignInView(LoginView):
    template_name = "registration/login.html"
    redirect_authenticated_user = True

    def get_context_data(self, **kwargs):
        return {**super().get_context_data(**kwargs), **_page_context()}


@login_not_required
def signup(request):
    if request.user.is_authenticated:
        return redirect("arena:home")
    form = UserCreationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        login(request, user)  # 註冊完直接登入，也會記一筆登入紀錄
        return redirect("arena:home")
    return render(request, "registration/signup.html", {"form": form, **_page_context()})
