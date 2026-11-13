"""每一頁的模板都拿得到的資料（config/settings.py 的 TEMPLATES 登記）。"""
from . import operations


def active_room(request):
    """頂端的「回到房間」（2026-11-13 使用者要求：回到首頁或其他頁面後，要能回到自己開的或加入的房間）。
    只讀資料、不是使用者按的請求，所以不經過 AI 助理（和 request.user 一樣是畫面的一部分）。
    active_room 是最近有動靜的一間，more_rooms 是另外還有幾間。"""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {}
    rooms = operations.active_rooms(user)
    return {"active_room": rooms[0] if rooms else None, "more_rooms": max(0, len(rooms) - 1)}
