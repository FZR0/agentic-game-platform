"""每一頁的模板都拿得到的資料（config/settings.py 的 TEMPLATES 登記）。"""
from . import operations
from .models import display_name


def viewer_name(request):
    """登入的人在畫面上的名稱（models.display_name，遊客加「（遊客）」）。play.html 的 viewer-name 要和玩家列表的名稱
    一模一樣，app.js 才標得出「你」，所以模板不自己拼 user.profile.name。"""
    user = getattr(request, "user", None)
    return {"viewer_name": display_name(user)} if user is not None and user.is_authenticated else {}


def active_room(request):
    """頂端的「回到房間」（2026-11-13 使用者要求：回到首頁或其他頁面後，要能回到自己開的或加入的房間）。
    只讀資料、不是使用者按的請求，所以不經過 AI 助理（和 request.user 一樣是畫面的一部分）。
    active_room 是最近有動靜的一間，more_rooms 是另外還有幾間。"""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {}
    rooms = operations.active_rooms(user)
    return {"active_room": rooms[0] if rooms else None, "more_rooms": max(0, len(rooms) - 1)}
