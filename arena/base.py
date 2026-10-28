"""遊戲模組的共用介面（研究計畫 3.3.1 的七個問題）與證據種類。

每個遊戲實作一個 GameModule 子類別；arena 的對局流程、Agent 迴圈、復盤與提問只透過這個介面使用遊戲。
行動一律以各遊戲記法的字串表示（例如五子棋 "H8"），存檔與傳送都用這個格式。
"""
import re

# 證據種類，對應研究計畫表 3-4
RULE = "規則確認"
SOLVED = "求解確認"
CONDITIONAL = "條件式計算"
ESTIMATE = "引擎估計"
EVIDENCE_TYPES = (RULE, SOLVED, CONDITIONAL, ESTIMATE)


def as_facts(points):
    """把 [(證據種類, 文字)] 轉成存檔與傳送用的 [{"evidence", "text"}]。"""
    return [p if isinstance(p, dict) else {"evidence": p[0], "text": p[1]} for p in points]


class GameModule:
    key = ""            # 網址與資料表用的代號，例如 "gomoku"
    name = ""           # 顯示名稱
    seat_names = {}     # {座位: 名稱}，例如 {1: "黑棋", 2: "白棋"}
    coord_pattern = ""  # 說明文字中座標的正規表示式；網頁也用同一個，第一個群組是座標
    default_steps = 6   # Agent 每手查證次數的預設值
    board_script = ""   # 網頁上畫棋盤的 JS（static 路徑）
    icon = ""           # 首頁遊戲卡片的圖（static 路徑，160×100 的 SVG）
    tagline = ""        # 首頁遊戲卡片上的一句介紹，寫給玩家看
    # 開局設定欄位，顯示在「設定」選單：[{"name", "label", "options": [[值, 顯示文字], ...], "default"}]
    settings_fields = []
    tools = []          # Agent 工具定義：find_threats / list_candidates / check_move / play_move
    benchmark_samples = []  # 測速用的固定局面（行動序列）
    human_seats = None  # 玩家可以選的座位；None 代表 seat_names 全部（撲克固定是 1 號位）
    numbered = True     # 棋盤能不能「顯示手數」
    # 這個遊戲的對局請求（開局、下棋、復盤、提問…）是否交給 AI 助理（arena/orchestrator.py）處理；
    # False 時直接由原本的程式處理（2026-10-28 先做五子棋與撲克，其他遊戲列在 docs/交接.md 待辦）
    agentic_requests = False
    step_word = "手"    # 「第幾手」的單位；撲克用「步」，因為「第 3 手」是第 3 手牌
    # 「規則」面板的內容，寫給玩家看（使用者要求每個遊戲都要有規則說明，2026-10-11）：[{"title", "items": [文字, ...]}]
    rules = []
    # 額外的參考表（撲克的起手牌勝率表），顯示在頂端列：{"label", "title", "note", "columns", "rows": [{"key", "cells"}]}
    reference = None

    @property
    def seat_choices(self):
        seats = self.human_seats or list(self.seat_names)
        return [(seat, self.seat_names[seat]) for seat in seats]

    @property
    def coord_re(self):
        return re.compile(self.coord_pattern)

    # ---- 規則（3.3.1 的問題 1–4） ----

    def clean_settings(self, data):
        """驗證開局設定（例如棋盤大小），回傳要存檔的設定；不合法時丟出 ValueError。"""
        return {}

    def new_secret(self):
        """開局時產生、只存在伺服器的資料（撲克的洗牌種子）。會以 settings["secret"] 傳給 new_state。"""
        return {}

    def new_state(self, settings):
        raise NotImplementedError

    def apply(self, state, action):
        """驗證並執行行動，回傳新的狀態（不修改原狀態）；不合法時丟出 ValueError。"""
        raise NotImplementedError

    def to_move(self, state):
        """輪到哪一個座位。"""
        raise NotImplementedError

    def outcome(self, state):
        """尚未結束時回傳 None；結束時回傳 (獲勝座位或 None, 是否和局)。"""
        raise NotImplementedError

    def frame(self, state, seat):
        """給網頁畫面的局面，只能包含 seat 看得到的資訊（3.3.1 問題 4）。必須有 "to_move"（輪到的座位）。"""
        raise NotImplementedError

    def normalize(self, text):
        """把文字轉成標準記法的行動；格式不對時回傳 None（不檢查是否合法）。"""
        raise NotImplementedError

    def replay(self, settings, actions):
        """從存檔的行動序列重建局面；序列不合法時丟出 ValueError。"""
        state = self.new_state(settings)
        for i, action in enumerate(actions):
            if self.outcome(state):
                raise ValueError(f"第 {i} 步已分出勝負，不能再繼續")
            state = self.apply(state, action)
        return state

    # ---- Agent ----

    def agent_prompt(self, seat):
        """Agent 的系統提示：規則、建議流程與硬性規定。"""
        raise NotImplementedError

    def board_text(self, state, actions):
        """給 Agent 看的局面描述。"""
        raise NotImplementedError

    def action_from(self, args):
        """從 play_move 的參數取出行動（標準記法），格式不對時回傳 None。"""
        raise NotImplementedError

    def run_tool(self, state, name, args):
        """執行查證工具，回傳 (步驟名稱, [(證據種類, 文字)])；不認得的工具回傳 None。"""
        raise NotImplementedError

    def step_text(self, action):
        """Agent 查證過程裡的一步，例如「下 H8」；撲克是「決定跟注」。"""
        return f"下 {action}"

    def rejection(self, state, action):
        """Agent 的行動違反硬性規定時回傳原因，合格時回傳 None。action 為 None 代表格式不對。"""
        raise NotImplementedError

    def forced(self, state):
        """不需要查證就能決定的一步（例如第一手、唯一合法步），回傳 {"action", "points"} 或 None。"""
        return None

    def engine_move(self, state):
        """引擎代下（Agent 失敗時）：回傳 {"action", "points"}。"""
        raise NotImplementedError

    def overall_points(self, settings, actions, human_seat):
        """整體評論用的遊戲統計（例如撲克的入池率）：[(證據種類, 文字)]；沒有就回傳空清單。"""
        return []

    def public_notes(self, state, notes):
        """AI 思路送到網頁前的過濾（3.3.1 問題 4）。撲克的思路含底牌，這手牌結束前要遮住。
        notes 是 [{"index", "seat", "facts", "narration", ...}]；遮住的要加 "hidden": True。"""
        return notes

    # ---- 復盤與提問 ----

    def review(self, settings, actions, human_seat):
        """玩家的關鍵失誤清單：[{"index", "type", "title", "played", "best", "points"}]。"""
        raise NotImplementedError

    def explain_position(self, state, focus, next_action):
        """提問用的局面事實；focus 是問題中提到的座標，next_action 是紀錄中實際的下一步。"""
        raise NotImplementedError

    def explain_action(self, settings, actions, n):
        """提問用：第 n 步（從 1 起算）的事實。"""
        raise NotImplementedError

    def question_focus(self, question):
        """問題中提到的座標（標準記法、不重複）。記法有大小寫之分的遊戲要覆寫。"""
        focus = []
        for match in self.coord_re.findall(question.upper()):
            action = self.normalize(match)
            if action and action not in focus:
                focus.append(action)
        return focus
