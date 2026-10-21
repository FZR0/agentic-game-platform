"""中國象棋規則（純程式，不依賴 Pikafish）。

棋盤 9 路 × 10 列；座標用 ICCS：欄 a–i（紅方由左到右），列 0–9（紅方底線是 0），一步寫成起點＋終點，例如 h2e2。
內部用 (f, r)，格子編號 r * 9 + f；棋子用 FEN 字母，紅方大寫、黑方小寫：K 帥、A 仕、B 相、N 馬、R 車、C 炮、P 兵。
勝負：將死或困斃（輪到的一方沒有合法步）判負；同一局面第三次出現時，若只有一方在這段循環裡每一步都將軍，
這一方判負（長將判負），否則判和（長捉的完整規則很複雜，第一版不判，一律算和）；連續 120 步（雙方各 60 步）沒有吃子判和。
"""
FILES = "abcdefghi"
RED, BLACK = 1, 2
COLOR_NAMES = {RED: "紅方", BLACK: "黑方"}
RESIGN = "resign"
START = ("RNBAKABNR" "........." ".C.....C." "P.P.P.P.P" "........."
         "........." "p.p.p.p.p" ".c.....c." "........." "rnbakabnr")
NO_CAPTURE_LIMIT = 120
PIECE_NAMES = {"K": "帥", "A": "仕", "B": "相", "N": "傌", "R": "俥", "C": "炮", "P": "兵",
               "k": "將", "a": "士", "b": "象", "n": "馬", "r": "車", "c": "砲", "p": "卒"}
RED_NUMS = "一二三四五六七八九"
BLACK_NUMS = "１２３４５６７８９"
ORTHO = ((1, 0), (-1, 0), (0, 1), (0, -1))
DIAG = ((1, 1), (1, -1), (-1, 1), (-1, -1))
KNIGHT = ((1, 2), (-1, 2), (1, -2), (-1, -2), (2, 1), (2, -1), (-2, 1), (-2, -1))


def other(color):
    return RED + BLACK - color


def color_of(piece):
    return RED if piece.isupper() else BLACK


def square(f, r):
    return f"{FILES[f]}{r}"


def parse_square(text):
    """'e2' → (4, 2)；格式不對時回傳 None。"""
    if len(text) != 2 or text[0] not in FILES or not text[1].isdigit():
        return None
    return FILES.index(text[0]), int(text[1])


def parse_move(action):
    """'h2e2' → ((7, 2), (4, 2))；格式不對時回傳 None。"""
    if not isinstance(action, str) or len(action) != 4:
        return None
    a, b = parse_square(action[:2]), parse_square(action[2:])
    return (a, b) if a and b else None


def on_board(f, r):
    return 0 <= f < 9 and 0 <= r < 10


def in_palace(color, f, r):
    return 3 <= f <= 5 and (0 <= r <= 2 if color == RED else 7 <= r <= 9)


def own_side(color, r):
    """r 是否在 color 這一方的河界內（相不能過河）。"""
    return r <= 4 if color == RED else r >= 5


class XiangqiState:
    """象棋局面。play 產生新的局面，不就地修改。"""

    def __init__(self):
        self.board = list(START)
        self.to_move = RED
        self.actions = []
        self.keys = [self.key()]  # 每一步之後的局面（含輪到誰），判斷重複用
        self.checks = []          # 每一步是否將軍
        self.quiet = 0            # 連續沒有吃子的步數
        self.result = None        # (勝方或 None, 說明)

    def key(self):
        return "".join(self.board) + str(self.to_move)

    def copy(self):
        s = XiangqiState.__new__(XiangqiState)
        s.board = list(self.board)
        s.to_move, s.quiet, s.result = self.to_move, self.quiet, self.result
        s.actions, s.keys, s.checks = list(self.actions), list(self.keys), list(self.checks)
        return s

    def at(self, f, r):
        return self.board[r * 9 + f]

    @property
    def over(self):
        return self.result is not None


def find_king(board, color):
    return board.index("K" if color == RED else "k")


def in_check(board, color):
    """color 的帥（將）是否被攻擊；將帥照面也算。"""
    k = find_king(board, color)
    kf, kr = k % 9, k // 9
    enemy = other(color)
    rook, cannon, knight, pawn, king = ("rcnpk" if enemy == BLACK else "RCNPK")
    for df, dr in ORTHO:
        f, r, screens = kf + df, kr + dr, 0
        while on_board(f, r):
            p = board[r * 9 + f]
            if p != ".":
                if screens == 0 and (p == rook or (p == king and df == 0)):
                    return True
                if screens == 1 and p == cannon:
                    return True
                screens += 1
                if screens > 1:
                    break
            f, r = f + df, r + dr
    for df, dr in KNIGHT:  # 對方的馬從 (kf - df, kr - dr) 跳過來，馬腿在馬的旁邊
        nf, nr = kf - df, kr - dr
        if on_board(nf, nr) and board[nr * 9 + nf] == knight:
            leg = (nf + (df // 2 if abs(df) == 2 else 0), nr + (dr // 2 if abs(dr) == 2 else 0))
            if board[leg[1] * 9 + leg[0]] == ".":
                return True
    forward = -1 if enemy == BLACK else 1  # 對方兵前進的方向
    for f, r in ((kf, kr - forward), (kf - 1, kr), (kf + 1, kr)):
        if on_board(f, r) and board[r * 9 + f] == pawn and (f == kf or not own_side(enemy, r)):
            return True
    return False


def pseudo_moves(board, color):
    """不檢查自己是否被將軍的走法：[(起點格, 終點格)]。"""
    moves = []
    for sq, p in enumerate(board):
        if p == "." or color_of(p) != color:
            continue
        f, r, kind = sq % 9, sq // 9, p.upper()

        def add(tf, tr):
            if on_board(tf, tr):
                t = board[tr * 9 + tf]
                if t == "." or color_of(t) != color:
                    moves.append((sq, tr * 9 + tf))

        if kind == "K":
            for df, dr in ORTHO:
                if in_palace(color, f + df, r + dr):
                    add(f + df, r + dr)
        elif kind == "A":
            for df, dr in DIAG:
                if in_palace(color, f + df, r + dr):
                    add(f + df, r + dr)
        elif kind == "B":
            for df, dr in DIAG:
                tf, tr = f + 2 * df, r + 2 * dr
                if on_board(tf, tr) and own_side(color, tr) and board[(r + dr) * 9 + f + df] == ".":
                    add(tf, tr)
        elif kind == "N":
            for df, dr in KNIGHT:
                lf, lr = (f + df // 2, r) if abs(df) == 2 else (f, r + dr // 2)
                if on_board(f + df, r + dr) and board[lr * 9 + lf] == ".":
                    add(f + df, r + dr)
        elif kind in "RC":
            for df, dr in ORTHO:
                tf, tr, jumped = f + df, r + dr, False
                while on_board(tf, tr):
                    t = board[tr * 9 + tf]
                    if not jumped:
                        if t == ".":
                            add(tf, tr)
                        else:
                            if kind == "R":
                                add(tf, tr)
                                break
                            jumped = True
                    elif t != ".":
                        if color_of(t) != color:
                            add(tf, tr)
                        break
                    tf, tr = tf + df, tr + dr
        elif kind == "P":
            forward = 1 if color == RED else -1
            add(f, r + forward)
            if not own_side(color, r):
                add(f - 1, r)
                add(f + 1, r)
    return moves


def _move_board(board, frm, to):
    b = list(board)
    b[to], b[frm] = b[frm], "."
    return b


def legal_moves(state):
    """輪到的一方所有合法步（ICCS）。對局結束時回傳空清單。"""
    if state.over:
        return []
    color = state.to_move
    return [square(frm % 9, frm // 9) + square(to % 9, to // 9)
            for frm, to in pseudo_moves(state.board, color)
            if not in_check(_move_board(state.board, frm, to), color)]


def _cycle_result(state):
    """同一局面第三次出現：只有一方在循環裡每一步都將軍，這一方判負（長將）；其他情況判和。"""
    key = state.keys[-1]
    start = state.keys.index(key)  # 第一次出現之後的每一步都屬於循環
    steps = len(state.keys) - 1 - start
    mover = state.to_move  # 剛走完的是對方；倒著數，最後一步是 other(mover) 走的
    perpetual = {}
    for color in (RED, BLACK):
        own = [state.checks[-1 - i] for i in range(steps) if (other(mover) if i % 2 == 0 else mover) == color]
        perpetual[color] = bool(own) and all(own)
    if perpetual[RED] != perpetual[BLACK]:
        loser = RED if perpetual[RED] else BLACK
        return other(loser), f"{COLOR_NAMES[loser]}長將（一直將軍造成重複局面）判負，{COLOR_NAMES[other(loser)]}獲勝"
    return None, "同一局面重複三次，判和"


def play(state, action):
    """執行一個行動，回傳新局面；不合法時丟出 ValueError（訊息說明原因）。"""
    if state.over:
        raise ValueError("對局已經結束")
    color = state.to_move
    s = state.copy()
    if action == RESIGN:
        s.actions.append(action)
        s.result = (other(color), f"{COLOR_NAMES[color]}認輸，{COLOR_NAMES[other(color)]}獲勝")
        return s
    move = parse_move(action)
    if move is None:
        raise ValueError(f"走法格式不對：{action}")
    (ff, fr), (tf, tr) = move
    frm, to = fr * 9 + ff, tr * 9 + tf
    piece = state.board[frm]
    if piece == "." or color_of(piece) != color:
        raise ValueError(f"{action[:2]} 沒有{COLOR_NAMES[color]}的棋子")
    if (frm, to) not in pseudo_moves(state.board, color):
        raise ValueError(f"{PIECE_NAMES[piece]}不能這樣走（{action}）")
    after = _move_board(state.board, frm, to)
    if in_check(after, color):
        raise ValueError(f"{action} 走完後自己的{PIECE_NAMES['K' if color == RED else 'k']}會被將軍（或將帥照面），不能走")
    captured = state.board[to] != "."
    s.board = after
    s.to_move = other(color)
    s.actions.append(action)
    s.quiet = 0 if captured else s.quiet + 1
    s.checks.append(in_check(after, s.to_move))
    s.keys.append(s.key())
    if not legal_moves(s):
        how = "將死" if s.checks[-1] else "困斃（沒有合法步可走）"
        s.result = (color, f"{COLOR_NAMES[s.to_move]}被{how}，{COLOR_NAMES[color]}獲勝")
    elif s.keys.count(s.keys[-1]) >= 3:
        s.result = _cycle_result(s)
    elif s.quiet >= NO_CAPTURE_LIMIT:
        s.result = (None, f"連續 {NO_CAPTURE_LIMIT // 2} 回合沒有吃子，判和")
    return s


def is_legal(state, action):
    try:
        play(state, action)
        return True
    except ValueError:
        return False


def perft(state, depth):
    """走法產生器的正確性檢查：depth 層內的所有走法序列數。"""
    if depth == 0:
        return 1
    moves = legal_moves(state)
    if depth == 1:
        return len(moves)
    total = 0
    for m in moves:
        s = state.copy()
        (ff, fr), (tf, tr) = parse_move(m)
        s.board = _move_board(state.board, fr * 9 + ff, tr * 9 + tf)
        s.to_move = other(state.to_move)
        total += perft(s, depth - 1)
    return total


# ---- 中文記法（炮二平五）：紅方用一到九、由紅方右邊數起；黑方用１到９、由黑方右邊數起 ----

def file_number(color, f):
    return RED_NUMS[8 - f] if color == RED else BLACK_NUMS[f]


def chinese(state, action):
    """把合法的 ICCS 走法寫成中文記法，例如 h2e2 → 炮二平五；同一直線有兩三個同種棋子時用前、中、後。"""
    if action == RESIGN:
        return "認輸"
    (ff, fr), (tf, tr) = parse_move(action)
    piece = state.at(ff, fr)
    color, kind = color_of(piece), piece.upper()
    nums = RED_NUMS if color == RED else BLACK_NUMS
    ahead = (tr - fr) * (1 if color == RED else -1)
    same = sorted((r for r in range(10) if state.at(ff, r) == piece), reverse=(color == RED))  # 由前到後
    if len(same) in (2, 3):
        head = ("前後" if len(same) == 2 else "前中後")[same.index(fr)] + PIECE_NAMES[piece]
    else:
        head = PIECE_NAMES[piece] + file_number(color, ff)
    if ahead == 0:
        return f"{head}平{file_number(color, tf)}"
    verb = "進" if ahead > 0 else "退"
    if kind in "NBA":
        return f"{head}{verb}{file_number(color, tf)}"
    return f"{head}{verb}{nums[abs(ahead) - 1]}"


_CANON = str.maketrans({"俥": "車", "傌": "馬", "砲": "炮", "包": "炮", "象": "相", "士": "仕", "將": "帥",
                        "卒": "兵", "帅": "帥", "车": "車", "马": "馬", "进": "進",
                        **{c: str(i + 1) for i, c in enumerate(RED_NUMS)},
                        **{c: str(i + 1) for i, c in enumerate(BLACK_NUMS)}})


def from_chinese(state, text):
    """中文記法 → 輪到的一方對應的合法 ICCS 走法；認不出時回傳 None。紅黑用字與數字寫法不同都可以。"""
    want = text.strip().translate(_CANON)
    for m in legal_moves(state):
        if chinese(state, m).translate(_CANON) == want:
            return m
    return None
