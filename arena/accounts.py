"""帳號：註冊、登入、登入紀錄與線上訪客（使用者要求，2026-11-05；docs/交接.md 4.10）。

- 任何人都可以註冊（使用者決定）；所有頁面與 API 都要登入（settings 的 LoginRequiredMiddleware），
  否則網址傳出去，別人就能用研究用伺服器。
- 註冊、登入不經過 AI 助理：密碼不能交給模型，而且這是安全檢查，不是遊戲的請求。
- 每次登入記一筆 LoginRecord（IP、瀏覽器）；PresenceMiddleware 記每個瀏覽器最後活動的時間，後台看得到目前在線的人。
- 遊客（2026-11-06）：不註冊也能玩，建立一個沒有密碼的暫時帳號；之後設定帳號名稱與密碼就升級成正式帳號，對局都保留。
"""
import ipaddress
import secrets
import time

from django.conf import settings
from django.contrib.auth import login, update_session_auth_hash
from django.contrib.auth.models import User
from django.contrib.auth.decorators import login_not_required
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.signals import user_logged_in
from django.contrib.auth.views import LoginView, redirect_to_login
from django.dispatch import receiver
from django.http import Http404, HttpResponsePermanentRedirect
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme

from .models import LoginRecord, Presence

LOOPBACK = {"127.0.0.1", "::1"}
LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]"}
GUEST_PREFIX = "遊客-"
GUEST_LETTERS = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # 和房間密碼一樣，不用容易看錯的 0／O、1／I／L
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


def from_this_machine(request):
    """直接在這台電腦上打開網站，不是經過 Cloudflare 通道。通道的請求也是從本機的 cloudflared 連進來，
    所以還要看網址（通道是 *.trycloudflare.com，Cloudflare 照網址轉送，外面的人改不了）與 CF-Connecting-IP（Cloudflare 一定會帶）。"""
    return (request.META.get("REMOTE_ADDR") in LOOPBACK and "HTTP_CF_CONNECTING_IP" not in request.META
            and request.get_host().rsplit(":", 1)[0] in LOCAL_HOSTS)


class PublicSiteMiddleware:
    """公開模式（settings.PUBLIC，經過通道對外）的兩道保護，只對經過通道的請求（2026-11-09 帳號安全檢查）：
    - http 一律轉到 https：通道網址用 http 也打得開，登入的 cookie 就沒加密傳出去，在公共 Wi-Fi 上可能被截取冒用；
      遊客沒有密碼，cookie 就是整個帳號。cookie 另外標成只走 https（settings 的 SESSION_COOKIE_SECURE）。
    - 管理員後台一律當成不存在：後台登入沒有猜密碼的次數限制。要看登入紀錄、線上訪客，在這台電腦開 http://localhost:<埠>/admin/。
    放在 SessionMiddleware 前面：轉到 https 時不建立 session、不送 cookie。"""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if settings.PUBLIC and not from_this_machine(request):
            if request.path.startswith("/admin/"):
                raise Http404
            if not request.is_secure():
                return HttpResponsePermanentRedirect(f"https://{request.get_host()}{request.get_full_path()}")
        return self.get_response(request)


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


def is_guest(user):
    """遊客＝沒有可用密碼的帳號（只能從建立它的瀏覽器登入）。不看名稱：註冊時也可以取「遊客-」開頭的名字。"""
    return user.is_authenticated and not user.has_usable_password()


def _safe_next(request):
    target = request.POST.get("next") or request.GET.get("next") or ""
    return target if url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}) else "arena:home"


@login_not_required
def guest(request):
    """以遊客身分進入：建立暫時帳號並登入。邀請連結帶來的 next 會保留，朋友可以直接到加入對局的頁面。"""
    if request.method != "POST":
        # 直接在網址列打這個網址：轉到登入頁按按鈕（2026-11-09，原本是看不懂的 405）。
        # 不能用打開網址就建立帳號：聊天軟體產生連結預覽時也會打開網址，每傳一次就多一個沒人用的遊客。
        next_url = request.GET.get("next")
        return redirect_to_login(next_url) if next_url else redirect("login")
    if not request.user.is_authenticated:
        while True:
            name = GUEST_PREFIX + "".join(secrets.choice(GUEST_LETTERS) for _ in range(6))
            if not User.objects.filter(username=name).exists():
                break
        user = User(username=name)
        user.set_unusable_password()
        user.save()
        login(request, user)  # 也會記一筆登入紀錄
    return redirect(_safe_next(request))


def upgrade(request):
    """遊客設定帳號名稱與密碼，變成正式帳號；是同一個帳號，所以對局、戰績、真人對戰的座位都保留。"""
    if not is_guest(request.user):
        return redirect("arena:home")
    # 名稱欄一開始是空的：留著「遊客-XXXXXX」直接送出的話，會因為名稱已經存在而失敗
    form = UserCreationForm(request.POST or None, instance=request.user, initial={"username": ""})
    if request.method == "POST" and form.is_valid():
        user = form.save()
        update_session_auth_hash(request, user)  # 改了密碼之後保持登入
        return redirect("arena:home")
    return render(request, "registration/upgrade.html", {"form": form, **_page_context()})


@login_not_required
def csrf_failure(request, reason=""):
    """表單驗證碼不符（settings.CSRF_FAILURE_VIEW）時的白話說明頁，取代 Django 看不懂的「CSRF 驗證失敗」（2026-11-09）。
    常見原因：登入後按上一頁、在舊頁面再按一次（登入會換新的驗證碼），或瀏覽器擋 cookie（有些 App 內建的瀏覽器）。
    真正的原因由 CsrfViewMiddleware 寫進網站紀錄（logger django.security.csrf），這裡不顯示技術訊息。
    「重新整理」回到送出表單的那一頁（Referer）重新拿驗證碼；不能用送出的網址，登出這類網址只接受 POST。"""
    back = request.META.get("HTTP_REFERER", "")
    if not url_has_allowed_host_and_scheme(back, allowed_hosts={request.get_host()}):
        back = ""
    return render(request, "registration/csrf_failure.html", {"retry": back, **_page_context()}, status=403)
