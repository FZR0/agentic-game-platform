"""國際象棋的遊戲模組：rules.py（python-chess 的規則與窮舉，事實屬於規則確認）＋Stockfish（分析，屬於引擎估計；
強制將死屬於求解確認）。

行動記法：UCI（"e2e4"、升變 "e7e8q"、王車易位寫王的走法 "e1g1"）與 "resign"。座位 1＝白棋（先手）、2＝黑棋。
說明裡的走法一律寫成「白棋的馬 g1→f3（Nf3）」：用箭頭寫格子，說明檢查才抓得到座標。
"""
import re
import threading
from collections import OrderedDict

import chess

from arena.base import ESTIMATE, RULE, SOLVED, GameModule

from . import rules
from .rules import COLOR_NAMES, PIECE_NAMES, RESIGN, move_text, piece_text
from .stockfish import ENGINE, StockfishUnavailable

DEPTH = 14      # 所有分析（Agent 工具、引擎代下、復盤）共用同一個深度與候選數，結果才一致
MULTIPV = 5
CANDIDATES = 5
# 復盤用勝率損失（行動方觀點，0–1）。【暫定值、尚未校準】沒有實測資料支持這些數字，
# 要拿真實對局與引擎自我對局的結果調整後才能當成定論（規劃文件：「門檻在前導研究時調整」）。
# 損失不會超過下之前的勝率本身，所以局面已經輸定（勝率低於 5%）時不會被列出，不必另外處理。
KEY_LOSS = 0.20      # 損失 20 個百分點以上是關鍵時刻
MINOR_LOSS = 0.10    # 10–20 個百分點是小失誤
SUGGEST_LOSS = 0.05  # 5–10 個百分點不算失誤，列為「建議」；再小就在引擎估計的誤差內


def pct(winrate):
    return f"{winrate * 100:.0f}%"


def arrow(uci):
    return f"{uci[:2]}→{uci[2:4]}" + (f"={uci[4].upper()}" if len(uci) == 5 else "")


def mate_text(n):
    return f"{abs(n)} 步內"


class ChessModule(GameModule):
    key = "chess"
    name = "國際象棋"
    seat_names = COLOR_NAMES
    # x 不擋：吃子寫成 Nxd5、exd5 時 d5 也要抓得到；前面緊接一個格子時也要抓：UCI 寫成 g1f3 時 f3 也要檢查
    coord_pattern = r"(?:(?<![a-wyz0-9])|(?<=[a-h][1-8]))([a-h][1-8])(?![0-9])"
    default_steps = 4
    board_script = "chessgame/board.js"
    icon = "chessgame/icon.svg"
    tagline = "標準規則，可以執白或執黑。AI 用 Stockfish 查證每一步。"
    numbered = False  # 棋子不會一直待在原地，沒有「在棋子上顯示手數」這回事
    rules = [
        {"title": "目標", "items": ["把對方的王困住，讓它被攻擊（將軍）而且沒有任何辦法解開，叫「將死」，先將死對方的人獲勝。"]},
        {"title": "怎麼下", "items": ["白棋先下，雙方輪流走一步（每一方走一步算一手）。",
                                   "先點自己的棋子，棋盤會標出它能走到的格子，再點目標格。點錯了，再點別的棋子或同一顆棋子就會取消。",
                                   "兵走到最後一排會升變，畫面會讓你選要變成后、車、象或馬。",
                                   "格子的座標：直的是 a 到 h，橫的是 1 到 8，白棋在下面。說明裡的座標可以點，棋盤上會標出位置。"]},
        {"title": "棋子怎麼走", "items": ["王：每次走一格，八個方向都可以。",
                                      "后：直、橫、斜都可以，走幾格都行，但不能跳過別的棋子。",
                                      "車：直走或橫走，不能跳過棋子。",
                                      "象：只能斜走，不能跳過棋子。",
                                      "馬：先走兩格再轉一格（像「日」字），可以跳過別的棋子。",
                                      "兵：往前走一格；還在起點時可以走兩格；斜前方有對方的子時才能吃，往前不能吃。"]},
        {"title": "特殊走法", "items": ["王車易位：王和車都沒有動過、中間沒有棋子、王沒有被將軍也不會經過被攻擊的格子時，王往車的方向走兩格，車跳到王的另一邊。畫面上點王再點它要去的那一格（兩格外）就是易位。",
                                     "吃過路兵：對方的兵從起點一次走兩格、停在你的兵旁邊時，你可以在下一手斜走到它經過的那一格，把它吃掉。",
                                     "升變：兵走到對方底線，變成后、車、象或馬。"]},
        {"title": "將軍與和局", "items": ["被將軍時一定要解開：把王移開、擋住攻擊的路線，或吃掉攻擊的子。",
                                      "輪到的一方沒有合法走法、但沒有被將軍，叫「逼和」，和局。",
                                      "雙方都沒有足夠的子將死對方、同一個局面出現三次、五十步內沒有吃子也沒有走兵，也是和局，成立時系統會直接判和。",
                                      "也可以按「認輸」結束。"]},
    ]
    tools = [
        {"type": "function", "function": {
            "name": "find_threats",
            "description": "查現在的威脅：有沒有被將軍、自己或對方能一步將死、被攻擊的子、現在能吃的子。",
            "parameters": {"type": "object", "properties": {}}}},
        {"type": "function", "function": {
            "name": "list_candidates",
            "description": f"用 Stockfish 列出最好的前 {CANDIDATES} 個走法，附勝率與後續幾步。",
            "parameters": {"type": "object", "properties": {}}}},
        {"type": "function", "function": {
            "name": "check_move",
            "description": "檢查某一步：合不合法、吃子、將軍、下完這顆子安不安全，以及 Stockfish 估計的勝率變化。",
            "parameters": {"type": "object", "properties": {
                "move": {"type": "string", "description": "UCI 走法：起點格加終點格，例如 g1f3；升變多一個字母，例如 e7e8q"}},
                "required": ["move"]}}},
        {"type": "function", "function": {
            "name": "play_move",
            "description": "決定走哪一步。",
            "parameters": {"type": "object", "properties": {
                "move": {"type": "string", "description": "UCI 走法：起點格加終點格，例如 e2e4；升變多一個字母，例如 e7e8q；王車易位寫王的走法，例如 e1g1"},
                "reason": {"type": "string", "description": "一句話說明為什麼這樣走"}},
                "required": ["move", "reason"]}}},
    ]
    benchmark_samples = [  # 義大利開局、法國防禦、西西里防禦的開局幾手
        ["e2e4", "e7e5", "g1f3", "b8c6"],
        ["e2e4", "e7e6", "d2d4", "d7d5", "b1c3", "g8f6"],
        ["e2e4", "c7c5", "g1f3", "d7d6", "d2d4", "c5d4", "f3d4", "g8f6"],
    ]

    def __init__(self):
        self.analyzer = ENGINE  # 測試時換成假的分析器
        self._cache = OrderedDict()  # 同一個局面在一次 Agent 決策中常被查好幾次
        self._lock = threading.Lock()

    # ---- 規則 ----

    def clean_settings(self, data):
        return {}

    def new_state(self, settings):
        return rules.ChessState()

    def normalize(self, text):
        return rules.normalize(text)

    def apply(self, state, action):
        return rules.play(state, action)

    def to_move(self, state):
        return state.to_move

    def outcome(self, state):
        r = rules.result(state)
        return (r[0], r[0] is None) if r else None

    def frame(self, state, seat):
        """board 是 64 個字元，第 0 個是 a1、第 1 個是 b1……第 8 個是 a2（大寫白棋、小寫黑棋、. 空格）；
        legal 是 {起點格: [終點格, ...]}，promotions 是需要選升變的「起點終點」。西洋棋沒有隱藏資訊，所有座位看到的一樣。"""
        board = state.board
        r = rules.result(state)
        legal, promotions = {}, set()
        if not r:
            for move in board.legal_moves:
                targets = legal.setdefault(chess.square_name(move.from_square), [])
                name = chess.square_name(move.to_square)
                if name not in targets:
                    targets.append(name)
                if move.promotion:
                    promotions.add(chess.square_name(move.from_square) + name)
        king = board.king(board.turn)
        return {"board": "".join(board.piece_at(s).symbol() if board.piece_at(s) else "." for s in range(64)),
                "last": state.actions[-1] if state.actions else None, "to_move": state.to_move,
                "check": chess.square_name(king) if board.is_check() and king is not None else None,
                "legal": legal, "promotions": sorted(promotions), "result": r[1] if r else None}

    # ---- 分析 ----

    def _analysis(self, board):
        key = (board.fen(), tuple(m.uci() for m in board.move_stack))
        with self._lock:
            if key not in self._cache:
                self._cache[key] = self.analyzer.analyse(board, DEPTH, MULTIPV)
                while len(self._cache) > 64:
                    self._cache.popitem(last=False)
            return self._cache[key]

    def _value(self, board, move):
        """下 move 之後，行動方的 (勝率, 將死步數)；將死步數正數＝行動方這麼多步內將死對方，負數＝被將死，沒有就是 None。
        走法在前幾名裡就直接用那條候選線；不在的話分析下完之後的局面（對手的觀點，要翻過來）。"""
        for line in self._analysis(board):
            if line["move"] == move.uci():
                return line["winrate"], line["mate"]
        after = board.copy()
        after.push(move)
        if after.is_checkmate():
            return 1.0, 1
        lines = self._analysis(after)
        if not lines:  # 和局
            return 0.5, None
        top = lines[0]
        mate = None if top["mate"] is None else (-top["mate"] + 1 if top["mate"] < 0 else -top["mate"])
        return 1 - top["winrate"], mate

    def _threats(self, state):
        board = state.board
        me, them = COLOR_NAMES[state.to_move], COLOR_NAMES[rules.other(state.to_move)]
        points = []
        if board.is_check():
            names = "、".join(piece_text(board, s) for s in board.checkers())
            points.append((RULE, f"{me}正被將軍（{names}），這一手一定要解開。"))
        mates = rules.mates_in_one(board)
        if mates:
            points.append((RULE, f"{me}現在可以一步將死對方：{'；'.join(move_text(board, m) for m in mates[:3])}。"))
        threats = rules.threatened_mates(board)
        if threats:
            points.append((RULE, f"如果{me}這一手沒有處理，{them}下一手就能一步將死："
                                 f"{'；'.join(move_text(board, m) for m in threats[:3])}（輪到{them}時的走法）。"))
        for square, attackers, defended in rules.weak_pieces(board, board.turn)[:3]:
            by = "、".join(piece_text(board, a) for a in attackers[:2])
            why = "而且沒有己方的子保護" if not defended else "雖然有己方的子保護，但攻擊它的子價值比較低"
            points.append((RULE, f"{piece_text(board, square)} 正被攻擊（{by}），{why}。"))
        for square, attackers, defended in rules.weak_pieces(board, not board.turn)[:3]:
            captures = [m for m in board.legal_moves if m.to_square == square]
            if captures:
                cheapest = min(captures, key=lambda m: rules.PIECE_VALUES[board.piece_type_at(m.from_square)])
                why = "沒有對方的子保護" if not defended else "雖然有對方的子保護，但吃它的子價值比較低"
                points.append((RULE, f"{piece_text(board, square)} 正被攻擊，{why}；{me}現在可以吃它：{move_text(board, cheapest)}。"))
        if not points:
            points.append((RULE, f"{me}沒有被將軍、沒有一步將死的機會或威脅，雙方也沒有明顯被攻擊的子。"))
        return points

    def _line_text(self, board, line):
        """一條候選線：走法、勝率或將死、後續三步。"""
        move = chess.Move.from_uci(line["move"])
        head = move_text(board, move)
        if line["mate"] is not None and line["mate"] > 0:
            head += f"，可在 {mate_text(line['mate'])}將死對方"
        else:
            head += f"，勝率約 {pct(line['winrate'])}"
        rest = [chess.Move.from_uci(u) for u in line["pv"][:3]]
        return f"{head}，後續：{board.variation_san(rest)}"

    def _candidates(self, state):
        board = state.board
        me = COLOR_NAMES[state.to_move]
        lines = self._analysis(board)
        if not lines:
            return []
        items = "；".join(f"{i}. {self._line_text(board, line)}" for i, line in enumerate(lines[:CANDIDATES], 1))
        points = [(ESTIMATE, f"依引擎估計（Stockfish 深度 {DEPTH}），{me}目前勝率約 {pct(lines[0]['winrate'])}；"
                             f"候選依好壞排序：{items}。")]
        best = lines[0]
        if best["mate"] is not None and best["mate"] > 0:
            points.append((SOLVED, f"在搜尋範圍內已確認，{me}可以在 {mate_text(best['mate'])}將死對方，"
                                   f"第一步是 {move_text(board, chess.Move.from_uci(best['move']))}。"))
        elif best["mate"] is not None:
            points.append((SOLVED, f"在搜尋範圍內已確認，{me}最好的下法也躲不過在 {mate_text(best['mate'])}被將死。"))
        return points

    def _check(self, state, action):
        board = state.board
        me, them = COLOR_NAMES[state.to_move], COLOR_NAMES[rules.other(state.to_move)]
        try:
            rules.play(state, action)
        except ValueError as exc:
            return [(RULE, f"{me}不能這樣下：{exc}。")]
        move = chess.Move.from_uci(action)
        points = [(RULE, f"{move_text(board, move)}。")]
        if board.is_capture(move):
            victim = chess.PAWN if board.is_en_passant(move) else board.piece_type_at(move.to_square)
            points.append((RULE, f"這一步會吃掉{them}的{PIECE_NAMES[victim]}。"))
        after = board.copy()
        after.push(move)
        if after.is_checkmate():
            points.append((RULE, f"這一步將死對方，{me}獲勝。"))
            return points
        if after.is_check():
            points.append((RULE, f"這一步將軍。"))
        ended = rules.result(rules.play(state, action))
        if not ended:
            for square, attackers, defended in rules.weak_pieces(after, board.turn):
                if square == move.to_square:
                    by = "、".join(piece_text(after, a) for a in attackers[:2])
                    why = "而且沒有己方的子保護" if not defended else "雖然有己方的子保護，但攻擊它的子價值比較低"
                    points.append((RULE, f"下完後這顆子（{piece_text(after, square)}）會被攻擊（{by}），{why}。"))
        else:
            points.append((RULE, f"下完後對局結束：{ended[1]}。"))
            return points
        mates = rules.allowed_mates(board, move)
        if mates:
            points.append((RULE, f"下完後{them}可以一步將死：{'；'.join(move_text(after, m) for m in mates[:3])}。"))
        try:
            lines = self._analysis(board)
            wr, mate = self._value(board, move)
            points.append((ESTIMATE, f"依引擎估計（Stockfish 深度 {DEPTH}），{me}下 {arrow(action)} 後勝率約 {pct(wr)}"
                                     f"（下之前最佳約 {pct(lines[0]['winrate'])}）。"))
            reply = self._analysis(after)
            if reply:
                points.append((ESTIMATE, f"依引擎估計，{them}最好的應對是 {move_text(after, chess.Move.from_uci(reply[0]['move']))}。"))
            if mate is not None and mate > 0:
                points.append((SOLVED, f"在搜尋範圍內已確認，{me}下 {arrow(action)} 後可以在 {mate_text(mate)}將死對方。"))
            elif mate is not None:
                points.append((SOLVED, f"在搜尋範圍內已確認，{me}下 {arrow(action)} 後會在 {mate_text(mate)}被將死。"))
        except StockfishUnavailable as exc:
            points.append((ESTIMATE, f"Stockfish 無法使用，沒有勝率估計（{exc}）。"))
        return points

    # ---- Agent ----

    def agent_prompt(self, seat):
        return f"""你是國際象棋 AI 棋手，執{COLOR_NAMES[seat]}。用工具查證之後再走棋。
走法一律用 UCI 記法：起點格加終點格，例如 e2e4、g1f3；兵升變多一個字母，例如 e7e8q；王車易位寫王的走法，例如白棋短易位 e1g1。
工具結果裡的走法寫成「g1→f3（Nf3）」，play_move 請填 g1f3。格子是 a–h 與 1–8，白棋在下方（第 1 排）。
建議流程：先呼叫 find_threats 看有沒有被將軍、一步將死、被攻擊的子；再用 list_candidates 看 Stockfish 推薦的走法；
需要時用 check_move 檢查一兩步；最後呼叫 play_move。
硬性規定：只能走合法的步；能一步將死對方時必須走將死的那一步；不能走讓對手一步將死的步（除非每一步都擋不住）；不能認輸。違反時會被退回。
play_move 的 reason 用繁體中文一句話說明，只能提到工具結果裡出現過的格子。"""

    def board_text(self, state, actions):
        board = state.board
        lines = [f"目前局面（FEN）：{board.fen()}"]
        for color in (chess.WHITE, chess.BLACK):
            pieces = sorted(board.piece_map().items(), key=lambda kv: (-rules.PIECE_VALUES[kv[1].piece_type], kv[0]))
            names = "、".join(f"{PIECE_NAMES[p.piece_type]} {chess.square_name(s)}" for s, p in pieces if p.color == color)
            lines.append(f"{COLOR_NAMES[rules.seat_of(color)]}：{names}")
        if board.move_stack:
            moves = board.move_stack[-6:]
            base = board.copy()
            for _ in moves:
                base.pop()
            lines.append(f"最近幾手：{base.variation_san(moves)}")
        lines.append(f"現在輪到你（{COLOR_NAMES[state.to_move]}）下第 {len(actions) + 1} 手。")
        return "\n".join(lines)

    def action_from(self, args):
        return self.normalize(args.get("move"))

    def run_tool(self, state, name, args):
        try:
            if name == "find_threats":
                return "查威脅", self._threats(state)
            if name == "list_candidates":
                return "看 Stockfish 候選", self._candidates(state)
            if name == "check_move":
                action = self.normalize(args.get("move"))
                if action is None or action == RESIGN:
                    return None
                return f"檢查 {arrow(action)}", self._check(state, action)
        except StockfishUnavailable as exc:
            return name, [(ESTIMATE, f"Stockfish 無法使用（{exc}），這個工具沒有結果。")]
        return None

    def rejection(self, state, action):
        if action is None:
            return "走法格式不對，請用 UCI 記法：起點格加終點格，例如 g1f3（升變 e7e8q）"
        if action == RESIGN:
            return "Agent 不能認輸，請走一步"
        try:
            rules.play(state, action)
        except ValueError as exc:
            return str(exc)
        board = state.board
        move = chess.Move.from_uci(action)
        mates = rules.mates_in_one(board)
        if mates and move not in mates:
            return f"現在可以一步將死對方（{'；'.join(move_text(board, m) for m in mates[:3])}），必須走將死的那一步"
        allowed = rules.allowed_mates(board, move)
        if allowed:
            safe = self._safe_example(board, move)
            if safe:
                after = board.copy()
                after.push(move)
                return (f"走 {arrow(action)} 之後對手可以一步將死（{move_text(after, allowed[0])}），"
                        f"請改走不會被一步將死的步，例如 {move_text(board, safe)}")
        return None

    def _safe_example(self, board, move):
        """move 以外不會讓對手一步將死的一步，沒有就回傳 None。優先用 Stockfish 排名最前的：
        只取第一個合法步的話，舉例可能是一步送后的壞棋，Agent 照著走、玩家照著學都會出問題。"""
        safe = [m for m in list(board.legal_moves) if m != move and not rules.allowed_mates(board, m)]
        if not safe:
            return None
        try:
            for line in self._analysis(board):
                candidate = chess.Move.from_uci(line["move"])
                if candidate in safe:
                    return candidate
        except StockfishUnavailable:
            pass
        return safe[0]

    def forced(self, state):
        legal = list(state.board.legal_moves)
        if len(legal) == 1:
            move = legal[0]
            return {"action": move.uci(),
                    "points": [(RULE, f"{COLOR_NAMES[state.to_move]}只有一步合法的走法：{move_text(state.board, move)}。")]}
        return None

    def engine_move(self, state):
        try:
            lines = self._analysis(state.board)
            if lines:
                best = lines[0]
                move = chess.Move.from_uci(best["move"])
                points = [(ESTIMATE, f"依引擎估計（Stockfish 深度 {DEPTH}），{move_text(state.board, move)}是最佳候選，"
                                     f"勝率約 {pct(best['winrate'])}。")]
                if best["mate"] is not None and best["mate"] > 0:
                    points.append((SOLVED, f"在搜尋範圍內已確認，這一步之後可以在 {mate_text(best['mate'])}將死對方。"))
                return {"action": best["move"], "points": points}
        except StockfishUnavailable as exc:
            return self._simple_move(state, str(exc))
        return self._simple_move(state, "Stockfish 沒有回傳候選")

    def _simple_move(self, state, why):
        """Stockfish 無法使用時的簡單選步：能將死就將死；否則吃價值最高、而且划算的子；否則避開一步被將死的步。品質很低，要標明。"""
        board = state.board
        note = (ESTIMATE, f"Stockfish 無法使用（{why}），這一步只用簡單規則選擇，品質很低。")
        mates = rules.mates_in_one(board)
        if mates:
            return {"action": mates[0].uci(), "points": [note]}
        safe = [m for m in board.legal_moves if not rules.allowed_mates(board, m)] or list(board.legal_moves)

        def gain(move):
            if not board.is_capture(move):
                return 0
            victim = chess.PAWN if board.is_en_passant(move) else board.piece_type_at(move.to_square)
            after = board.copy()
            after.push(move)
            exposed = move.to_square in {w[0] for w in rules.weak_pieces(after, board.turn)}
            return rules.PIECE_VALUES[victim] - (rules.PIECE_VALUES[board.piece_type_at(move.from_square)] if exposed else 0)

        best = max(safe, key=lambda m: (gain(m), -len(m.uci()), m.uci()))
        return {"action": best.uci(), "points": [note]}

    # ---- 復盤與提問 ----

    def review(self, settings, actions, human_seat):
        state = self.new_state(settings)
        states = [state]
        for action in actions:
            state = rules.play(state, action)
            states.append(state)
        moments = []
        try:
            for t, action in enumerate(actions):
                if action != RESIGN and states[t].to_move == human_seat:
                    moment = self._judge(states[t], action)
                    if moment:
                        moment["index"] = t
                        moments.append(moment)
        except StockfishUnavailable:
            return []
        return moments

    def _judge(self, state, action):
        """玩家這一步和引擎的最佳選擇比；沒有問題時回傳 None。標準和 AI 自己的選擇一致（同一個分析）。"""
        board = state.board
        move = chess.Move.from_uci(action)
        me, them = COLOR_NAMES[state.to_move], COLOR_NAMES[rules.other(state.to_move)]
        lines = self._analysis(board)
        if not lines or lines[0]["move"] == action:
            return None
        best = chess.Move.from_uci(lines[0]["move"])
        best_wr = lines[0]["winrate"]
        wr, mate = self._value(board, move)
        loss = best_wr - wr
        played = move_text(board, move)
        engine_points = [
            (ESTIMATE, f"依引擎估計（Stockfish 深度 {DEPTH}），{me}下 {arrow(action)} 前勝率約 {pct(best_wr)}，"
                       f"下完後約 {pct(wr)}，約損失 {loss * 100:.0f} 個百分點。"),
            (ESTIMATE, f"Stockfish 建議下 {move_text(board, best)}（勝率約 {pct(best_wr)}）。")]

        mates = rules.mates_in_one(board)
        allowed = rules.allowed_mates(board, move)
        safe = self._safe_example(board, move) if allowed else None
        if mates and move not in mates:
            level, pattern = "key", "錯過一步將死"
            title = f"錯過一步將死：{board.san(mates[0])}"
            points = [(RULE, f"當時{me}可以一步將死對方：{move_text(board, mates[0])}；實際下的是 {played}。")]
        elif safe:
            level, pattern = "key", "讓對手一步將死"
            title = "下完後被一步將死"
            after = board.copy()
            after.push(move)
            points = [(RULE, f"{me}下 {played} 之後，{them}可以一步將死：{move_text(after, allowed[0])}；"
                             f"當時有不會被一步將死的下法，例如 {move_text(board, safe)}。")]
        elif lines[0]["mate"] is not None and lines[0]["mate"] > 0 and not (mate is not None and mate > 0):
            level, pattern = "key", "錯過強制將死"
            title = f"錯過 {mate_text(lines[0]['mate'])}將死"
            points = [(SOLVED, f"在搜尋範圍內已確認，當時{me}下 {move_text(board, best)} 可以在 {mate_text(lines[0]['mate'])}將死對方；"
                               f"實際下的是 {played}。")]
        elif loss >= SUGGEST_LOSS:
            level = "key" if loss >= KEY_LOSS else "minor" if loss >= MINOR_LOSS else "suggest"
            pattern = "建議" if level == "suggest" else "勝率損失"
            title = (f"建議：Stockfish 推薦 {board.san(best)}" if level == "suggest"
                     else f"勝率從約 {pct(best_wr)} 掉到約 {pct(wr)}")
            points = []
            if level == "suggest":
                engine_points.append((ESTIMATE, f"差距約 {loss * 100:.0f} 個百分點，不算失誤；{board.san(best)} 是 Stockfish 比較推薦的下法。"))
        else:
            return None
        return {"type": "gap", "level": level, "title": title, "played": action, "pattern": pattern,
                "best": best.uci(), "loss": loss,
                "points": [{"evidence": ev, "text": text} for ev, text in points + engine_points]}

    def overall_points(self, settings, actions, human_seat):
        state = self.new_state(settings)
        moves = captures = checks = 0
        castled = False
        for action in actions:
            if action != RESIGN and state.to_move == human_seat:
                move = chess.Move.from_uci(action)
                moves += 1
                captures += state.board.is_capture(move)
                castled = castled or state.board.is_castling(move)
                state = rules.play(state, action)
                checks += state.board.is_check()
            else:
                state = rules.play(state, action)
        if not moves:
            return []
        return [(RULE, f"你一共走了 {moves} 步，其中吃子 {captures} 次、將軍 {checks} 次。"),
                (RULE, "你這盤有王車易位。" if castled else "你這盤沒有王車易位。")]

    def question_focus(self, question):
        """問題中提到的格子（小寫、不重複）；格子不是行動，explain_position 會說明那一格的情況。"""
        focus = []
        for match in re.findall(r"(?:(?<![A-Za-z0-9])|(?<=[a-hA-H][1-8]))([a-hA-H][1-8])(?![0-9])", question):
            if match.lower() not in focus:
                focus.append(match.lower())
        return focus

    def _square_facts(self, board, name):
        square = chess.parse_square(name)
        piece = board.piece_at(square)
        points = [(RULE, f"{name} 上是{piece_text(board, square).rsplit(' ', 1)[0]}。" if piece else f"{name} 目前是空格。")]
        white, black = board.attackers(chess.WHITE, square), board.attackers(chess.BLACK, square)
        if white or black:
            points.append((RULE, f"能走到 {name} 的子（攻擊對方的子，或保護自己的子）：{COLOR_NAMES[rules.WHITE]}"
                                 f" {len(white)} 個、{COLOR_NAMES[rules.BLACK]} {len(black)} 個。"))
        if piece and piece.color == board.turn:
            targets = [chess.square_name(m.to_square) for m in board.legal_moves if m.from_square == square]
            points.append((RULE, f"這顆子現在可以走到：{'、'.join(dict.fromkeys(targets))}。" if targets else "這顆子現在沒有合法的走法。"))
        return points

    def explain_position(self, state, focus, next_action):
        points = [(RULE, f"目前輪到{COLOR_NAMES[state.to_move]}下。")] + self._threats(state)
        try:
            points += self._candidates(state)
        except StockfishUnavailable as exc:
            points.append((ESTIMATE, f"Stockfish 無法使用，沒有候選分析（{exc}）。"))
        for name in focus[:3]:
            points += self._square_facts(state.board, name)
        if next_action:
            points.append((RULE, f"紀錄中實際的下一手是{COLOR_NAMES[state.to_move]}"
                                 + ("認輸。" if next_action == RESIGN else f"下 {move_text(state.board, chess.Move.from_uci(next_action))}。")))
            if next_action != RESIGN:
                points += self._check(state, next_action)
        return [{"evidence": ev, "text": text} for ev, text in points]

    def explain_action(self, settings, actions, n):
        state = self.replay(settings, actions[:n - 1])
        action = actions[n - 1]
        if action == RESIGN:
            return [{"evidence": RULE, "text": f"第 {n} 手是{COLOR_NAMES[state.to_move]}認輸。"}]
        points = [(RULE, f"第 {n} 手是{COLOR_NAMES[state.to_move]}下 {move_text(state.board, chess.Move.from_uci(action))}。")]
        points += self._check(state, action)
        return [{"evidence": ev, "text": text} for ev, text in points]


MODULE = ChessModule()
