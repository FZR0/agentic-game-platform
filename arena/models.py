import logging
import platform
import urllib.parse

from django.conf import settings
from django.db import models
from django.utils import timezone


class Game(models.Model):
    """一盤對局（任何遊戲）。actions 由伺服器逐步驗證後才寫入，因此一定是合法的紀錄。"""

    game_type = models.CharField(max_length=20)  # 遊戲模組的 key，例如 "gomoku"
    human_seat = models.PositiveSmallIntegerField()  # 玩家的座位；五子棋 1＝黑、2＝白
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

    @property
    def finished(self):
        return bool(self.winner or self.draw or self.ended)

    def summary(self):
        return {"id": self.id, "game_type": self.game_type, "human_seat": self.human_seat,
                "settings": self.settings, "winner": self.winner, "draw": self.draw, "ended": self.ended,
                "action_count": len(self.actions), "created_at": self.created_at.isoformat()}


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
             "overall": "整體評論", "ask": "提問",
             # AI 助理處理的使用者請求（arena/orchestrator.py，2026-10-28）
             "req_home": "請求：首頁", "req_history": "請求：對局紀錄", "req_guide": "請求：怎麼玩",
             "req_play_page": "請求：對局頁", "req_end_form": "請求：清單結束對局", "req_models": "請求：模型清單",
             "req_list_games": "請求：對局清單", "req_detail": "請求：打開對局", "req_end": "請求：結束對局",
             "req_review": "請求：復盤", "req_note": "請求：AI 思路說明", "req_narrate": "請求：說明",
             "req_new_game": "請求：新對局", "req_move": "請求：下棋", "req_ask": "請求：提問"}
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
