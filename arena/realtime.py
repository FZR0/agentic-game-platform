"""真人對戰的即時同步（WebSocket，Django Channels；2026-11-05，docs/交接.md 4.10）。

- 網頁打開一盤真人對戰時連到 ws/games/<編號>/；只有坐在這盤的真人和觀戰的人連得上（沒登入、別人的對局一律拒絕）。
  觀戰的人座位是 None：畫面只有所有人都看得到的資訊（2026-11-08）。
- 聊天室有新訊息時 views 呼叫 notify_chat(game)，每條連線送自己看得到的訊息（玩家、觀戰者開始後各一個聊天室，2026-11-13）。
- 有人下了一步（或有人加入、換座位）時，views 呼叫 notify(game)；每條連線依「自己現在的座位」重新產生畫面再送出，
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


def notify_chat(game):
    """聊天室有新訊息（2026-11-13）：每條連線依自己是玩家或觀戰者，重新送一次看得到的訊息。"""
    async_to_sync(get_channel_layer().group_send)(_group(game.id), {"type": "chat.changed"})


def seat_for(user, game_id):
    """(對局, 這位使用者在這盤真人對戰坐的座位)；觀戰的人座位是 None。
    不能連（沒登入、不是真人對戰、沒坐也沒在觀戰）時回傳 (None, None)。"""
    if not user.is_authenticated:
        return None, None
    game = Game.watchable_by(user).filter(pk=game_id, mode="pvp").first()
    if game is None:
        return None, None
    if not game.in_room(user):
        return None, None  # 不在房間裡（撲克牌桌打過牌後離開房間的人、管理員看得到的舊對局）
    return game, game.seat_of(user)


class GameConsumer(JsonWebsocketConsumer):
    def connect(self):
        self.game_id = self.scope["url_route"]["kwargs"]["game_id"]
        game, self.seat = seat_for(self.scope["user"], self.game_id)  # 觀戰的人 seat 是 None
        if game is None:
            self.close()
            return
        async_to_sync(self.channel_layer.group_add)(_group(self.game_id), self.channel_name)
        self.accept()
        self.game_changed(None)  # 連上（或斷線重連）時先送一次目前的局面，網頁不必另外請求（那會經過 AI 助理）
        self.chat_changed(None)

    def disconnect(self, code):
        async_to_sync(self.channel_layer.group_discard)(_group(self.game_id), self.channel_name)

    def game_changed(self, event):
        # 每次都重新查座位：房間大廳裡可以換座位、坐下或起身觀戰（2026-11-15）
        game, self.seat = seat_for(self.scope["user"], self.game_id)
        if game is None:  # 被房主請出房間（或自己離開了）
            self.send_json({"type": "left"})
            self.close()
            return
        module = games.get(game.game_type)
        self.send_json({"type": "update", **operations.game_detail(module, game, self.seat, self.scope["user"])})

    def chat_changed(self, event):
        game = Game.objects.get(pk=self.game_id)
        self.send_json({"type": "chat", "chat": operations.chat_messages(game, self.seat, self.scope["user"])})


websocket_urlpatterns = [path("ws/games/<int:game_id>/", GameConsumer.as_asgi())]
