"""中國象棋的遊戲模組：rules.py（規則，事實屬於規則確認）＋Pikafish（分析：找到的殺棋屬於求解確認，其他屬於引擎估計）。

行動記法：ICCS（例如 "h2e2"）或 "resign"（認輸）。說明文字寫成「炮二平五（h2e2）」，ICCS 那段在網頁上可以點。
座位 1＝紅方（先走）、2＝黑方。
"""
import re
import threading
from collections import OrderedDict

from arena.base import ESTIMATE, RULE, SOLVED, GameModule

from . import rules
from .pikafish import ENGINE, PikafishUnavailable, value
from .rules import BLACK, COLOR_NAMES, PIECE_NAMES, RED, RESIGN

ACTION_RE = re.compile(r"^[a-i][0-9][a-i][0-9]$")
# 中文記法，例如 炮二平五、馬８進７、前車退一；問題裡提到時要認得
CHINESE_RE = r"(?:[前中後][車俥馬傌炮砲包相象仕士帥將兵卒]|[車俥馬傌炮砲包相象仕士帥將兵卒][一二三四五六七八九１-９1-9])[進进退平][一二三四五六七八九１-９1-9]"
# 工具、引擎代下與復盤都用同一個深度：Pikafish 單執行緒、固定深度的結果每次相同，
# 引擎代下選的那一步在復盤時就一定是引擎的首選，不會被挑出問題（第二台開發機約 0.1–0.2 秒一次）
DEPTH = 12
CANDIDATES = 5
LOSS_KEY, LOSS_MINOR, LOSS_SUGGEST = 300, 150, 50  # 復盤的分級（百分之一個兵）；50 以下在搜尋誤差內
DECIDED = 1000  # 落後 10 個兵以上算勝負大致已定
VALUES = {"K": 100, "R": 9, "C": 4.5, "N": 4, "B": 2, "A": 2, "P": 1}


def pawns(cp):
    return f"{abs(cp) / 100:.1f} 個兵"


def lead_text(cp):
    return f"約領先 {pawns(cp)}" if cp >= 0 else f"約落後 {pawns(cp)}"


def piece_text(board, sq):
    """紅方炮（h2）"""
    p = board[sq]
    return f"{COLOR_NAMES[rules.color_of(p)]}{PIECE_NAMES[p]}（{rules.square(sq % 9, sq // 9)}）"


class XiangqiModule(GameModule):
    key = "xiangqi"
    name = "象棋"
    seat_names = COLOR_NAMES
    coord_pattern = r"(?<![A-Za-z0-9])([a-i][0-9](?:[a-i][0-9])?)(?![A-Za-z0-9])"
    default_steps = 4
    board_script = "xiangqi/board.js"
    icon = "xiangqi/icon.svg"
    tagline = "紅先黑後，先點棋子再點目的地。AI 用 Pikafish 查證每一步。"
    numbered = False  # 棋子會移動，手數標在格子上沒有意義
    rules = [
        {"title": "目標", "items": ["紅黑兩方輪流走一步，先把對方的帥（將）將死的一方獲勝。"]},
        {"title": "怎麼走", "items": ["紅方先走。先點自己的一顆棋子，棋盤會標出它能走的位置，再點要去的位置。",
                                   "帥（將）、仕（士）只能在九宮裡走；相（象）走田字、不能過河，田字中間有子擋住就不能走（塞象眼）。",
                                   "馬走日字，緊貼著馬的那一格有子擋住就不能走（蹩馬腿）。車直走、不限格數。",
                                   "炮不吃子時和車一樣走；吃子時中間要剛好隔一顆棋子（炮架）。",
                                   "兵（卒）只能往前走一格；過河以後也可以往左右走一格，但不能後退。"]},
        {"title": "將軍與勝負", "items": ["帥和將不能在同一條直線上直接面對面（中間沒有棋子）。",
                                     "被將軍時一定要應將；走完會讓自己被將軍的步不能走。",
                                     "被將死，或輪到自己卻沒有任何一步可以走（困斃），都算輸。也可以按「認輸」結束。"]},
        {"title": "和局與長將", "items": ["同一個局面出現第三次時：如果只有一方一直在將軍，這一方判負（長將判負）；其他情況判和。",
                                     "長捉（一直追捉對方的子）的正式規則很複雜，這裡還沒有判定，一律算和。",
                                     "雙方連續 60 回合都沒有吃子，判和。"]},
        {"title": "記法與座標", "items": ["說明用中文記法，例如「炮二平五」：紅方的直線由紅方的右邊數起用一到九，黑方由黑方的右邊數起用１到９；進是往前、退是往後、平是橫走。",
                                     "括號裡的 h2e2 是座標記法：直的是 a 到 i（紅方由左到右），橫的是 0 到 9（紅方底線是 0）；可以點，棋盤上會標出起點和終點。"]},
    ]
    benchmark_samples = [  # 開局、中炮對屏風馬、中局常見的對攻
        ["h2e2"],
        ["h2e2", "h9g7", "h0g2", "i9h9", "i0h0", "b9c7"],
        ["h2e2", "h7e7", "b0c2", "b9c7", "h0g2", "h9g7", "i0h0", "i9h9"],
    ]
    tools = [
        {"type": "function", "function": {
            "name": "find_threats",
            "description": "查將軍、一步殺（雙方），以及被攻擊又沒有保護、或被價值較低的棋子攻擊的棋子。",
            "parameters": {"type": "object", "properties": {}}}},
        {"type": "function", "function": {
            "name": "list_candidates",
            "description": f"用 Pikafish 列出最好的前 {CANDIDATES} 步，附局面評估。",
            "parameters": {"type": "object", "properties": {}}}},
        {"type": "function", "function": {
            "name": "check_move",
            "description": "檢查某一步：合不合法、吃到什麼、有沒有將軍、會不會讓對方一步殺，以及 Pikafish 估計的局面變化。",
            "parameters": {"type": "object", "properties": {
                "move": {"type": "string", "description": "ICCS 走法，例如 h2e2"}}, "required": ["move"]}}},
        {"type": "function", "function": {
            "name": "play_move",
            "description": "決定要走的一步。",
            "parameters": {"type": "object", "properties": {
                "move": {"type": "string", "description": "ICCS 走法，例如 h2e2"},
                "reason": {"type": "string", "description": "一句話說明為什麼這樣走"}},
                "required": ["move", "reason"]}}},
    ]

    def __init__(self):
        self.analyzer = ENGINE  # 測試時換成假的分析器
        self._cache = OrderedDict()  # 同一個局面在一次 Agent 決策中常被查好幾次
        self._lock = threading.Lock()

    # ---- 規則 ----

    def new_state(self, settings):
        return rules.XiangqiState()

    def normalize(self, text):
        t = str(text or "").strip()
        if t.lower() in (RESIGN, "認輸"):
            return RESIGN
        return t.lower() if ACTION_RE.match(t.lower()) else None

    def apply(self, state, action):
        return rules.play(state, action)

    def to_move(self, state):
        return state.to_move

    def outcome(self, state):
        if state.result is None:
            return None
        return state.result[0], state.result[0] is None

    def frame(self, state, seat):
        return {"board": "".join(state.board), "last": state.actions[-1] if state.actions else None,
                "to_move": state.to_move, "check": not state.over and rules.in_check(state.board, state.to_move),
                "legal": rules.legal_moves(state), "result": state.result[1] if state.result else None}

    def move_text(self, state, action):
        """炮二平五（h2e2）；state 是走之前的局面。"""
        return f"{rules.chinese(state, action)}（{action}）"

    @staticmethod
    def wins_in_one(state):
        """輪到的一方一步就獲勝的走法（將死或困斃對方）。"""
        wins = []
        for m in rules.legal_moves(state):
            result = rules.play(state, m).result
            if result and result[0] == state.to_move:
                wins.append(m)
        return wins

    # ---- 分析 ----

    def _analysis(self, state, multipv=1, searchmoves=None):
        actions = [a for a in state.actions if a != RESIGN]
        key = (tuple(actions), multipv, tuple(searchmoves or ()))
        with self._lock:
            if key not in self._cache:
                self._cache[key] = self.analyzer.analyze(actions, DEPTH, multipv, searchmoves)
                while len(self._cache) > 64:
                    self._cache.popitem(last=False)
            return self._cache[key]

    def _eval_point(self, color, entry, subject):
        """一個 Pikafish 結果寫成事實。subject 例如「紅方走炮二平五（h2e2）後」。殺棋屬於求解確認。"""
        name = COLOR_NAMES[color]
        if entry["mate"] is not None:
            if entry["mate"] > 0:
                return SOLVED, f"在搜尋範圍內已確認（Pikafish {DEPTH} 層）：{subject}{name}可以在 {entry['mate']} 回合內將死對方。"
            return SOLVED, f"在搜尋範圍內已確認（Pikafish {DEPTH} 層）：{subject}{name}會在 {-entry['mate']} 回合內被將死。"
        return ESTIMATE, f"依引擎估計（Pikafish {DEPTH} 層），{subject}{name}{lead_text(entry['score'])}。"

    def _attacks(self, board, sq):
        """攻擊 sq 那顆棋子的對方棋子（格子編號，價值低的在前）；只算走完不會讓自己被將軍的。"""
        enemy = rules.other(rules.color_of(board[sq]))
        found = [frm for frm, to in rules.pseudo_moves(board, enemy)
                 if to == sq and not rules.in_check(rules._move_board(board, frm, to), enemy)]
        return sorted(found, key=lambda s: VALUES[board[s].upper()])

    def _defended(self, board, sq):
        """把 sq 的棋子換成對方的，看自己能不能吃回來。"""
        flipped = list(board)
        flipped[sq] = board[sq].swapcase()
        return bool(self._attacks(flipped, sq))

    def _threats(self, state):
        color, opp = state.to_move, rules.other(state.to_move)
        me, them = COLOR_NAMES[color], COLOR_NAMES[opp]
        board = state.board
        points = []
        if rules.in_check(board, color):
            king = rules.find_king(board, color)
            checkers = "、".join(piece_text(board, s) for s in self._attacks(board, king))
            points.append((RULE, f"{me}正被將軍（{checkers}），這一步一定要應將。"))
        wins = self.wins_in_one(state)
        if wins:
            points.append((RULE, f"{me}一步就能獲勝：{'、'.join(self.move_text(state, m) for m in wins[:3])}。"))
        elif not rules.in_check(board, color):
            # 假設輪到對方（自己這步不管），對方有沒有一步殺：這就是自己這步要防的
            flipped = state.copy()
            flipped.to_move = opp
            threats = self.wins_in_one(flipped)
            if threats:
                points.append((RULE, f"如果{me}不處理，{them}下一步走 {'、'.join(self.move_text(flipped, m) for m in threats[:3])} 就會獲勝。"))
        for owner in (color, opp):
            found = 0
            for sq, p in sorted(enumerate(board), key=lambda x: -VALUES[x[1].upper()] if x[1] != "." else 0):
                if p == "." or p.upper() == "K" or rules.color_of(p) != owner or found >= 3:
                    continue
                attackers = self._attacks(board, sq)
                if not attackers:
                    continue
                cheaper = VALUES[board[attackers[0]].upper()] < VALUES[p.upper()]
                if self._defended(board, sq) and not cheaper:
                    continue
                why = "沒有保護" if not self._defended(board, sq) else "攻擊它的棋子價值比較低"
                who = "對方下一步可以吃掉" if owner == color else f"{me}現在可以吃掉"
                points.append((RULE, f"{piece_text(board, sq)}受到{piece_text(board, attackers[0])}攻擊，{why}（{who}）。"))
                found += 1
        if not points:
            points.append((RULE, "雙方都沒有被將軍、一步殺，或被攻擊又沒有保護的棋子。"))
        return points

    def _candidates(self, state):
        color = state.to_move
        lines = self._analysis(state, CANDIDATES)
        items = []
        for i, e in enumerate(lines, 1):
            how = (f"{e['mate']} 回合內將死對方" if e["mate"] and e["mate"] > 0 else
                   f"會在 {-e['mate']} 回合內被將死" if e["mate"] else lead_text(e["score"]))
            items.append(f"{i}. {self.move_text(state, e['move'])}（{how}）")
        evidence = SOLVED if lines and lines[0]["mate"] is not None else ESTIMATE
        lead = "在搜尋範圍內已確認" if evidence == SOLVED else "依引擎估計"
        return [(evidence, f"{lead}（Pikafish {DEPTH} 層），{COLOR_NAMES[color]}的候選依好壞排序：{'；'.join(items)}。")]

    def _rule_facts(self, state, action):
        """走 action 的規則事實：合不合法、吃子、將軍、會不會讓對方一步殺。"""
        me, them = COLOR_NAMES[state.to_move], COLOR_NAMES[rules.other(state.to_move)]
        try:
            after = rules.play(state, action)
        except ValueError as exc:
            return None, [(RULE, f"{me}不能走 {action}：{exc}。")]
        if action == RESIGN:
            return after, [(RULE, f"{me}認輸。")]
        name = self.move_text(state, action)
        (ff, fr), (tf, tr) = rules.parse_move(action)
        target = state.at(tf, tr)
        text = f"{me}走{name}" + (f"，吃掉{them}的{PIECE_NAMES[target]}" if target != "." else "")
        if after.result:
            return after, [(RULE, f"{text}，對局結束：{after.result[1]}。")]
        if after.checks[-1]:
            text += "，將軍"
        points = [(RULE, text + "。")]
        replies = self.wins_in_one(after)
        if replies:
            points.append((RULE, f"{me}走{name}之後，{them}走 {self.move_text(after, replies[0])} 就會獲勝。"))
        return after, points

    def _check(self, state, action):
        after, points = self._rule_facts(state, action)
        if after is None or after.over:
            return points
        color = state.to_move
        try:
            best = self._analysis(state)[0]
            mine = self._analysis(state, searchmoves=[action])[0]
            points.append(self._eval_point(color, mine, f"{COLOR_NAMES[color]}走{self.move_text(state, action)}後，"))
            if best["move"] != action:
                points.append((ESTIMATE, f"Pikafish 的首選是 {self.move_text(state, best['move'])}。"))
        except (PikafishUnavailable, IndexError) as exc:
            points.append((ESTIMATE, f"Pikafish 無法使用，沒有局面評估（{exc}）。"))
        return points

    # ---- Agent ----

    def agent_prompt(self, seat):
        return f"""你是象棋 AI 棋手，執{COLOR_NAMES[seat]}。用工具查證之後再走棋。
座標用 ICCS：直線 a–i（紅方由左到右），橫線 0–9（紅方底線是 0、黑方底線是 9）；一步寫成起點加終點，例如 h2e2。
規則：將死或困斃對方獲勝；帥將不能照面；同一局面出現第三次時，只有一方一直將軍的話這一方判負（長將判負）。
建議流程：先呼叫 find_threats 看有沒有將軍、一步殺或被攻擊的棋子；再用 list_candidates 看 Pikafish 推薦的走法；
需要時用 check_move 檢查一兩步；最後呼叫 play_move。
硬性規定：只能走合法的步；能一步獲勝時一定要走；不能走讓對方下一步就獲勝的步（除非每一步都會這樣）；不能認輸。違反時會被退回。
play_move 的 reason 用繁體中文一句話說明，只能提到工具結果裡出現過的座標。"""

    def board_text(self, state, actions):
        lines = []
        for color in (RED, BLACK):
            pieces = [f"{PIECE_NAMES[p]} {rules.square(sq % 9, sq // 9)}" for sq, p in enumerate(state.board)
                      if p != "." and rules.color_of(p) == color]
            lines.append(f"{COLOR_NAMES[color]}（{len(pieces)} 子）：{'、'.join(pieces)}")
        if actions:
            lines.append(f"最近幾步（ICCS）：{'、'.join(actions[-6:])}")
        if rules.in_check(state.board, state.to_move):
            lines.append("你正被將軍。")
        lines.append(f"現在輪到你（{COLOR_NAMES[state.to_move]}）走第 {len(actions) + 1} 步。")
        return "\n".join(lines)

    def action_from(self, args):
        return self.normalize(args.get("move"))

    def step_text(self, action):
        return f"走 {action}"

    def run_tool(self, state, name, args):
        try:
            if name == "find_threats":
                return "查將軍與威脅", self._threats(state)
            if name == "list_candidates":
                return "看 Pikafish 候選", self._candidates(state)
            if name == "check_move":
                action = self.normalize(args.get("move"))
                if action is None or action == RESIGN:
                    return None
                return f"檢查 {action}", self._check(state, action)
        except PikafishUnavailable as exc:
            return name, [(ESTIMATE, f"Pikafish 無法使用（{exc}），這個工具沒有結果。")]
        return None

    def rejection(self, state, action):
        if action is None:
            return "走法格式不對，請用 ICCS 座標（例如 h2e2）"
        if action == RESIGN:
            return "Agent 不能認輸，請走一步"
        try:
            after = rules.play(state, action)
        except ValueError as exc:
            return str(exc)
        me = state.to_move
        if after.result and after.result[0] == rules.other(me):
            return f"走 {action} 會讓{COLOR_NAMES[me]}輸掉：{after.result[1]}"
        wins = self.wins_in_one(state)
        if wins and action not in wins:
            return f"{self.move_text(state, wins[0])} 一步就能獲勝，必須走"
        if not after.over and self.wins_in_one(after):
            safe = next((m for m in rules.legal_moves(state) if m != action and not self._loses_at_once(state, m)), None)
            if safe:
                return f"走 {action} 之後對方一步就能獲勝；改走例如 {self.move_text(state, safe)} 可以避免"
        return None

    def _loses_at_once(self, state, action):
        after = rules.play(state, action)
        if after.result:
            return after.result[0] == rules.other(state.to_move)
        return bool(self.wins_in_one(after))

    def forced(self, state):
        legal = rules.legal_moves(state)
        if len(legal) == 1:
            return {"action": legal[0], "points": [
                (RULE, f"{COLOR_NAMES[state.to_move]}只有{self.move_text(state, legal[0])}這一步合法。")]}
        return None

    def engine_move(self, state):
        try:
            for e in self._analysis(state):
                if rules.is_legal(state, e["move"]):
                    return {"action": e["move"], "points": [self._eval_point(
                        state.to_move, e, f"Pikafish 的首選是{self.move_text(state, e['move'])}；走完後")]}
        except PikafishUnavailable as exc:
            return self._simple_move(state, str(exc))
        return self._simple_move(state, "Pikafish 沒有給出合法的走法")

    def _simple_move(self, state, why):
        """Pikafish 無法使用時的簡單選步：一步殺 → 不會馬上輸的步裡吃最大的子 → 第一個合法步。品質很低，要標明。"""
        note = (ESTIMATE, f"Pikafish 無法使用（{why}），這一步只用簡單規則選擇，品質很低。")
        wins = self.wins_in_one(state)
        if wins:
            return {"action": wins[0], "points": [note]}
        legal = rules.legal_moves(state)
        safe = [m for m in legal if not self._loses_at_once(state, m)] or legal

        def gain(m):
            target = state.at(*rules.parse_square(m[2:]))
            return VALUES[target.upper()] if target != "." else 0
        return {"action": max(safe, key=gain), "points": [note]}

    # ---- 復盤與提問 ----

    def review(self, settings, actions, human_seat):
        state = self.new_state(settings)
        moments = []
        for t, action in enumerate(actions):
            if state.to_move == human_seat and action != RESIGN:
                found = self._review_move(state, t, action)
                if found:
                    moments.append(found)
            state = rules.play(state, action)
        return moments

    def _review_move(self, state, t, action):
        color, name = state.to_move, COLOR_NAMES[state.to_move]
        played = self.move_text(state, action)
        _, rule_points = self._rule_facts(state, action)
        wins = self.wins_in_one(state)
        # 規則確認的錯誤：有一步殺沒走、或讓對方一步殺（明明有別的步可以避免）
        if wins and action not in wins:
            best = wins[0]
            return self._moment(t, action, best, "key", "錯過一步殺", f"錯過一步殺：{self.move_text(state, best)}", [
                (RULE, f"{name}走{self.move_text(state, best)}就能直接獲勝，實際走了{played}。")] + rule_points)
        if self._loses_at_once(state, action) and not wins:
            safe = next((m for m in rules.legal_moves(state) if not self._loses_at_once(state, m)), None)
            if safe:
                return self._moment(t, action, safe, "key", "讓對方一步殺", "讓對方一步就獲勝", [
                    (RULE, f"{name}走{played}之後，對方一步就能獲勝；例如改走{self.move_text(state, safe)}就不會。")] + rule_points)
            return None  # 每一步都會輸，不是這一步的錯
        try:
            best = self._analysis(state)[0]
            mine = self._analysis(state, searchmoves=[action])[0]
        except (PikafishUnavailable, IndexError):
            return None
        if best["move"] == action or (best["mate"] is not None and best["mate"] < 0):
            return None  # 引擎的首選，或本來就會被將死：沒有證據說這一步錯
        best_text = self.move_text(state, best["move"])
        if best["mate"] is not None and (mine["mate"] is None or mine["mate"] < 0):
            return self._moment(t, action, best["move"], "key", "錯過殺棋", f"錯過殺棋：{best_text}", [
                self._eval_point(color, best, f"走{best_text}的話，"), self._eval_point(color, mine, f"實際走{played}後，")]
                + rule_points)
        if mine["mate"] is not None and mine["mate"] < 0:
            return self._moment(t, action, best["move"], "key", "被對方找到殺棋", "走完後會被將死", [
                self._eval_point(color, mine, f"走{played}後，"),
                self._eval_point(color, best, f"Pikafish 建議的{best_text}，走完後")] + rule_points)
        if best["mate"] is not None or mine["mate"] is not None:
            return None  # 兩邊都是殺棋（只差快慢），不列
        # 先四捨五入到 0.1 個兵（和文字顯示的一樣），否則會出現「相差約 1.5 個兵，不算失誤」這種前後矛盾
        loss = round((value(best) - value(mine)) / 10) * 10
        if loss < LOSS_SUGGEST:
            return None
        level = "key" if loss >= LOSS_KEY else "minor" if loss >= LOSS_MINOR else "suggest"
        points = [(ESTIMATE, f"依引擎估計（Pikafish {DEPTH} 層），{name}走{played}後{lead_text(mine['score'])}；"
                             f"走{best_text}的話{lead_text(best['score'])}，相差約 {pawns(loss)}。")]
        if best["score"] <= -DECIDED:
            points.append((ESTIMATE, f"這時{name}已{lead_text(best['score'])}，勝負大致已定；這一步的問題是損失，不是輸棋的原因。"))
        if level == "suggest":
            points.append((ESTIMATE, f"差距約 {pawns(loss)}，不算失誤；{best_text}是 Pikafish 比較推薦的走法。"))
        title = f"建議：Pikafish 推薦{best_text}" if level == "suggest" else f"損失約 {pawns(loss)}"
        return self._moment(t, action, best["move"], level, "建議" if level == "suggest" else "局面損失", title,
                            points + rule_points, loss=loss)

    @staticmethod
    def _moment(t, played, best, level, pattern, title, points, **extra):
        return {"index": t, "type": "gap", "level": level, "title": title, "pattern": pattern,
                "played": played, "best": best, **extra,
                "points": [{"evidence": ev, "text": text} for ev, text in points]}

    def question_focus(self, question):
        """問題裡的走法：ICCS（h2e2）或中文記法（炮二平五）。中文記法要看局面才知道是哪一步，在 explain_position 換算。"""
        focus = []
        for match in re.findall(rf"(?<![A-Za-z0-9])([a-iA-I][0-9][a-iA-I][0-9])(?![A-Za-z0-9])|({CHINESE_RE})", question):
            token = match[0].lower() or match[1]
            if token not in focus:
                focus.append(token)
        return focus

    def explain_position(self, state, focus, next_action):
        points = [(RULE, f"目前輪到{COLOR_NAMES[state.to_move]}走。")]
        if state.over:
            return [{"evidence": RULE, "text": f"對局已經結束：{state.result[1]}。"}]
        points += self._threats(state)
        try:
            points += self._candidates(state)
        except PikafishUnavailable as exc:
            points.append((ESTIMATE, f"Pikafish 無法使用，沒有候選分析（{exc}）。"))
        moves = []
        for token in focus[:3]:
            move = token if ACTION_RE.match(token) else rules.from_chinese(state, token)
            if move is None:
                points.append((RULE, f"{COLOR_NAMES[state.to_move]}現在沒有「{token}」這一步。"))
            elif move not in moves:
                moves.append(move)
                points += self._check(state, move)
        if next_action:
            text = self.move_text(state, next_action) if next_action != RESIGN else "認輸"
            points.append((RULE, f"紀錄中實際的下一步是{COLOR_NAMES[state.to_move]}{text}。"))
            if next_action != RESIGN and next_action not in moves:  # 問題已經問到這一步時不重複
                points += self._check(state, next_action)
        return [{"evidence": ev, "text": text} for ev, text in points]

    def explain_action(self, settings, actions, n):
        state = self.replay(settings, actions[:n - 1])
        action = actions[n - 1]
        text = self.move_text(state, action) if action != RESIGN else "認輸"
        points = [(RULE, f"第 {n} 步是{COLOR_NAMES[state.to_move]}{text}。")]
        if action != RESIGN:
            points += self._check(state, action)
        return [{"evidence": ev, "text": text} for ev, text in points]


MODULE = XiangqiModule()
