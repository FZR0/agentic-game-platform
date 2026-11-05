"""真人對戰的即時同步（WebSocket，Django Channels；2026-11-05，docs/交接.md 4.10）。

- 網頁打開一盤真人對戰時連到 ws/games/<編號>/；只有坐在這盤的真人連得上（沒登入、別人的對局一律拒絕）。
- 有人下了一步（或有人加入）時，views 呼叫 notify(game)；每條連線依「自己的座位」重新產生畫面再送出，
  所以撲克每個人只看得到自己的底牌（operations.game_detail 和打開對局時用的是同一個函式）。
- 推送不是使用者按的請求，不經過 AI 助理；下棋、加入仍然是 HTTP 請求，照樣經過 AI 助理。
- 通道層是 InMemoryChannelLayer（settings.CHANNEL_LAYERS）：只在同一個程序裡有效，runserver 只有一個程序，夠用；
  要開好幾個程序時要換成 Redis。
"""
from asgiref.sync import async_to_sync
from channels.generic.websocket import JsonWebsocketConsumer
from channels.layers import get_channel_layer
from django.urls import path

from . import games, operations
from .models import Game


def _group(game_id):
    return f"game_{game_id}"


def notify(game):
    """通知同一盤的所有連線：局面變了。沒有人連著時什麼都不做。"""
    async_to_sync(get_channel_layer().group_send)(_group(game.id), {"type": "game.changed"})


def seat_for(user, game_id):
    """這位使用者在這盤真人對戰坐的座位；不能連（沒登入、不是真人對戰、不在這盤）時回傳 (None, None)。"""
    if not user.is_authenticated:
        return None, None
    game = Game.visible_to(user).filter(pk=game_id, mode="pvp").first()
    seat = game.seat_of(user) if game else None
    return (game, seat) if seat is not None else (None, None)


class GameConsumer(JsonWebsocketConsumer):
    def connect(self):
        self.game_id = self.scope["url_route"]["kwargs"]["game_id"]
        game, self.seat = seat_for(self.scope["user"], self.game_id)
        if game is None:
            self.close()
            return
        async_to_sync(self.channel_layer.group_add)(_group(self.game_id), self.channel_name)
        self.accept()
        self.game_changed(None)  # 連上（或斷線重連）時先送一次目前的局面，網頁不必另外請求（那會經過 AI 助理）

    def disconnect(self, code):
        async_to_sync(self.channel_layer.group_discard)(_group(self.game_id), self.channel_name)

    def game_changed(self, event):
        game = Game.objects.get(pk=self.game_id)
        module = games.get(game.game_type)
        self.send_json({"type": "update", **operations.game_detail(module, game, self.seat)})


websocket_urlpatterns = [path("ws/games/<int:game_id>/", GameConsumer.as_asgi())]
