"""德州撲克（無限注）規則：一場牌局包含多手牌，純程式。

行動記法（存在 Game.actions）：
- "fold" 棄牌、"check" 過牌、"call" 跟注、"allin" 全下（畫面上顯示 All in）
- "raise N"：把自己這一輪的下注加到 N（下注與加注都用這個；N 是這一輪的總額，不是增加的量）
- "deal"：這手牌結束後開下一手（輪到玩家按）
- "leave"：玩家離場，牌局結束；一手牌進行中離場等於先棄牌（使用者要求，2026-10-11）
每手牌用 secret 的種子加上第幾手洗牌，所以從行動序列可以完整重建，牌序只存在伺服器。
"""
import random

from . import cards

HUMAN = 1  # 玩家固定坐 1 號位；位置靠按鈕輪轉，坐哪裡都一樣
STREETS = ["翻牌前", "翻牌", "轉牌", "河牌"]
ACTION_WORDS = {"fold": "棄牌", "check": "過牌", "call": "跟注", "allin": "All in", "deal": "下一手", "leave": "離場"}  # 使用者要求全下顯示成 All in


def seat_label(seat):
    """事實裡的座位名稱：主詞要明確，玩家寫成「玩家（1 號位）」。"""
    return "玩家（1 號位）" if seat == HUMAN else f"{seat} 號位"


def action_text(action):
    if action.startswith("raise "):
        return f"加注到 {action.split()[1]}"
    return ACTION_WORDS.get(action, action)


class Table:
    """牌局狀態。apply 產生新的狀態，不就地修改（copy 後再改）。"""

    def __init__(self, players, stack, sb, bb, seed):
        self.players, self.sb, self.bb, self.seed = players, sb, bb, seed
        self.seats = list(range(1, players + 1))
        self.stacks = {s: stack for s in self.seats}  # 桌上還沒投入底池的籌碼
        self.start_stack = stack
        self.left = False  # 玩家離場了，牌局結束
        self.hand_no = 0
        self.button = random.Random(f"{seed}:button").choice(self.seats)
        self.actions = []
        self.start_hand(first=True)

    def copy(self):
        t = Table.__new__(Table)
        t.__dict__.update(self.__dict__)
        for name in ("stacks", "bet", "total", "acted", "hole", "pre"):
            setattr(t, name, dict(getattr(self, name)))
        for name in ("actions", "board", "deck", "history"):
            setattr(t, name, list(getattr(self, name)))
        for name in ("folded", "allin", "shown"):
            setattr(t, name, set(getattr(self, name)))
        t.winnings = dict(self.winnings)
        return t

    # ---- 開一手牌 ----

    def start_hand(self, first=False):
        self.hand_no += 1
        self.hand_start = len(self.actions)  # 這手牌第一個行動在 actions 中的位置
        live = [s for s in self.seats if self.stacks[s] > 0]
        if not first:
            self.button = self.next_seat(self.button, live)
        elif self.button not in live:
            self.button = self.next_seat(self.button, live)
        deck = cards.DECK[:]
        random.Random(f"{self.seed}:{self.hand_no}").shuffle(deck)
        order = self.order_from(self.button, live)[1:] + [self.button]  # 從按鈕左邊開始發
        self.hole = {s: [deck[i], deck[i + len(order)]] for i, s in enumerate(order)}
        self.deck = deck[2 * len(order):]
        self.board, self.history = [], []
        self.last_seat = None  # 最後一個行動的座位（網頁動畫用）
        self.street = 0
        self.folded, self.allin, self.shown = {s for s in self.seats if s not in live}, set(), set()
        self.bet = {s: 0 for s in self.seats}
        self.total = {s: 0 for s in self.seats}
        self.pre = {}  # 每位玩家翻牌前最積極的行動：raise / call / check（推估範圍用）
        self.winnings, self.hand_over, self.hand_result = {}, False, ""
        if len(live) == 2:  # 單挑：按鈕是小盲，翻牌前先行動
            sb_seat, bb_seat = self.button, self.next_seat(self.button, live)
        else:
            sb_seat = self.next_seat(self.button, live)
            bb_seat = self.next_seat(sb_seat, live)
        self.sb_seat, self.bb_seat = sb_seat, bb_seat
        self._put(sb_seat, min(self.sb, self.stacks[sb_seat]))
        self._put(bb_seat, min(self.bb, self.stacks[bb_seat]))
        self.current = max(self.bet.values())
        self.min_raise = self.bb
        self.full_level = self.current  # 最後一次完整加注後的下注額；不足額的全下不重新開放加注
        self.acted = {}  # 這一輪已行動的玩家 → 行動時的 full_level
        self.to_act = self._next_to_act(bb_seat)
        if self.to_act is None:
            self._finish_round()

    # ---- 座位 ----

    def order_from(self, seat, among):
        """從 seat 開始順時針排列 among 中的座位。"""
        ordered = sorted(among)
        i = next((k for k, s in enumerate(ordered) if s >= seat), 0)
        return ordered[i:] + ordered[:i]

    def next_seat(self, seat, among):
        later = [s for s in sorted(among) if s > seat]
        return later[0] if later else min(among)

    def in_hand(self):
        return [s for s in self.seats if s not in self.folded]

    def can_act(self):
        return [s for s in self.in_hand() if s not in self.allin]

    def _needs_action(self, s):
        return s not in self.acted or self.bet[s] < self.current

    def _next_to_act(self, after):
        for s in self.order_from(after + 1 if after < self.players else 1, self.can_act()):
            if self._needs_action(s):
                return s
        return None

    # ---- 行動 ----

    def _put(self, seat, amount):
        self.stacks[seat] -= amount
        self.bet[seat] += amount
        self.total[seat] += amount
        if self.stacks[seat] == 0:
            self.allin.add(seat)

    def pot(self):
        return sum(self.total.values())

    def legal(self, seat=None):
        """輪到的玩家可以做什麼：{"fold", "check", "call"（要補的籌碼）, "raise": (最少, 最多) 或 None, "allin"}。"""
        s = seat or self.to_act
        to_call = self.current - self.bet[s]
        stack = self.stacks[s]
        others = [o for o in self.can_act() if o != s]
        can_raise = stack > to_call and bool(others) and self.acted.get(s, -1) < self.full_level
        max_total = self.bet[s] + stack
        min_total = self.current + self.min_raise if self.current else self.bb
        raise_range = None
        if can_raise and max_total > self.current:
            raise_range = (min(min_total, max_total), max_total)
        return {"to_call": min(to_call, stack), "check": to_call == 0, "call": to_call > 0,
                "raise": raise_range, "allin": stack > 0 and (to_call >= stack or raise_range is not None)}

    def apply(self, action):
        if self.left:
            raise ValueError("你已經離場，牌局結束了")
        if action == "leave":
            self._leave()
            return
        if self.hand_over:
            if action != "deal":
                raise ValueError("這手牌已經結束，請開下一手")
            self.actions.append(action)
            self.start_hand()
            return
        s = self.to_act
        legal = self.legal()
        to_call = self.current - self.bet[s]
        kind = action  # 翻牌前的積極程度：check / call / raise
        if action == "fold":
            self.folded.add(s)
        elif action == "check":
            if not legal["check"]:
                raise ValueError(f"要跟注 {legal['to_call']} 或棄牌，不能過牌")
        elif action == "call":
            if not legal["call"]:
                raise ValueError("沒有人下注，請過牌")
            self._put(s, min(to_call, self.stacks[s]))
        elif action == "allin" or action.startswith("raise "):
            if action == "allin":
                if not legal["allin"]:
                    raise ValueError("現在不能 All in（已經不能再加注），請跟注或棄牌")
                target = self.bet[s] + self.stacks[s]
            else:
                try:
                    target = int(action.split()[1])
                except (IndexError, ValueError):
                    raise ValueError(f"加注金額不對：{action}")
                if legal["raise"] is None:
                    raise ValueError("現在不能加注")
                low, high = legal["raise"]
                if not low <= target <= high:
                    raise ValueError(f"加注到的金額必須在 {low} 到 {high} 之間")
            if target <= self.current:  # 籌碼不夠跟注的全下
                self._put(s, target - self.bet[s])
                kind = "call"
            else:
                kind = "bet" if self.current == 0 else "raise"  # 這一輪第一個下注叫 bet，之後的叫 raise
                increase = target - self.current
                self._put(s, target - self.bet[s])
                if increase >= self.min_raise:
                    self.min_raise = increase
                    self.full_level = target
                self.current = target
        else:
            raise ValueError(f"看不懂的行動：{action}")
        rank = {"check": 0, "call": 1, "raise": 2}
        if self.street == 0 and kind in rank and rank[kind] >= rank.get(self.pre.get(s), -1):
            self.pre[s] = kind
        self.acted[s] = self.full_level
        self.history.append((s, action, self.street, kind))  # kind：fold / check / call / bet / raise，推估對手範圍用
        self.last_seat = s
        self.actions.append(action)
        if len(self.in_hand()) == 1:
            self._award({self.in_hand()[0]: self.pot()}, by_fold=True)
            return
        self.to_act = self._next_to_act(s)
        if self.to_act is None:
            self._finish_round()

    def _leave(self):
        """玩家離場：一手牌之間直接結束；一手牌進行中要輪到玩家才能離場，先棄牌（已經下的注留在底池）。"""
        if not self.hand_over:
            if self.to_act != HUMAN:
                raise ValueError("輪到你的時候才能離場")
            self.folded.add(HUMAN)
            self.history.append((HUMAN, "leave", self.street, "fold"))
            self.last_seat = HUMAN
        self.left = True
        self.actions.append("leave")

    def _finish_round(self):
        """這一輪下注結束：發下一條街，或攤牌。只剩一人能行動時直接把牌發完。"""
        while True:
            for s in self.seats:
                self.bet[s] = 0
            self.current, self.min_raise, self.full_level, self.acted = 0, self.bb, 0, {}
            if self.street == 3:
                self._showdown()
                return
            self.street += 1
            take = 3 if self.street == 1 else 1
            self.board += self.deck[:take]
            self.deck = self.deck[take:]
            if len(self.can_act()) >= 2:
                self.to_act = self._next_to_act(self.button)
                return

    # ---- 結算 ----

    def side_pots(self):
        """主池與邊池：[(金額, 有資格贏的座位)]，依投入的籌碼分層。"""
        levels = sorted({v for v in self.total.values() if v > 0})
        pots, prev = [], 0
        for level in levels:
            contributors = [s for s in self.seats if self.total[s] >= level]
            amount = (level - prev) * len(contributors)
            eligible = [s for s in contributors if s not in self.folded]
            if pots and (not eligible or pots[-1][1] == eligible):
                pots[-1] = (pots[-1][0] + amount, pots[-1][1])
            else:
                pots.append((amount, eligible))
            prev = level
        return pots

    def _showdown(self):
        self.shown = set(self.in_hand())
        ranks = {s: cards.evaluate(self.hole[s] + self.board) for s in self.shown}
        won = {}
        for amount, eligible in self.side_pots():
            best = max(ranks[s] for s in eligible)
            winners = self.order_from(self.button + 1, [s for s in eligible if ranks[s] == best])
            share, extra = divmod(amount, len(winners))
            for i, s in enumerate(winners):  # 除不盡的籌碼從按鈕左邊開始一人一個
                won[s] = won.get(s, 0) + share + (1 if i < extra else 0)
        self._award(won, ranks=ranks)

    def _award(self, won, by_fold=False, ranks=None):
        for s, amount in won.items():
            self.stacks[s] += amount
        self.winnings = won
        self.hand_over, self.to_act = True, None
        if by_fold:
            (s, amount), = won.items()
            self.hand_result = f"其他人都棄牌，{seat_label(s)}贏得 {amount}。"
        else:
            parts = [f"{seat_label(s)}以{cards.describe(ranks[s])} 贏得 {won[s]}" for s in sorted(won, key=lambda x: -won[x])]
            self.hand_result = "攤牌：" + "；".join(parts) + "。"

    # ---- 牌局結束 ----

    def result(self):
        """玩家離場、輸光或其他人都輸光時結束：(勝方座位或 None（不輸不贏）, 說明)；還沒結束回傳 None。"""
        if self.left:
            chips = self.stacks[HUMAN]
            diff = chips - self.start_stack
            text = f"你離場，帶走 {chips} 籌碼（起始 {self.start_stack}，" + (f"贏 {diff}）" if diff >= 0 else f"輸 {-diff}）")
            if diff > 0:
                return HUMAN, text
            if diff == 0:
                return None, text
            return max((s for s in self.seats if s != HUMAN), key=lambda s: self.stacks[s]), text
        if not self.hand_over:
            return None
        if self.stacks[HUMAN] == 0:
            leader = max((s for s in self.seats if s != HUMAN), key=lambda s: self.stacks[s])
            return leader, f"你的籌碼輸光了，{leader} 號位籌碼最多（{self.stacks[leader]}）"
        if all(self.stacks[s] == 0 for s in self.seats if s != HUMAN):
            return HUMAN, "其他玩家的籌碼都輸光了，你贏得整場牌局"
        return None
