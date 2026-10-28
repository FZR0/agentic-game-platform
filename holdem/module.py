"""德州撲克的遊戲模組：rules.py（規則確認）＋cards.py 的勝率模擬（條件式計算：結論取決於列出的假設）。

座位：玩家固定 1 號位；「1 位 Agent＋風格機器人」時 2 號位是 Agent、其他是風格機器人，「全部 Agent」時 AI 都是 Agent。
隱藏資訊：frame 只含 viewer 看得到的牌；AI 的思路含底牌，這手牌結束前由 public_notes 遮住。
分析一律只用行動者當下看得到的資訊（自己的底牌＋公共牌＋公開的行動），不看別人的底牌。
"""
import json
import random
import re
import secrets
from collections import OrderedDict
from pathlib import Path

from arena.base import CONDITIONAL, ESTIMATE, RULE, GameModule

from . import cards
from .rules import HUMAN, STREETS, Table, action_text, seat_label

SB, BB = 10, 20
EQUITY_FILE = Path(__file__).with_name("preflop_equity.json")  # manage.py poker_equity_table 產生
SIMS = 600      # 引擎（Agent 工具、代下、復盤）每次模擬次數；三者用同一個數字與種子，結果才會一致
BOT_SIMS = 300  # 風格機器人用的模擬次數（求快）
# 問題檢查的分級（使用者要求列出全部問題並分成關鍵時刻與小失誤，2026-10-11）：損失＝最好的選擇比你的選擇多的期望值。
# 門檻同時看固定金額與底池比例，因為模擬 600 次的誤差大約是底池的 2%，太小的差距不算數。
KEY_LOSS, KEY_SHARE = 2 * BB, 0.2       # 關鍵時刻：少 2 個大盲以上，而且至少是底池的 20%
MINOR_LOSS, MINOR_SHARE = BB // 2, 0.05  # 小失誤：少半個大盲以上，而且至少是底池的 5%
SUGGEST_LOSS, SUGGEST_SHARE = BB // 4, 0.02  # 建議：不算失誤，但有更推薦的打法（使用者要求，2026-10-11）；再小就在模擬誤差內
PASSIVE = {"check": 3, "call": 2, "raise": 1, "allin": 1, "fold": 0}  # 期望值一樣時，偏好比較保守的選擇
CONTINUE_MIN = 0.2  # 面對加注，每位對手至少用範圍裡最強的 20% 繼續，免得把超大下注算成對手幾乎都會棄牌
# 翻牌後的動作透露牌力（使用者要求依對手的動作判斷是否比你大）：不符合條件的牌只保留一部分（詐唬或亂跟的比例）
READS = {"bet": 0.3, "raise": 0.15, "call": 0.35}
READ_WORDS = {"bet": "下注", "raise": "加注", "call": "跟注"}
READ_RULE = ("對手翻牌後下注，假設代表至少一對或聽牌（約 3 成是詐唬）；加注代表頂對以上或聽牌（約 1.5 成是詐唬）；"
             "跟注代表有對子或聽牌（約 3.5 成是亂跟）")
RAISE_RULE = ("加注的期望值假設每位對手面對加注時，照「底池 ÷（底池＋你加的籌碼）」的比例繼續（至少 20%），"
              "留下來的是範圍裡最強的牌，而且只會有一個人跟；只算到攤牌，不算之後的下注。"
              "引擎只在勝率高於平分時才考慮加注，不詐唬")
MIN_PLAYERS, MAX_PLAYERS = 2, 10  # 含玩家（使用者決定，2026-10-11）
# 依翻牌前最積極的行動推估對手範圍（起手牌前百分之幾）；翻牌後不再縮小
RANGES = {"raise": 0.15, "call": 0.35, "check": 0.6, None: 0.5}
RANGE_TEXT = {"raise": "翻牌前加注過，假設前 15% 的起手牌", "call": "翻牌前跟注過，假設前 35%",
              "check": "翻牌前只有過牌，假設前 60%", None: "翻牌前還沒表態，假設前 50%"}
STYLES = {  # play：翻牌前會玩的起手牌範圍；open：會加注的範圍；raise_at：加注門檻倍率；call_margin：跟注時放寬的勝率
    "tag": {"name": "緊兇", "play": 0.20, "open": 0.12, "raise_at": 1.0, "call_margin": 0.0, "bluff": 0.0},
    "lag": {"name": "鬆兇", "play": 0.40, "open": 0.25, "raise_at": 0.8, "call_margin": 0.05, "bluff": 0.25},
    "station": {"name": "跟注站", "play": 0.55, "open": 0.03, "raise_at": 1.6, "call_margin": 0.15, "bluff": 0.0},
    "rock": {"name": "岩石", "play": 0.12, "open": 0.06, "raise_at": 1.3, "call_margin": -0.05, "bluff": 0.0},
}
STYLE_ORDER = ["tag", "lag", "station", "rock"]
# 整體評論用的參考：依人數的常見主動入池比例（一般經驗值，不是這盤算出來的）。人越少，該玩的手牌越多
VPIP_RANGES = [(2, 0.50, 0.80), (4, 0.30, 0.45), (6, 0.20, 0.32), (10, 0.15, 0.25)]
CARD_RE = r"(?<![0-9A-Za-z])((?:10|[2-9JQKA])[♠♥♦♣])"
QUESTION_WORDS = [("棄牌", "fold"), ("蓋牌", "fold"), ("FOLD", "fold"), ("跟注", "call"), ("CALL", "call"),
                  ("過牌", "check"), ("CHECK", "check"), ("全下", "allin"), ("ALLIN", "allin"), ("ALL-IN", "allin"),
                  ("ALL IN", "allin")]


def pct(x):
    return f"{x * 100:.0f}%"


def pct1(x):
    return f"{x * 100:.1f}%"


def fits(kind, hole, board):
    """對手在這個牌面做了 kind（bet／raise／call），手上的牌符不符合這個動作通常代表的牌力。"""
    if cards.has_draw(hole, board):
        return True
    mine = cards.evaluate(hole + board)
    improves = mine[0] > cards.evaluate(board)[0]  # 牌力來自自己的底牌，不是公共牌本身
    if kind == "raise":
        top_pair = mine[0] == 1 and improves and mine[1] >= max(cards.value(c) for c in board)
        return top_pair or (improves and mine[0] >= 2)
    return improves


class HoldemModule(GameModule):
    key = "holdem"
    agentic_requests = True
    name = "德州撲克"
    seat_names = {s: f"{s} 號位" for s in range(1, MAX_PLAYERS + 1)}
    human_seats = [HUMAN]
    numbered = False
    step_word = "步"
    coord_pattern = CARD_RE
    default_steps = 3
    board_script = "holdem/board.js"
    icon = "holdem/icon.svg"
    tagline = "2–10 人無限注，盲注 10／20。分析會算出勝率與期望值。"
    rules = [
        {"title": "目標", "items": ["用手上的兩張底牌，加上桌面的五張公共牌，組出最好的五張牌，贏下底池裡的籌碼。",
                                  "一場牌局會打很多手牌，你可以一直玩到想停為止。"]},
        {"title": "一手牌怎麼進行", "items": ["每人發兩張只有自己看得到的底牌。按鈕（D）左邊的兩位先放小盲 10、大盲 20。",
                                         "翻牌前：從大盲左邊的人開始輪流決定。只剩兩個人時，按鈕是小盲而且翻牌前先說話。",
                                         "翻牌（三張公共牌）、轉牌（第四張）、河牌（第五張），每發一次牌就再輪一次。",
                                         "最後還沒棄牌的人亮牌比大小（攤牌）；其他人都棄牌時，剩下的那個人直接贏。按「下一手」繼續。"]},
        {"title": "輪到你時可以做什麼", "items": ["棄牌：放棄這手牌，已經下的注拿不回來。",
                                           "過牌：沒有人下注時，不花錢讓下一個人說話。",
                                           "跟注：補到和目前最高的下注一樣多。",
                                           "加注：把這一輪的下注加到某個金額；至少要比上一次加的多一樣多（例如別人從 20 加到 60，你至少要加到 100）。",
                                           "All in：把桌上的籌碼全部押下去。籌碼不夠跟的時候也可以 All in，只能贏到自己押得到的那一份（邊池）。"]},
        {"title": "牌型大小（由大到小）", "items": ["同花順：同花色的順子，例如 9♠ 8♠ 7♠ 6♠ 5♠。",
                                             "四條：四張同點數。",
                                             "葫蘆：三條加一對。",
                                             "同花：五張同花色。",
                                             "順子：五張連續點數（A 可以當最大，也可以當 1，例如 A 2 3 4 5）。",
                                             "三條：三張同點數。",
                                             "兩對、一對、高牌。",
                                             "牌型一樣時比點數，再比剩下的牌（踢腳）；完全一樣就平分底池。花色不分大小。"]},
        {"title": "這個牌桌", "items": ["你固定坐 1 號位，按鈕每手輪流移動，所以每個位置都會輪到。",
                                     "可以設定人數 2–10 人、起始籌碼、AI 組成（1 位會查證思考的 Agent 加上不同風格的機器人，或全部都是 Agent）。",
                                     "你輸光、其他人都輸光，或你按「離場」時，牌局結束。離場會帶走目前的籌碼；一手牌進行中離場等於先棄牌。",
                                     "AI 看不到你的底牌，它只知道自己的牌、公共牌和大家做過的動作。"]},
    ]
    settings_fields = [
        {"name": "players", "label": "人數（含你）",
         "options": [[n, f"{n} 人"] for n in range(MIN_PLAYERS, MAX_PLAYERS + 1)], "default": 6},
        {"name": "ai_mode", "label": "AI 組成",
         "options": [["agent_bots", "1 位 Agent＋風格機器人"], ["all_agent", "全部 Agent（較慢）"]], "default": "agent_bots"},
        {"name": "stack", "label": "起始籌碼（盲注 10/20）",
         "options": [[1000, "1000（50 個大盲）"], [2000, "2000（100 個大盲）"], [4000, "4000（200 個大盲）"]], "default": 2000},
    ]
    tools = [
        {"type": "function", "function": {
            "name": "find_threats",
            "description": "看牌面：自己的牌型、補牌張數、公共牌的危險（對子、同花、順子面）、底池與要跟注的金額。",
            "parameters": {"type": "object", "properties": {}}}},
        {"type": "function", "function": {
            "name": "list_candidates",
            "description": "在列出的對手範圍假設下，模擬勝率並比較棄牌、跟注、加注的期望值。",
            "parameters": {"type": "object", "properties": {}}}},
        {"type": "function", "function": {
            "name": "check_move",
            "description": "檢查一個行動合不合法，以及它在假設下的期望值。",
            "parameters": {"type": "object", "properties": {
                "action": {"type": "string", "description": "fold、check、call、allin 或 raise N（把這一輪的下注加到 N）"}},
                "required": ["action"]}}},
        {"type": "function", "function": {
            "name": "play_move",
            "description": "決定行動。",
            "parameters": {"type": "object", "properties": {
                "action": {"type": "string", "description": "fold、check、call、allin 或 raise N"},
                "reason": {"type": "string", "description": "一句話說明為什麼這樣做"}},
                "required": ["action", "reason"]}}},
    ]

    def __init__(self):
        self._cache = OrderedDict()
        self._benchmark = None
        self._reference = None

    @property
    def reference(self):
        """起手牌勝率表（使用者要求，2026-10-11）：169 種起手牌對上一手隨機牌打到攤牌的勝率與平手率。"""
        if self._reference is None:
            try:
                data = json.loads(EQUITY_FILE.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
            rows = sorted(data["rows"], key=lambda r: -r["win"])
            self._reference = {
                "label": "勝率表", "title": "起手牌勝率表",
                "note": (f"每種起手牌對上一位拿隨機兩張牌的對手、一路打到攤牌的機率（電腦模擬 {data['sims']:,} 次）。"
                         "人越多勝率越低；這張表適合比較哪些起手牌比較強。"),
                "columns": ["起手牌", "勝率", "平手率"],
                "rows": [{"key": r["hand"], "cells": [cards.hand_label(r["hand"]), pct1(r["win"]), pct1(r["tie"])]}
                         for r in rows],
            }
        return self._reference

    # ---- 規則 ----

    def clean_settings(self, data):
        players = int(data.get("players", 6))
        ai_mode = data.get("ai_mode", "agent_bots")
        stack = int(data.get("stack", 2000))
        if not MIN_PLAYERS <= players <= MAX_PLAYERS:
            raise ValueError(f"人數必須是 {MIN_PLAYERS} 到 {MAX_PLAYERS} 人（含你）")
        if ai_mode not in ("agent_bots", "all_agent"):
            raise ValueError("AI 組成只能是「1 位 Agent＋風格機器人」或「全部 Agent」")
        if stack not in (1000, 2000, 4000):
            raise ValueError("起始籌碼必須是 1000、2000 或 4000")
        return {"players": players, "ai_mode": ai_mode, "stack": stack, "sb": SB, "bb": BB}

    def new_secret(self):
        return {"seed": secrets.randbits(48)}

    def new_state(self, settings):
        s = {**self.clean_settings({}), **(settings or {})}
        t = Table(s["players"], s["stack"], s["sb"], s["bb"], (s.get("secret") or {}).get("seed", 0))
        t.roles = {HUMAN: "human"}
        for i, seat in enumerate(range(2, s["players"] + 1)):
            agent = s["ai_mode"] == "all_agent" or i == 0
            t.roles[seat] = "agent" if agent else STYLE_ORDER[(i - 1) % len(STYLE_ORDER)]
        return t

    def role_name(self, t, seat):
        role = t.roles[seat]
        return {"human": "", "agent": "Agent"}.get(role, f"{STYLES.get(role, {}).get('name', '')}機器人")

    def normalize(self, text):
        t = str(text or "").strip().lower()
        words = {"fold": "fold", "棄牌": "fold", "check": "check", "過牌": "check", "call": "call", "跟注": "call",
                 "allin": "allin", "all-in": "allin", "all in": "allin", "全下": "allin", "deal": "deal", "下一手": "deal",
                 "leave": "leave", "離場": "leave"}
        if t in words:
            return words[t]
        m = re.match(r"^(?:raise|bet|加注|下注)\s*(?:to|到)?\s*(\d+)$", t)
        return f"raise {int(m.group(1))}" if m else None

    def apply(self, state, action):
        t = state.copy()
        t.apply(action)
        return t

    def to_move(self, state):
        return HUMAN if state.hand_over else state.to_act

    def outcome(self, state):
        r = state.result()
        return (r[0], r[0] is None) if r else None  # 離場時不輸不贏算和局

    def frame(self, t, seat):
        last = {}  # 每個座位在「這一輪」最後的動作；還沒動作的座位，網頁改顯示這手牌已經下注的總額
        for s, action, street, _ in t.history:
            if street == t.street:
                last[s] = action_text(action)
        seats = []
        for s in t.seats:
            if s not in t.hole:
                shown = []  # 輸光、沒有發牌
            elif s == seat or s in t.shown:
                shown = t.hole[s]  # 自己的牌，或攤牌時亮出的牌
            elif s in t.folded:
                shown = []
            else:
                shown = [None, None]  # 背面朝上
            # 目前的牌型只給看得到牌的座位（自己的牌、攤牌時亮出的牌），否則等於洩漏別人的底牌
            visible = bool(shown) and None not in shown
            seats.append({"seat": s, "name": "你" if s == HUMAN else f"{s} 號位", "role": self.role_name(t, s), "stack": t.stacks[s],
                          "bet": t.bet[s], "folded": s in t.folded and s in t.hole, "out": s not in t.hole,
                          "allin": s in t.allin, "cards": shown, "last": last.get(s, ""), "total": t.total[s],
                          "hand_name": cards.describe(cards.evaluate(t.hole[s] + t.board)) if visible else "",
                          "won": t.winnings.get(s, 0), "button": s == t.button})
        legal = None
        if not t.hand_over and t.to_act == seat:
            L = t.legal(seat)
            legal = {"to_call": L["to_call"], "check": L["check"], "call": L["call"], "raise": L["raise"],
                     "allin": L["allin"], "pot": t.pot(), "bet": t.bet[seat], "current": t.current, "bb": t.bb}
        r = t.result()
        return {"hand": t.hand_no, "street": STREETS[t.street], "board": t.board, "pot": t.pot(), "seats": seats,
                "to_move": self.to_move(t), "legal": legal, "hand_over": t.hand_over, "hand_result": t.hand_result,
                "result": r[1] if r else None, "last": t.actions[-1] if t.actions else None,
                "last_seat": t.last_seat}

    # ---- 分析（只用 seat 看得到的資訊） ----

    def analysis(self, t, seat, sims=SIMS):
        """seat 這一步的分析：勝率、底池、每個選擇的期望值（相對於棄牌，平均每次多賺或少虧的籌碼）與最好的選擇。"""
        key = (t.seed, tuple(t.actions), seat, sims)
        if key not in self._cache:
            opponents = [s for s in t.in_hand() if s != seat]
            rng = random.Random(f"{t.seed}:{len(t.actions)}:{seat}:{sims}")
            ranges = [r for r in (self._opponent_range(t, seat, o) for o in opponents) if r]
            eq = cards.equity_vs(t.hole[seat], t.board, ranges, sims, rng)
            L = t.legal(seat)
            pot, to_call = t.pot(), L["to_call"]
            ev = {}
            if to_call:
                ev["fold"] = 0.0
            if L["check"]:
                ev["check"] = eq * pot
            if L["call"]:
                ev["call"] = eq * (pot + to_call) - to_call
            fair = 1 / (len(opponents) + 1)
            # 只在牌力領先（勝率高於平分）時考慮加注：要的是讓對手多付錢，不是詐唬（單街模型會高估詐唬）
            for target in (self._raise_targets(t, seat) if eq >= fair else []):
                ev[self._raise_action(t, seat, target)] = self._raise_ev(t, seat, target, sims)
            best = max(ev, key=lambda a: (ev[a], PASSIVE[a.split()[0]]))
            self._cache[key] = {
                "equity": eq, "pot": pot, "to_call": to_call, "legal": L, "ev": ev, "best": best,
                "pot_odds": to_call / (pot + to_call) if to_call else 0.0,
                "ev_call": eq * (pot + to_call) - to_call,
                "fair": fair,
            }
            while len(self._cache) > 512:
                self._cache.popitem(last=False)
        return self._cache[key]

    def _reads(self, t, opp):
        """對手這手牌翻牌後的下注、加注、跟注：[(第幾條街, 動作種類)]。"""
        return [(street, kind) for s, _, street, kind in t.history if s == opp and street > 0 and kind in READS]

    def _opponent_range(self, t, seat, opp, reads=True):
        """seat 眼中 opp 可能的牌：翻牌前的動作決定起手牌範圍，翻牌後的動作再調整權重（reads=False 時不看）。

        只排除 seat 看得到的牌（自己的底牌、公共牌），所以結果和 opp 實際拿什麼無關。
        """
        key = ("range", t.seed, len(t.actions), seat, opp, reads)
        if key not in self._cache:
            limit = RANGES[t.pre.get(opp)]
            actions = self._reads(t, opp) if reads else []
            boards = {1: t.board[:3], 2: t.board[:4], 3: t.board[:5]}

            def weight(a, b):
                if cards.TOP[frozenset((a, b))] > limit:
                    return 0.0
                w = 1.0
                for street, kind in actions:
                    if not fits(kind, [a, b], boards[street]):
                        w *= READS[kind]
                return w
            self._cache[key] = cards.weighted_range(set(t.hole[seat]) | set(t.board), weight)
        return self._cache[key]

    def _continuing(self, t, seat, opp, fraction):
        """opp 面對加注時繼續的牌：範圍裡最強的 fraction（依權重計算）。"""
        key = ("sorted", t.seed, len(t.actions), seat, opp)
        if key not in self._cache:
            r = self._opponent_range(t, seat, opp)
            if not r:
                self._cache[key] = None
            else:
                combos, cum = r
                weights = [c - p for c, p in zip(cum, [0.0] + cum[:-1])]
                if t.board:
                    score = lambda h: cards.evaluate(h + t.board)
                else:
                    score = lambda h: -cards.TOP[frozenset(h)]
                order = sorted(range(len(combos)), key=lambda i: score(combos[i]), reverse=True)
                self._cache[key] = ([combos[i] for i in order], [weights[i] for i in order], cum[-1])
        ranked = self._cache[key]
        if not ranked:
            return None
        combos, weights, total = ranked
        kept, cum, running = [], [], 0.0
        for combo, w in zip(combos, weights):
            if running >= fraction * total:
                break
            running += w
            kept.append(combo)
            cum.append(running)
        return kept, cum

    def _raise_target(self, t, seat, fraction):
        """加注到「目前下注＋底池的 fraction」，調整到合法範圍、取 10 的倍數；不能加注時回傳 None。"""
        L = t.legal(seat)
        if L["raise"] is None:
            return None
        low, high = L["raise"]
        size = max(t.min_raise, round(fraction * (t.pot() + L["to_call"]) / 10) * 10)
        return min(high, max(low, t.current + size))

    def _raise_targets(self, t, seat):
        """引擎考慮的加注金額：半個底池與一個底池（籌碼不夠時就是 All in）。籌碼很深時不考慮直接 All in。"""
        return sorted({x for x in (self._raise_target(t, seat, 0.5), self._raise_target(t, seat, 1.0)) if x})

    def _raise_action(self, t, seat, target):
        return "allin" if target == t.bet[seat] + t.stacks[seat] else f"raise {target}"

    def _raise_ev(self, t, seat, target, sims=SIMS):
        """加注到 target 的期望值（假設見 RAISE_RULE）：對手全棄就贏下底池，有人跟就打到攤牌。"""
        key = ("raise", t.seed, len(t.actions), seat, target, sims)
        if key not in self._cache:
            pot, cost = t.pot(), target - t.bet[seat]
            responders = [o for o in t.in_hand() if o != seat and o not in t.allin]
            go_on = max(CONTINUE_MIN, pot / (pot + cost))
            ranges = [r for r in (self._continuing(t, seat, o, go_on) for o in responders) if r]
            if not ranges:
                self._cache[key] = float(pot)
            else:
                someone = 1 - (1 - go_on) ** len(responders)
                rng = random.Random(f"{t.seed}:{len(t.actions)}:{seat}:{sims}:{target}")
                eq = cards.equity_vs(t.hole[seat], t.board, ranges, max(100, sims // 2), rng, mixture=True)
                call = sum(min(target - t.bet[o], t.stacks[o]) for o in responders) / len(responders)
                self._cache[key] = (1 - someone) * pot + someone * (eq * (pot + cost + call) - cost)
        return self._cache[key]

    def _action_key(self, t, seat, action):
        """把行動換成期望值表用的寫法：不夠跟注的 All in 算跟注，其他加注寫成 raise N 或 allin。"""
        if action in ("fold", "check", "call"):
            return action
        target = t.bet[seat] + t.stacks[seat] if action == "allin" else int(action.split()[1])
        return "call" if target <= t.current else self._raise_action(t, seat, target)

    def _ev_of(self, t, seat, action):
        a = self.analysis(t, seat)
        key = self._action_key(t, seat, action)
        if key in a["ev"]:
            return a["ev"][key]
        if key == "fold":
            return 0.0
        target = t.bet[seat] + t.stacks[seat] if key == "allin" else int(key.split()[1])
        return self._raise_ev(t, seat, target)

    def _assumption_points(self, t, seat, a):
        me = seat_label(seat)
        parts, any_reads = [], False
        for o in t.in_hand():
            if o == seat:
                continue
            reads = self._reads(t, o)
            any_reads = any_reads or bool(reads)
            said = "、".join(f"{STREETS[street]}{READ_WORDS[kind]}" for street, kind in reads)
            parts.append(f"{seat_label(o)}{RANGE_TEXT[t.pre.get(o)]}" + (f"，翻牌後{said}" if said else ""))
        points = [(CONDITIONAL, f"假設：{'；'.join(parts) or '沒有對手'}。")]
        if any_reads:
            points.append((CONDITIONAL, f"判斷對手牌力：{READ_RULE}。"))
        points.append((CONDITIONAL, f"在這些假設下，{me}的勝率約 {pct(a['equity'])}（模擬 {SIMS} 次，假設大家都打到攤牌；"
                                    f"{len(t.in_hand())} 人平分時是 {pct(a['fair'])}）。"))
        return points

    def _ev_text(self, ev):
        return "、".join(f"{action_text(k)} {v:+.0f}" for k, v in sorted(ev.items(), key=lambda kv: -kv[1]))

    def _decide(self, t, seat):
        """引擎的選擇（Agent 代下與問題檢查的標準）：在列出的假設下，期望值最高的選擇。"""
        a = self.analysis(t, seat)
        return a["best"], f"在這些假設下期望值最高，約 {a['ev'][a['best']]:+.0f}"

    def _bot(self, t, seat):
        """風格機器人：翻牌前看起手牌強度，翻牌後看勝率；各風格的門檻不同。"""
        style = STYLES[t.roles[seat]]
        name = f"{seat_label(seat)}（{style['name']}機器人）"
        rng = random.Random(f"{t.seed}:{len(t.actions)}:bot")
        hole = t.hole[seat]
        L = t.legal(seat)
        points = []
        if t.street == 0:
            top = cards.top_fraction(*hole)
            points.append((ESTIMATE, f"{name}的起手牌 {cards.show_all(hole)}，依陳氏公式約在所有起手牌的前 {pct(top)}。"))
            raised = t.current > t.bb
            open_at, play_at = (style["open"] * 0.4, style["play"] * 0.6) if raised else (style["open"], style["play"])
            target = self._raise_target(t, seat, 1.0) if top <= open_at else None
            if target:
                action = self._raise_action(t, seat, target)
            elif top <= play_at or L["check"]:
                action = "check" if L["check"] else "call"
            else:
                action = "fold"
            rule = (f"{style['name']}風格{'面對加注時' if raised else ''}：前 {pct(open_at)} 加注、"
                    f"前 {pct(play_at)} 跟注，其餘棄牌（能免費過牌就過牌）")
        else:
            a = self.analysis(t, seat, BOT_SIMS)
            points.append((CONDITIONAL, f"{name}的底牌 {cards.show_all(hole)}；在對手範圍的假設下，勝率約 {pct(a['equity'])}"
                                        f"（底池 {a['pot']}，要跟注 {a['to_call']}）。"))
            strong = min(0.95, min(0.7, 1.5 * a["fair"]) * style["raise_at"])
            target = self._raise_target(t, seat, 2 / 3)
            bluff = L["check"] and a["equity"] < strong and rng.random() < style["bluff"]
            if target and (a["equity"] >= strong or bluff):
                action = self._raise_action(t, seat, target if not bluff else self._raise_target(t, seat, 0.5))
            elif L["check"]:
                action = "check"
            elif a["equity"] >= a["pot_odds"] - style["call_margin"]:
                action = "call"
            else:
                action = "fold"
            rule = (f"{style['name']}風格：勝率 {pct(strong)} 以上加注，"
                    f"勝率高於底池賠率 {pct(a['pot_odds'])}{'減 ' + pct(style['call_margin']) if style['call_margin'] > 0 else ''}就跟注"
                    + (f"，沒人下注時有 {pct(style['bluff'])} 機會詐唬" if style["bluff"] else "") + (
                        "（這次是詐唬）" if bluff and target else ""))
        points.append((CONDITIONAL, f"{rule}；所以{name}選擇{action_text(action)}。"))
        return {"action": action, "points": points}

    # ---- Agent ----

    def agent_prompt(self, seat):
        return f"""你是德州撲克（無限注）AI 玩家，坐 {seat} 號位。用工具查證之後再行動。
規則：每人兩張底牌，加上五張公共牌，取最好的五張比大小；盲注 10/20。
行動的寫法：fold 棄牌、check 過牌、call 跟注、raise N（把自己這一輪的下注加到 N）、allin（All in，全下）。
建議流程：先呼叫 find_threats 看牌面與底池賠率；再用 list_candidates 看勝率與各選擇的期望值；
需要時用 check_move 檢查一個加注金額；最後呼叫 play_move。
硬性規定：行動必須合法（最小加注、籌碼上限）；可以免費過牌時不能棄牌。違反時會被退回。
勝率與期望值是在列出的假設下算的（條件式計算），reason 要說「在這些假設下」，不能說一定會贏。
play_move 的 reason 用繁體中文一句話說明，只能提到工具結果裡出現過的牌。"""

    def _position(self, t, seat):
        names = []
        if seat == t.button:
            names.append("按鈕位")
        if seat == t.sb_seat:
            names.append("小盲")
        if seat == t.bb_seat:
            names.append("大盲")
        return "、".join(names) or "一般位置"

    def board_text(self, t, actions):
        seat = t.to_act
        label = lambda s: "你" if s == seat else seat_label(s)
        L = t.legal(seat)
        lines = [f"第 {t.hand_no} 手牌，{STREETS[t.street]}。你坐 {seat} 號位（{self._position(t, seat)}）。",
                 f"你的底牌：{cards.show_all(t.hole[seat])}。公共牌：{cards.show_all(t.board) or '還沒發'}。",
                 f"底池 {t.pot()}；你要跟注 {L['to_call']}；你的籌碼 {t.stacks[seat]}。"]
        others = [f"{label(s)} {t.stacks[s]}" + ("（已棄牌）" if s in t.folded else "（All in）" if s in t.allin else "")
                  for s in t.seats if s != seat and s in t.hole]
        lines.append("其他人的籌碼：" + "、".join(others) + "。")
        hand_actions = [f"{label(s)}{action_text(a)}" for s, a, *_ in t.history]
        lines.append("這手牌到目前的行動：" + ("；".join(hand_actions) or "（只有盲注）") + "。")
        options = ["棄牌" if not L["check"] else None, "過牌" if L["check"] else f"跟注 {L['to_call']}",
                   f"加注到 {L['raise'][0]}–{L['raise'][1]}" if L["raise"] else None, "All in" if L["allin"] else None]
        lines.append("你可以：" + "、".join(o for o in options if o) + "。")
        return "\n".join(lines)

    def action_from(self, args):
        return self.normalize(args.get("action"))

    def _board_points(self, t, seat):
        me = seat_label(seat)
        hole = t.hole[seat]
        points = [(RULE, f"{me}的底牌是 {cards.show_all(hole)}；公共牌 {cards.show_all(t.board) or '還沒發'}（{STREETS[t.street]}）。")]
        if t.board:
            points.append((RULE, f"{me}目前的牌型是{cards.describe(cards.evaluate(hole + t.board))}。"))
            for kind, outs in cards.outs(hole, t.board).items():
                if outs:
                    points.append((RULE, f"{me}再來一張就能成{kind}的補牌有 {len(outs)} 張。"))
            values = [cards.value(c) for c in t.board]
            suits = [c[1] for c in t.board]
            if len(set(values)) < len(values):
                points.append((RULE, "公共牌有對子，對手可能有三條或葫蘆。"))
            if max(suits.count(x) for x in set(suits)) >= 3:
                points.append((RULE, "公共牌有三張以上同花色，對手可能有同花。"))
        else:
            points.append((ESTIMATE, f"{me}的起手牌依陳氏公式約在所有起手牌的前 {pct(cards.top_fraction(*hole))}。"))
        L = t.legal(seat)
        if L["to_call"]:
            need = L["to_call"] / (t.pot() + L["to_call"])
            points.append((RULE, f"底池 {t.pot()}，{me}要跟注 {L['to_call']}：底池賠率是勝率至少 {pct(need)} 跟注才不虧。"))
        else:
            points.append((RULE, f"底池 {t.pot()}，沒有人下注，{me}可以免費過牌。"))
        return points

    def _candidate_points(self, t, seat):
        a = self.analysis(t, seat)
        me = seat_label(seat)
        points = self._assumption_points(t, seat, a)
        points.append((CONDITIONAL, f"在這些假設下，{me}各選擇的期望值（平均每次比棄牌多賺或少虧的籌碼）：{self._ev_text(a['ev'])}。"))
        if any(k.startswith(("raise", "allin")) for k in a["ev"]):
            points.append((CONDITIONAL, f"{RAISE_RULE}。"))
        elif a["legal"]["raise"]:
            points.append((CONDITIONAL, f"{me}的勝率沒有高於平分，引擎不考慮加注（不詐唬）。"))
        action, why = self._decide(t, seat)
        points.append((CONDITIONAL, f"在這些假設下，引擎的選擇是{action_text(action)}（{why}）。"))
        return points

    def _check_points(self, t, seat, action):
        me = seat_label(seat)
        problem = self.rejection(t, action)
        if problem:
            return [(RULE, f"{me}不能{action_text(action)}：{problem}。")]
        a = self.analysis(t, seat)
        if action == "fold":
            return [(CONDITIONAL, f"{me}棄牌：期望值 0（放棄底池 {a['pot']}）。")]
        ev, best = self._ev_of(t, seat, action), a["ev"][a["best"]]
        note = "" if ev >= best else f"，比最好的選擇{action_text(a['best'])}少約 {best - ev:.0f}"
        return [(CONDITIONAL, f"在假設下，{me}{action_text(action)}的期望值約 {ev:+.0f}{note}。")]

    def step_text(self, action):
        return f"決定{action_text(action)}"

    def run_tool(self, state, name, args):
        seat = state.to_act
        if name == "find_threats":
            return "看牌面", self._board_points(state, seat)
        if name == "list_candidates":
            return "算勝率與期望值", self._candidate_points(state, seat)
        if name == "check_move":
            action = self.normalize(args.get("action"))
            if action is None or action == "deal":
                return None
            return f"檢查{action_text(action)}", self._check_points(state, seat, action)
        return None

    def rejection(self, state, action):
        if action is None:
            return "行動格式不對，請用 fold、check、call、allin 或 raise N"
        if action == "leave":
            return "AI 不能離場"
        if action == "deal" or state.hand_over:
            return "這手牌還沒結束，不能開下一手"
        if action == "fold" and state.legal()["check"]:
            return "可以免費過牌，不能棄牌"
        try:
            state.copy().apply(action)
        except ValueError as exc:
            return str(exc)
        return None

    def forced(self, state):
        if not state.hand_over and state.roles.get(state.to_act) in STYLES:
            return self._bot(state, state.to_act)
        return None

    def engine_move(self, state):
        seat = state.to_act
        action, why = self._decide(state, seat)
        a = self.analysis(state, seat)
        return {"action": action, "points": self._assumption_points(state, seat, a) + [
            (CONDITIONAL, f"各選擇的期望值：{self._ev_text(a['ev'])}。"),
            (CONDITIONAL, f"在這些假設下，{seat_label(seat)}選擇{action_text(action)}：{why}。")]}

    def overall_points(self, settings, actions, human_seat):
        """整體評論用的打法統計：打了幾手、主動入池（翻牌前跟注或加注）、翻牌前加注、攤牌、贏下的手數與籌碼變化。"""
        t = self.new_state(settings)
        hands = joined = raised = showdown = won = 0
        dealt = HUMAN in t.hole
        entered = opened = False
        for action in actions:
            if not t.hand_over and t.to_act == HUMAN and t.street == 0 and action != "leave":
                kind = self._action_key(t, HUMAN, action) if action != "fold" else "fold"
                entered = entered or kind not in ("fold", "check")
                opened = opened or kind not in ("fold", "check", "call")
            t = self.apply(t, action)
            if t.hand_over and dealt:  # 這手牌剛結束：記下玩家這手的情況
                hands += 1
                joined += entered
                raised += opened
                showdown += HUMAN in t.shown
                won += t.winnings.get(HUMAN, 0) > 0
                dealt = entered = opened = False
            if action == "deal":
                dealt = HUMAN in t.hole
        if not hands:
            return []
        now = t.stacks[HUMAN] + (0 if t.hand_over else t.total[HUMAN])  # 打到一半的那手牌，已經下的注還算你的
        diff = now - t.start_stack
        points = [(RULE, f"你打完了 {hands} 手牌：主動入池（翻牌前跟注或加注）{joined} 手（{pct(joined / hands)}），"
                         f"翻牌前加注 {raised} 手（{pct(raised / hands)}），打到攤牌 {showdown} 手，贏下 {won} 手。"),
                  (RULE, f"籌碼：起始 {t.start_stack}，現在 {now}（{'+' if diff >= 0 else ''}{diff}）。")]
        # 數字的好壞由程式判斷後寫進事實，語言模型不自己評價（它曾把 78% 的入池率說成「很穩定」）
        players = settings.get("players", 6)
        low, high = next((lo, hi) for n, lo, hi in VPIP_RANGES if players <= n)
        rate = joined / hands
        verdict = "偏鬆（玩太多手）" if rate > high else "偏緊（玩太少手）" if rate < low else "在常見範圍內"
        points.append((ESTIMATE, f"參考：{players} 人桌一般建議主動入池約 {pct(low)}–{pct(high)}（經驗值，不是這盤算出來的）；"
                                 f"你這盤 {pct(rate)}，{verdict}。"))
        if joined >= 3:
            share = raised / joined
            if share < 0.35:
                points.append((ESTIMATE, f"你入池的 {joined} 手裡只有 {raised} 手是加注，大多只跟注，打法偏被動（經驗上入池時加注比較容易贏下底池）。"))
            elif share > 0.7:
                points.append((ESTIMATE, f"你入池的 {joined} 手裡有 {raised} 手是加注，打法偏積極。"))
        return points

    def public_notes(self, state, notes):
        """這手牌還沒結束時，AI 的思路（含底牌與勝率）只顯示它做了什麼。"""
        result = []
        for note in notes:
            if note["index"] < state.hand_start or state.hand_over or state.left:  # 離場後牌局結束，可以看
                result.append(note)
            else:
                text = f"{seat_label(note['seat'])}{action_text(state.actions[note['index']])}。"
                result.append({**note, "facts": {"kind": "ai_move", "points": [{"evidence": RULE, "text": text}]},
                               "narration": None, "hidden": True})
        return result

    # ---- 測速 ----

    @property
    def benchmark_samples(self):
        """預設設定下，輪到 Agent（2 號位）在翻牌前、翻牌、轉牌行動的三個局面（其他人都跟注或過牌）。"""
        if self._benchmark is None:
            t = self.new_state(self.clean_settings({}))
            samples, actions = [], []
            while len(samples) < 3 and not t.hand_over:
                if t.to_act == 2 and t.street == len(samples):
                    samples.append(list(actions))
                action = "check" if t.legal()["check"] else "call"
                t = self.apply(t, action)
                actions.append(action)
            self._benchmark = samples
        return self._benchmark

    # ---- 復盤與提問（玩家的角度） ----

    def review(self, settings, actions, human_seat):
        t = self.new_state(settings)
        moments = []
        for i, action in enumerate(actions):
            if not t.hand_over and t.to_act == HUMAN and action != "leave":  # 離場是要不要繼續玩，不是牌局判斷
                moment = self._judge(t, i, action)
                if moment:
                    moments.append(moment)
            t = self.apply(t, action)
        return moments

    def _facing_aggression(self, t):
        return t.street > 0 and any(street == t.street and kind in ("bet", "raise") for _, _, street, kind in t.history)

    def _naive_call(self, t):
        """不看對手翻牌後動作時，玩家的勝率與跟注期望值（用來看出讀對手有沒有幫上忙）。"""
        opponents = [o for o in t.in_hand() if o != HUMAN]
        ranges = [r for r in (self._opponent_range(t, HUMAN, o, reads=False) for o in opponents) if r]
        rng = random.Random(f"{t.seed}:{len(t.actions)}:naive")
        eq = cards.equity_vs(t.hole[HUMAN], t.board, ranges, SIMS, rng)
        pot, to_call = t.pot(), t.legal(HUMAN)["to_call"]
        return eq, eq * (pot + to_call) - to_call

    def _judge(self, t, index, action):
        """玩家這一步：和引擎在同樣假設下最好的選擇比，少賺或多虧多少；也找出讀對對手、精準止損的棄牌。"""
        a = self.analysis(t, HUMAN)
        ev = dict(a["ev"])
        played = self._action_key(t, HUMAN, action)
        ev.setdefault(played, self._ev_of(t, HUMAN, action))
        best = a["best"]
        loss = ev[best] - ev[played]
        scale = a["pot"] + a["to_call"]
        minor_at = max(MINOR_LOSS, MINOR_SHARE * scale)
        if loss >= max(KEY_LOSS, KEY_SHARE * scale):
            level = "key"
        elif loss >= minor_at:
            level = "minor"
        elif loss >= max(SUGGEST_LOSS, SUGGEST_SHARE * scale):
            level = "suggest"
        else:
            level = None
        read = None  # 不看對手動作時的 (勝率, 跟注期望值)
        if a["to_call"] and self._facing_aggression(t) and played in ("fold", "call"):
            read = self._naive_call(t)
        raises = ("fold", "check", "call")
        if level is None:
            # 精準止損（使用者要求列為加分）：不看對手的動作會想跟，但對手的動作顯示牌可能比你大，棄牌才對
            if not (played == "fold" and read and read[1] >= 0 and ev["call"] <= -minor_at):
                return None
            level, title = "good", "精準止損：對手的動作顯示牌可能比你大"
        elif level == "suggest":
            if played in ("check", "call") and best not in raises:
                title = f"建議：可以{action_text(best)}，讓對手多付一點"
            elif best == "fold":
                title = "建議：這裡棄牌比較穩"
            elif played == "fold":
                title = f"建議：{action_text(best)}也可以考慮"
            else:
                title = f"建議：{action_text(best)}會更好一點"
        elif played == "fold" and not a["to_call"]:
            title = "可以免費過牌卻棄牌"
        elif played == "fold":
            title = "棄掉了有利的牌"
        elif played in ("check", "call") and best not in raises:
            title = "少賺了：牌力領先，可以加注讓對手多付" if a["equity"] >= a["fair"] else "錯過了加注的好機會"
        elif played == "call" and best == "fold":
            title = "沒有止損：對手的動作顯示牌可能比你大" if read and read[1] >= 0 else "跟注划不來"
        elif best not in raises:
            title = "加注金額可以更好"
        else:
            title = "All in 風險太高" if action == "allin" else "這次加注划不來"
        points = [(RULE, f"第 {t.hand_no} 手牌{STREETS[t.street]}：你的底牌 {cards.show_all(t.hole[HUMAN])}，"
                         f"公共牌 {cards.show_all(t.board) or '還沒發'}；底池 {a['pot']}，你要跟注 {a['to_call']}。")]
        points += self._assumption_points(t, HUMAN, a)
        if read:
            points.append((CONDITIONAL, f"如果不看對手翻牌後的動作，你的勝率約 {pct(read[0])}（跟注期望值約 {read[1]:+.0f}）；"
                                        f"把對手的動作算進去，勝率是 {pct(a['equity'])}（跟注期望值約 {ev['call']:+.0f}）。"))
        points.append((CONDITIONAL, f"在這些假設下，各選擇的期望值（平均每次比棄牌多賺或少虧的籌碼）：{self._ev_text(ev)}。"))
        if best not in raises or played not in raises:
            points.append((CONDITIONAL, f"{RAISE_RULE}。"))
        if level == "good":
            points.append((CONDITIONAL, f"你棄牌（0）比跟注（約 {ev['call']:+.0f}）好，避開了約 {-ev['call']:.0f} 的損失。"))
        elif level == "suggest":
            points.append((CONDITIONAL, f"差距很小，不算失誤：在這些假設下，{action_text(best)}約 {ev[best]:+.0f}，"
                                        f"你的選擇（{action_text(action)}）約 {ev[played]:+.0f}，只多約 {loss:.0f}，是比較推薦的打法。"))
        else:
            points.append((CONDITIONAL, f"在這些假設下，最好的選擇是{action_text(best)}（約 {ev[best]:+.0f}），"
                                        f"你的選擇（{action_text(action)}）約 {ev[played]:+.0f}，少了約 {loss:.0f}。"))
        points.append((RULE, "提醒：單手輸贏不等於決策好壞；這裡只用你當時看得到的資訊，事後亮出的底牌不算。"))
        return {"index": index, "type": "ev", "level": level, "title": title, "played": action, "best": best,
                "loss": round(loss, 1), "points": [{"evidence": e, "text": text} for e, text in points]}

    def question_focus(self, question):
        q = question.upper()
        focus = [action for word, action in QUESTION_WORDS if word in q]
        m = re.search(r"(?:加注|RAISE)\s*(?:到|TO)?\s*(\d+)", q)
        if m:
            focus.append(f"raise {int(m.group(1))}")
        return list(dict.fromkeys(focus))

    def explain_position(self, state, focus, next_action):
        t = state
        if t.hand_over:
            return [{"evidence": RULE, "text": f"這時第 {t.hand_no} 手牌已經結束：{t.hand_result}"}]
        if HUMAN not in t.in_hand():
            points = [(RULE, f"這時你已經不在這手牌裡（底池 {t.pot()}，公共牌 {cards.show_all(t.board) or '還沒發'}）。")]
        else:
            points = self._board_points(t, HUMAN) + self._candidate_points(t, HUMAN)
            if t.to_act == HUMAN:
                for action in focus[:3]:
                    points += self._check_points(t, HUMAN, action)
        if next_action:
            actor = HUMAN if t.to_act is None else t.to_act
            points.append((RULE, f"紀錄中接下來是{seat_label(actor)}{action_text(next_action)}。"))
        return [{"evidence": ev, "text": text} for ev, text in points]

    def explain_action(self, settings, actions, n):
        t = self.replay(settings, actions[:n - 1])
        action = actions[n - 1]
        if action == "leave":
            return [{"evidence": RULE, "text": f"第 {n} 步是你離場：{self.apply(t, action).result()[1]}。"}]
        if t.hand_over:
            return [{"evidence": RULE, "text": f"第 {n} 步是開始第 {t.hand_no + 1} 手牌。"}]
        actor = t.to_act
        points = [(RULE, f"第 {n} 步是第 {t.hand_no} 手牌{STREETS[t.street]}，{seat_label(actor)}{action_text(action)}"
                         f"（當時底池 {t.pot()}，要跟注 {t.legal(actor)['to_call']}）。")]
        if actor == HUMAN:
            points += self._check_points(t, HUMAN, action)
        return [{"evidence": ev, "text": text} for ev, text in points]


MODULE = HoldemModule()
