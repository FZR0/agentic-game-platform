import logging
import platform
import secrets
import urllib.parse
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

USER_MODEL = settings.AUTH_USER_MODEL  # Game 有一個欄位叫 settings，類別裡面不能直接用 settings.AUTH_USER_MODEL


class Game(models.Model):
    """一盤對局（任何遊戲）。actions 由伺服器逐步驗證後才寫入，因此一定是合法的紀錄。"""

    game_type = models.CharField(max_length=20)  # 遊戲模組的 key，例如 "gomoku"
    # 開局的人（2026-11-05 加帳號）；空的＝加帳號之前的舊對局，只有管理員（superuser）看得到（使用者決定）
    owner = models.ForeignKey(USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="games")
    human_seat = models.PositiveSmallIntegerField()  # 玩家的座位（真人對戰時是開局的人的座位）；五子棋 1＝黑、2＝白
    # 真人對戰（2026-11-05，docs/交接.md 4.10）：pvp 時真人的座位在 GamePlayer，對手用房間密碼加入
    MODES = {"ai": "和 AI 對戰", "pvp": "真人對戰"}
    mode = models.CharField(max_length=4, default="ai")
    room_code = models.CharField(max_length=8, blank=True, db_index=True)
    settings = models.JSONField(default=dict)  # 各遊戲的設定，例如圍棋的棋盤大小
    actions = models.JSONField(default=list)  # 各遊戲記法的字串，例如五子棋 "H8"
    secret = models.JSONField(default=dict, blank=True)  # 只在伺服器的資料（撲克的洗牌種子），絕不送到網頁
    winner = models.PositiveSmallIntegerField(null=True, blank=True)  # 獲勝的座位
    draw = models.BooleanField(default=False)
    # 玩家按「結束（不計勝負）」：對局停在這裡，不算輸贏、不進戰績、不能再下，仍可復盤（使用者要求，2026-10-21）
    ended = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    @classmethod
    def visible_to(cls, user):
        """這位使用者看得到的對局：自己開的、真人對戰裡坐了一個座位的；沒有擁有者的舊對局只給管理員看。"""
        mine = models.Q(owner=user) | models.Q(players__user=user)
        if user.is_superuser:
            mine |= models.Q(owner__isnull=True)
        return cls.objects.filter(pk__in=cls.objects.filter(mine).values("pk"))  # 子查詢避免 join 造成重複

    @classmethod
    def watchable_by(cls, user):
        """看得到的對局，再加上用房間密碼進來觀戰的（2026-11-08）。只用在打開對局與即時同步：
        清單、戰績、下棋、AI 教練仍用 visible_to，觀戰的對局不算是自己的。"""
        watching = cls.objects.filter(spectators__user=user).values("pk") if user.is_authenticated else []
        return cls.objects.filter(models.Q(pk__in=cls.visible_to(user).values("pk")) | models.Q(pk__in=watching))

    @property
    def pvp(self):
        return self.mode == "pvp"

    def human_seats(self):
        """真人坐的座位；其他座位由 AI 行動。"""
        if self.pvp:
            return {p.seat for p in self.players.all()}
        return {self.human_seat}

    def seat_of(self, user):
        """這位使用者坐哪個座位；和 AI 的對局只有一位真人。不在這盤裡時是 None。"""
        if not self.pvp:
            return self.human_seat
        return next((p.seat for p in self.players.all() if p.user_id == user.id), None)

    @property
    def waiting(self):
        """真人對戰還有空的真人座位：等對手用房間密碼加入，還不能開始下。"""
        return self.pvp and any(p.user_id is None for p in self.players.all())

    @staticmethod
    def new_room_code():
        """房間密碼：6 個字，不用容易看錯的 0／O、1／I／L。"""
        letters = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
        while True:
            code = "".join(secrets.choice(letters) for _ in range(6))
            if not Game.objects.filter(room_code=code).exists():
                return code

    @property
    def finished(self):
        return bool(self.winner or self.draw or self.ended)

    def summary(self):
        data = {"id": self.id, "game_type": self.game_type, "human_seat": self.human_seat, "mode": self.mode,
                "settings": self.settings, "winner": self.winner, "draw": self.draw, "ended": self.ended,
                "action_count": len(self.actions), "created_at": self.created_at.isoformat()}
        if self.pvp:
            data["players"] = {p.seat: p.user.username if p.user else None for p in self.players.all()}
            data["waiting"] = self.waiting
            data["room_code"] = self.room_code if self.waiting else ""
        return data


class GamePlayer(models.Model):
    """真人對戰的一個真人座位。user 是空的＝還在等人用房間密碼加入（2026-11-05）。"""

    game = models.ForeignKey(Game, on_delete=models.CASCADE, related_name="players")
    seat = models.PositiveSmallIntegerField()
    user = models.ForeignKey(USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="seats")
    joined_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["seat"]
        constraints = [models.UniqueConstraint(fields=["game", "seat"], name="unique_arena_player_seat")]


class GameSpectator(models.Model):
    """真人對戰的觀戰者（使用者要求，2026-11-08）：有房間密碼或邀請連結的人按「觀戰」就記一筆。
    沒有座位：畫面只有所有人都看得到的資訊（撲克的底牌要攤牌才看得到），不能下棋，也不能用 AI 教練。"""

    game = models.ForeignKey(Game, on_delete=models.CASCADE, related_name="spectators")
    user = models.ForeignKey(USER_MODEL, on_delete=models.CASCADE, related_name="watching")
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["game", "user"], name="unique_arena_spectator")]


class AiNote(models.Model):
    """AI 某一步的思路事實與說明，復盤時可以回看。

    獨立成一張表：背景產生說明時只更新這一列，不會蓋掉同時間剛寫入的行動。
    """

    game = models.ForeignKey(Game, on_delete=models.CASCADE, related_name="ai_notes")
    index = models.PositiveSmallIntegerField()  # 這一步在 Game.actions 中的位置，從 0 起算
    seat = models.PositiveSmallIntegerField()  # 哪一個座位的 AI；撲克有多位 AI 時用來區分
    facts = models.JSONField()
    narration = models.JSONField(null=True, blank=True)  # Agent 的查證過程，或語言模型成功改寫後的說明

    class Meta:
        ordering = ["index"]
        constraints = [models.UniqueConstraint(fields=["game", "index"], name="unique_arena_note_per_action")]

    def to_dict(self):
        return {"index": self.index, "seat": self.seat, "facts": self.facts, "narration": self.narration}


class ModelRun(models.Model):
    """每用一次語言模型就記一筆：哪個模型、哪台伺服器、花多久、有沒有成功（使用者要求，2026-10-19）。
    manage.py model_report 依這張表整理各模型的差異。

    Agent 每一手（含改由引擎代下的）、AI 思路說明、問題說明、整體評論、提問、測速都記；
    不需要模型的步驟（第一手、唯一合法步、撲克的風格機器人）不記。對局刪掉後紀錄仍保留，統計不會變。
    """

    KINDS = {"agent": "Agent 下棋", "benchmark": "測速", "ai_move": "AI 思路說明", "review": "問題說明",
             "overall": "整體評論", "ask": "提問", "opponent": "對手觀察",
             # AI 助理處理的使用者請求（arena/orchestrator.py，2026-10-28）
             "req_home": "請求：首頁", "req_history": "請求：對局紀錄", "req_guide": "請求：怎麼玩",
             "req_play_page": "請求：對局頁", "req_end_form": "請求：清單結束對局", "req_models": "請求：模型清單",
             "req_list_games": "請求：對局清單", "req_detail": "請求：打開對局", "req_end": "請求：結束對局",
             "req_review": "請求：復盤", "req_note": "請求：AI 思路說明", "req_narrate": "請求：說明",
             "req_new_game": "請求：新對局", "req_move": "請求：下棋", "req_ask": "請求：提問",
             "req_join_page": "請求：加入對戰頁", "req_join": "請求：加入對戰"}  # 真人對戰（2026-11-05）
    # ok：模型完成了（Agent 自己決定、說明通過檢查）；fallback：模型有回應但沒完成（次數內沒決定、說明沒通過檢查）；
    # error：呼叫不到模型（連不上、逾時、伺服器暫停呼叫、HTTP 錯誤）。error 反映的是伺服器，不算進模型的成功率與秒數。
    OUTCOMES = {"ok": "成功", "fallback": "沒完成", "error": "呼叫失敗"}

    created_at = models.DateTimeField(default=timezone.now)  # 匯入的舊紀錄只知道是哪一盤，用對局的開始時間
    kind = models.CharField(max_length=20)  # KINDS 的 key
    game_type = models.CharField(max_length=20)
    game = models.ForeignKey(Game, null=True, blank=True, on_delete=models.SET_NULL, related_name="model_runs")
    index = models.PositiveSmallIntegerField(null=True, blank=True)  # Agent：第幾步（從 0 起算）；提問：問的是第幾步之後
    model = models.CharField(max_length=100)
    server = models.CharField(max_length=100, blank=True)  # Ollama 伺服器的主機；空的＝加這張表之前匯入的舊紀錄，不明
    machine = models.CharField(max_length=100, blank=True)  # 跑網站的電腦：Agent 工具的引擎在這台算，本機 Ollama 也在這台
    seconds = models.FloatField()  # 總秒數；Agent 是到它做出決定或放棄為止，含引擎工具的時間，不含引擎代下
    model_seconds = models.FloatField(null=True, blank=True)  # 其中等模型回應的秒數
    calls = models.PositiveSmallIntegerField(null=True, blank=True)  # 呼叫模型幾次
    steps = models.PositiveSmallIntegerField(null=True, blank=True)  # Agent 的查證次數上限（玩家設定）
    rejected = models.PositiveSmallIntegerField(null=True, blank=True)  # Agent 想下卻被退回的次數
    outcome = models.CharField(max_length=10)  # OUTCOMES 的 key
    problem = models.TextField(blank=True)  # 沒成功的原因

    @classmethod
    def record(cls, kind, game_type, run, game=None, index=None):
        """存一筆紀錄。run 是 agent.agent_move 回傳的 run，或欄位相同的 dict（model、seconds、outcome…）。
        同時寫進運行紀錄（config/settings.py 的 LOGGING），沒成功的用 WARNING。"""
        where = f"{game_type}" + (f" 第 {game.pk} 盤" if game else "") + (f" 第 {index + 1} 步" if index is not None else "")
        logging.getLogger(__name__).log(
            logging.INFO if run["outcome"] == "ok" else logging.WARNING,
            "%s（%s）：%s，%.1f 秒，模型 %s，呼叫 %s 次，被退回 %s 次%s", cls.KINDS.get(kind, kind), where,
            cls.OUTCOMES.get(run["outcome"], run["outcome"]), run["seconds"], run["model"], run.get("calls"),
            run.get("rejected"), f"；原因：{run['problem']}" if run.get("problem") else "")
        return cls.objects.create(kind=kind, game_type=game_type, game=game, index=index,
                                  server=urllib.parse.urlsplit(settings.OLLAMA_URL).hostname or "",
                                  machine=platform.node(), **run)


class LoginRecord(models.Model):
    """每次登入一筆：誰、從哪個 IP、用什麼瀏覽器（使用者要求，2026-11-05：管理員後台要看得到登入 IP）。"""

    user = models.ForeignKey(USER_MODEL, on_delete=models.CASCADE, related_name="login_records")
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = verbose_name_plural = "登入紀錄"


class Presence(models.Model):
    """每個瀏覽器（session）最後一次在網站上活動的時間（使用者要求，2026-11-05：後台要看得到目前有誰在瀏覽）。
    arena.accounts.PresenceMiddleware 寫入；ONLINE 之內有活動的算「目前在線」。沒登入的訪客 user 是空的。"""

    ONLINE = timedelta(minutes=5)

    session_key = models.CharField(max_length=40, unique=True)
    user = models.ForeignKey(USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="presences")
    ip = models.GenericIPAddressField(null=True, blank=True)
    path = models.CharField(max_length=200, blank=True)
    user_agent = models.CharField(max_length=300, blank=True)
    first_seen = models.DateTimeField(default=timezone.now)
    last_seen = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-last_seen"]
        verbose_name = verbose_name_plural = "瀏覽中的訪客"

    @classmethod
    def online(cls):
        return cls.objects.filter(last_seen__gte=timezone.now() - cls.ONLINE)
