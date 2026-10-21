"""圍棋規則（純程式，不依賴 KataGo）。

規則：禁止自殺、局面超級劫（同一個盤面不能重複出現）、數子（Tromp-Taylor 式：
盤上的子一律算活，連續兩次虛手後照盤面數子，所以終局前要把對方的死子提掉）、貼目由設定決定。
座標用 GTP 慣例：欄 A–T 跳過 I，第 1 列在最下方；內部用 (x, y)，y = 0 是第 1 列。
"""
COLS = "ABCDEFGHJKLMNOPQRST"
EMPTY, BLACK, WHITE = 0, 1, 2
COLOR_NAMES = {BLACK: "黑棋", WHITE: "白棋"}
PASS, RESIGN = "pass", "resign"


def other(color):
    return BLACK + WHITE - color


def coord(x, y):
    return f"{COLS[x]}{y + 1}"


def parse(action, size):
    """座標字串 → (x, y)；格式不對或超出棋盤時回傳 None。"""
    if not action or action[0] not in COLS[:size]:
        return None
    try:
        y = int(action[1:]) - 1
    except ValueError:
        return None
    return (COLS.index(action[0]), y) if 0 <= y < size else None


class GoState:
    """圍棋局面。apply 產生新的局面，不就地修改。"""

    def __init__(self, size, komi):
        self.size = size
        self.komi = komi
        self.board = [[EMPTY] * size for _ in range(size)]
        self.to_move = BLACK
        self.captures = {BLACK: 0, WHITE: 0}  # 各方提掉的子數
        self.passes = 0  # 連續虛手次數
        self.resigned = None  # 認輸的一方
        self.actions = []
        self.seen = {self.key()}  # 出現過的盤面（超級劫）

    def key(self):
        return "".join(str(v) for row in self.board for v in row)

    def copy(self):
        s = GoState.__new__(GoState)
        s.size, s.komi = self.size, self.komi
        s.board = [row[:] for row in self.board]
        s.to_move, s.passes, s.resigned = self.to_move, self.passes, self.resigned
        s.captures = dict(self.captures)
        s.actions = list(self.actions)
        s.seen = set(self.seen)
        return s

    @property
    def over(self):
        return self.resigned is not None or self.passes >= 2


def neighbors(x, y, size):
    for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
        if 0 <= nx < size and 0 <= ny < size:
            yield nx, ny


def group(board, x, y):
    """(x, y) 所在的棋塊與它的氣：回傳 (棋子集合, 氣集合)。"""
    color = board[y][x]
    size = len(board)
    stones, liberties, todo = {(x, y)}, set(), [(x, y)]
    while todo:
        cx, cy = todo.pop()
        for nx, ny in neighbors(cx, cy, size):
            v = board[ny][nx]
            if v == EMPTY:
                liberties.add((nx, ny))
            elif v == color and (nx, ny) not in stones:
                stones.add((nx, ny))
                todo.append((nx, ny))
    return stones, liberties


def groups(board, color):
    """某一方所有棋塊：[(棋子集合, 氣集合)]。"""
    seen, result = set(), []
    for y, row in enumerate(board):
        for x, v in enumerate(row):
            if v == color and (x, y) not in seen:
                g = group(board, x, y)
                seen |= g[0]
                result.append(g)
    return result


def play(state, action):
    """執行一個行動，回傳新局面；不合法時丟出 ValueError（訊息說明原因）。"""
    if state.over:
        raise ValueError("對局已經結束")
    s = state.copy()
    color = s.to_move
    if action == RESIGN:
        s.resigned = color
    elif action == PASS:
        s.passes += 1
    else:
        cell = parse(action, s.size)
        if cell is None:
            raise ValueError(f"座標不對：{action}")
        x, y = cell
        if s.board[y][x] != EMPTY:
            raise ValueError(f"{action} 已經有棋子")
        s.board[y][x] = color
        captured = 0
        for nx, ny in neighbors(x, y, s.size):
            if s.board[ny][nx] == other(color):
                stones, libs = group(s.board, nx, ny)
                if not libs:
                    for gx, gy in stones:
                        s.board[gy][gx] = EMPTY
                    captured += len(stones)
        if not group(s.board, x, y)[1]:
            raise ValueError(f"{action} 是自殺手，不能下")
        key = s.key()
        if key in s.seen:
            raise ValueError(f"{action} 會讓盤面重複（劫），不能馬上下")
        s.seen.add(key)
        s.captures[color] += captured
        s.passes = 0
    s.actions.append(action)
    s.to_move = other(color)
    return s


def is_legal(state, action):
    try:
        play(state, action)
        return True
    except ValueError:
        return False


def legal_points(state):
    return [coord(x, y) for y in range(state.size) for x in range(state.size)
            if state.board[y][x] == EMPTY and is_legal(state, coord(x, y))]


def area_score(board):
    """數子：各方的子數加上只被自己的子圍住的空點。回傳 {BLACK: 分數, WHITE: 分數}。"""
    size = len(board)
    score = {BLACK: 0, WHITE: 0}
    seen = set()
    for y in range(size):
        for x in range(size):
            v = board[y][x]
            if v != EMPTY:
                score[v] += 1
            elif (x, y) not in seen:
                region, borders, todo = {(x, y)}, set(), [(x, y)]
                while todo:
                    cx, cy = todo.pop()
                    for nx, ny in neighbors(cx, cy, size):
                        nv = board[ny][nx]
                        if nv == EMPTY and (nx, ny) not in region:
                            region.add((nx, ny))
                            todo.append((nx, ny))
                        elif nv != EMPTY:
                            borders.add(nv)
                seen |= region
                if len(borders) == 1:
                    score[borders.pop()] += len(region)
    return score


def result(state):
    """終局結果：(勝方, 說明文字)；尚未結束時回傳 None。"""
    if state.resigned is not None:
        winner = other(state.resigned)
        return winner, f"{COLOR_NAMES[state.resigned]}認輸，{COLOR_NAMES[winner]}獲勝"
    if state.passes >= 2:
        score = area_score(state.board)
        margin = score[BLACK] - score[WHITE] - state.komi
        winner = BLACK if margin > 0 else WHITE
        return winner, (f"數子：黑棋 {score[BLACK]}，白棋 {score[WHITE]}＋貼目 {state.komi:g}；"
                        f"{COLOR_NAMES[winner]}勝 {abs(margin):g} 目")
    return None
