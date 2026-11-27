"""五子棋規則與分析引擎。

規則：15x15 棋盤、黑棋先行；預設是自由規則（連成五子或以上即獲勝），黑棋禁手可以在開局設定裡各自打開（Rules）。
落子與所有判斷都由這裡的程式決定；語言模型只負責把這裡產生的事實寫成說明。
"""
from functools import lru_cache
from typing import NamedTuple

from arena.base import ESTIMATE, RULE, SOLVED  # 證據種類（研究計畫表 3-4）

SIZE = 15
EMPTY, BLACK, WHITE = 0, 1, 2
COLS = "ABCDEFGHIJKLMNO"
COLOR_NAMES = {BLACK: "黑棋", WHITE: "白棋"}
# (dx, dy)：y 向上遞增，第 1 列在棋盤最下方
DIRS = ((1, 0), (0, 1), (1, 1), (1, -1))
DIR_NAMES = ("橫向", "直向", "斜向↗", "斜向↘")

NONE, TWO, OPEN_TWO, THREE, OPEN_THREE, FOUR, OPEN_FOUR, FIVE = range(8)
SHAPE_NAMES = {TWO: "眠二", OPEN_TWO: "活二", THREE: "眠三", OPEN_THREE: "活三",
               FOUR: "衝四", OPEN_FOUR: "活四", FIVE: "連五"}
SHAPE_WEIGHTS = {NONE: 0, TWO: 10, OPEN_TWO: 50, THREE: 60, OPEN_THREE: 500,
                 FOUR: 400, OPEN_FOUR: 10000, FIVE: 100000}
# 下一顆子能把棋形升級成什麼，就代表目前是哪一級
_LEVEL_DOWN = {OPEN_FOUR: OPEN_THREE, FOUR: THREE, OPEN_THREE: OPEN_TWO, THREE: TWO}


VCF_NODE_LIMIT = 3000
VCF_MAX_DEPTH = 12
SAFETY_CHECKS = 6  # 選步時檢查前幾名候選，確認下完後對手沒有連續衝四
GAP_MIN_VALUE = 500  # 復盤：最佳著至少達到「活三」等級才比較差距
# 問題檢查分兩級（使用者要求，2026-10-11）：規則或求解確認的戰術錯誤是關鍵時刻；只有引擎估計的「有更好的選擇」是小失誤
LEVELS = {"missed_win": "key", "missed_block": "key", "missed_vcf": "key", "allowed_vcf": "key", "gap": "minor",
          "suggest": "suggest"}
# 「建議」（使用者要求，2026-10-11）：不算失誤，但引擎更推薦另一點——實際下的點不到引擎選擇的 80%（不到一半就是小失誤）
SUGGEST_RATIO, SUGGEST_MIN_VALUE = 0.8, 100
CONTINUATION_PLIES = 3  # 回答提問時，一手棋之後往下推演幾手（對方應、己方再下、對方再應）
FORBIDDEN_DEPTH = 4  # 判斷「真活三」時往下檢查成四那一點是不是禁手的層數上限


class Rules(NamedTuple):
    """黑棋禁手（2026-11-20 使用者要求「黑子禁止雙活三雙活四」，並要三項各自可以設定；預設全關＝原本的自由規則，
    所以沒有這些設定的舊棋局照原規則重播）。白棋不受任何限制。使用者決定的細節：
    - 三三：一手同時形成兩個「真活三」（再下一子就成活四，而且成活四的那一點本身不是禁手）。
    - 四四：只算一手同時形成兩個活四（兩端都空的四），沖四不算（使用者選「只算兩個活四」，不是連珠的四四）。
    - 長連：黑棋六子以上不算贏，那一點也不能下。
    - 同時連成剛好五子時連五優先，算黑棋贏。
    - 禁手點伺服器直接拒絕（畫面標禁止符號），不是下了判負。"""
    three_three: bool = False
    four_four: bool = False
    overline: bool = False

    @property
    def active(self):
        return self.three_three or self.four_four or self.overline

    @classmethod
    def of(cls, settings):
        return cls(**{name: settings.get(name) == "ban" for name in cls._fields})

    def names(self):
        return [FORBIDDEN_NAMES[name] for name in self._fields if getattr(self, name)]


FREESTYLE = Rules()
FORBIDDEN_NAMES = {"three_three": "三三", "four_four": "四四", "overline": "長連"}


def coord(x, y):
    return f"{COLS[x]}{y + 1}"


def other(color):
    return BLACK + WHITE - color


def color_to_move(moves):
    return BLACK if len(moves) % 2 == 0 else WHITE


def new_board():
    return [[EMPTY] * SIZE for _ in range(SIZE)]


def build_board(moves, rules=FREESTYLE):
    """依著手序列（黑先）重建棋盤，回傳 (board, winner)。序列不合法（含黑棋禁手）時丟出 ValueError。"""
    board = new_board()
    winner = None
    for i, move in enumerate(moves):
        if winner:
            raise ValueError(f"第 {i} 手已分出勝負，不能再落子")
        try:
            x, y = int(move[0]), int(move[1])
        except (TypeError, ValueError, IndexError):
            raise ValueError(f"第 {i + 1} 手格式錯誤")
        if not (0 <= x < SIZE and 0 <= y < SIZE):
            raise ValueError(f"第 {i + 1} 手超出棋盤")
        if board[y][x] != EMPTY:
            raise ValueError(f"第 {i + 1} 手 {coord(x, y)} 已有棋子")
        color = BLACK if i % 2 == 0 else WHITE
        ban = forbidden(board, x, y, color, rules)
        if ban:
            raise ValueError(f"第 {i + 1} 手 {coord(x, y)} 是黑棋的禁手（{ban}）")
        board[y][x] = color
        if makes_five(board, x, y, color, rules):
            winner = color
    return board, winner


def _count_run(board, x, y, dx, dy, color):
    """假設 (x, y) 是 color，沿單一方向數連續同色棋子。"""
    n = 1
    for sign in (1, -1):
        cx, cy = x + sign * dx, y + sign * dy
        while 0 <= cx < SIZE and 0 <= cy < SIZE and board[cy][cx] == color:
            n += 1
            cx += sign * dx
            cy += sign * dy
    return n


def _is_five(run, color, rules):
    """連成 run 子算不算贏：禁長連時黑棋要剛好五子。"""
    return run == 5 if color == BLACK and rules.overline else run >= 5


def makes_five(board, x, y, color, rules=FREESTYLE):
    return any(_is_five(_count_run(board, x, y, dx, dy, color), color, rules) for dx, dy in DIRS)


# ---- 黑棋禁手 ----

def forbidden(board, x, y, color, rules=FREESTYLE, depth=0):
    """color 下在空點 (x, y) 是不是禁手：是的話回傳「三三」「四四」或「長連」，否則 None。
    只有黑棋有禁手；連成剛好五子時連五優先（使用者決定），不算禁手。"""
    if color != BLACK or not rules.active or board[y][x] != EMPTY:
        return None
    runs = [_count_run(board, x, y, dx, dy, BLACK) for dx, dy in DIRS]
    if any(_is_five(r, BLACK, rules) for r in runs):
        return None
    if rules.overline and max(runs) >= 6:
        return FORBIDDEN_NAMES["overline"]
    # 先粗篩（選步時每個候選點都要判斷，這裡最花時間）：活三要同一條線上另有 2 顆黑子、活四要 3 顆，至少兩個方向都夠才可能是禁手
    near = sorted((sum(board[cy][cx] == BLACK for _, cx, cy in _line_points(x, y, dx, dy)) for dx, dy in DIRS),
                  reverse=True)
    if near[1] < (2 if rules.three_three else 3):
        return None
    board[y][x] = BLACK  # 暫時放上，判斷完一定拿掉
    try:
        if rules.four_four and sum(_open_four(board, x, y, dx, dy, rules) for dx, dy in DIRS) >= 2:
            return FORBIDDEN_NAMES["four_four"]
        if (rules.three_three and depth < FORBIDDEN_DEPTH
                and sum(_live_three(board, x, y, dx, dy, rules, depth) for dx, dy in DIRS) >= 2):
            return FORBIDDEN_NAMES["three_three"]
    finally:
        board[y][x] = EMPTY
    return None


def legal(board, x, y, color, rules=FREESTYLE):
    return board[y][x] == EMPTY and not forbidden(board, x, y, color, rules)


def forbidden_points(board, rules=FREESTYLE):
    """黑棋現在所有的禁手點 [(x, y), ...]。禁手一定要用到附近 3 格內的黑子，只檢查那些點。"""
    if not rules.active:
        return []
    return [p for p in _near_cells(board, 3, BLACK) if forbidden(board, *p, BLACK, rules)]


def has_legal_move(board, color, rules=FREESTYLE):
    """還有沒有能下的點（黑棋所有空點都是禁手時沒有；實際上幾乎不會發生，找到第一個就停）。"""
    return any(legal(board, x, y, color, rules) for y in range(SIZE) for x in range(SIZE))


def _run_span(board, x, y, dx, dy, color):
    """(x, y) 所在的同色連續棋子往負方向、正方向各延伸幾格（不含 (x, y)）。"""
    span = []
    for sign in (-1, 1):
        n, cx, cy = 0, x + sign * dx, y + sign * dy
        while 0 <= cx < SIZE and 0 <= cy < SIZE and board[cy][cx] == color:
            n += 1
            cx += sign * dx
            cy += sign * dy
        span.append(n)
    return span


def _open_four(board, x, y, dx, dy, rules):
    """(x, y) 上已有黑子：它在這個方向是不是活四（剛好四子相連，兩端都是空點，下任一端都連五）。"""
    back, ahead = _run_span(board, x, y, dx, dy, BLACK)
    if back + ahead + 1 != 4:
        return False
    for k in (-back - 1, ahead + 1):
        ex, ey = x + k * dx, y + k * dy
        if not (0 <= ex < SIZE and 0 <= ey < SIZE and board[ey][ex] == EMPTY):
            return False
        if not _is_five(_count_run(board, ex, ey, dx, dy, BLACK), BLACK, rules):
            return False  # 禁長連時，再下一子會變六子的那一端不算
    return True


def _line_points(x, y, dx, dy):
    for k in range(-4, 5):
        cx, cy = x + k * dx, y + k * dy
        if k and 0 <= cx < SIZE and 0 <= cy < SIZE:
            yield k, cx, cy


def _live_three(board, x, y, dx, dy, rules, depth):
    """(x, y) 上已有黑子：它在這個方向是不是「真活三」：再下一子能成包含 (x, y) 的活四，而且那一子不是禁手。
    已經是四（這條線上有連五點）的不算三。"""
    empties = [(k, cx, cy) for k, cx, cy in _line_points(x, y, dx, dy) if board[cy][cx] == EMPTY]
    if any(_is_five(_count_run(board, cx, cy, dx, dy, BLACK), BLACK, rules) for _, cx, cy in empties):
        return False
    for k, cx, cy in empties:
        board[cy][cx] = BLACK
        back, ahead = _run_span(board, cx, cy, dx, dy, BLACK)
        four = -back <= -k <= ahead and _open_four(board, cx, cy, dx, dy, rules)  # 活四必須包含 (x, y)
        board[cy][cx] = EMPTY
        if four and not forbidden(board, cx, cy, BLACK, rules, depth + 1):
            return True
    return False


# ---- 棋形判斷 ----

def _line(board, x, y, dx, dy, color):
    """以 (x, y) 為中心取前後各 4 格。X=己方（中心視為己方）、_=空、O=對方或棋盤外。"""
    chars = []
    for k in range(-4, 5):
        if k == 0:
            chars.append("X")
            continue
        cx, cy = x + k * dx, y + k * dy
        if 0 <= cx < SIZE and 0 <= cy < SIZE:
            v = board[cy][cx]
            chars.append("_" if v == EMPTY else "X" if v == color else "O")
        else:
            chars.append("O")
    return "".join(chars)


def _run_has_five(line):
    i = j = 4
    while i > 0 and line[i - 1] == "X":
        i -= 1
    while j < len(line) - 1 and line[j + 1] == "X":
        j += 1
    return j - i + 1 >= 5


def _put(line, i):
    return line[:i] + "X" + line[i + 1:]


@lru_cache(maxsize=None)
def _shape(line):
    """中心那顆子在這條線上形成的棋形。所有棋形都以「能否連成包含中心的五子」來定義。"""
    if _run_has_five(line):
        return FIVE
    if line.count("X") < 2:
        return NONE
    wins = sum(1 for i, ch in enumerate(line) if ch == "_" and _run_has_five(_put(line, i)))
    if wins >= 2:
        return OPEN_FOUR
    if wins == 1:
        return FOUR
    best = NONE
    for i, ch in enumerate(line):
        if ch == "_":
            best = max(best, _LEVEL_DOWN.get(_shape(_put(line, i)), NONE))
    return best


def shapes_at(board, x, y, color):
    """假設 color 下在 (x, y)，四個方向各自形成的棋形。"""
    return [_shape(_line(board, x, y, dx, dy, color)) for dx, dy in DIRS]


def _shapes_score(shapes):
    s = sorted(shapes, reverse=True)
    if s[0] == FIVE:
        return SHAPE_WEIGHTS[FIVE]
    if s[0] == OPEN_FOUR or (s[0] == FOUR and s[1] >= OPEN_THREE):  # 活四、雙四、四三
        return SHAPE_WEIGHTS[OPEN_FOUR]
    if s[0] == OPEN_THREE and s[1] == OPEN_THREE:  # 雙活三
        return SHAPE_WEIGHTS[OPEN_FOUR] // 2
    return sum(SHAPE_WEIGHTS[v] for v in shapes)


def point_value(board, x, y, color, rules=FREESTYLE):
    """引擎估計：進攻價值加上阻擋對手的價值。對手（黑棋）在這裡是禁手時它本來就不能下，不算阻擋的價值。"""
    attack = _shapes_score(shapes_at(board, x, y, color))
    if forbidden(board, x, y, other(color), rules):
        return attack
    defense = _shapes_score(shapes_at(board, x, y, other(color)))
    return attack + defense * 0.9


def _windows(x, y, dx, dy):
    """經過 (x, y)、方向 (dx, dy) 的每一段 5 格。"""
    for start in range(-4, 1):
        cells = [(x + (start + k) * dx, y + (start + k) * dy) for k in range(5)]
        if all(0 <= a < SIZE and 0 <= b < SIZE for a, b in cells):
            yield cells


def five_open_at(board, x, y, color):
    """還有沒有一段經過 (x, y)、只有 color 的棋子或空點的 5 格：沒有的話，color 再怎麼下都不會經過這裡連成五子。"""
    return any(all(board[b][a] in (EMPTY, color) for a, b in cells)
               for dx, dy in DIRS for cells in _windows(x, y, dx, dy))


def dead_board_fact(board):
    """黑白雙方在整個棋盤上都已經不可能連成五子時，寫成規則確認的事實；還有可能時回傳 None。
    2026-11-27 使用者回報第 295 盤：第 190 手起雙方都連不成五，引擎仍把每個點寫成「延伸佈局」，
    AI 只能說「根據引擎評估，O2 是最好的候選」，看起來只是在抄引擎。"""
    empty = [(x, y) for y in range(SIZE) for x in range(SIZE) if board[y][x] == EMPTY]
    if any(five_open_at(board, x, y, c) for x, y in empty for c in (BLACK, WHITE)):
        return None
    return (RULE, "棋盤上已經沒有任何一段能讓黑棋或白棋連成五子：這盤會是和局，接下來下在哪裡都不會改變結果。")


def move_labels(board, x, y, color, rules=FREESTYLE):
    """把一手棋的作用寫成短句，例如「形成活三（橫向）」。"""
    opp = other(color)
    if not five_open_at(board, x, y, color) and not five_open_at(board, x, y, opp):
        return ["這裡雙方都已經連不成五子"]  # 棋形再好看也沒有用，不要寫成「延伸佈局」
    ban = forbidden(board, x, y, opp, rules)
    labels = []
    for name, mine, theirs in zip(DIR_NAMES, shapes_at(board, x, y, color), shapes_at(board, x, y, opp)):
        if mine >= OPEN_TWO:
            labels.append(f"形成{SHAPE_NAMES[mine]}（{name}）")
        if theirs >= THREE and not ban:
            labels.append(f"阻止{COLOR_NAMES[opp]}在此形成{SHAPE_NAMES[theirs]}（{name}）")
    if ban:
        labels.append(f"這裡是{COLOR_NAMES[opp]}的禁手（{ban}），{COLOR_NAMES[opp]}本來就不能下")
    return labels or ["延伸佈局"]


# ---- 候選與搜尋 ----

def _near_cells(board, radius, color=None):
    """距離指定棋子（color=None 時為任何棋子）radius 格內的空點。"""
    cells = set()
    for y in range(SIZE):
        for x in range(SIZE):
            v = board[y][x]
            if v == EMPTY or (color is not None and v != color):
                continue
            for ny in range(max(0, y - radius), min(SIZE, y + radius + 1)):
                for nx in range(max(0, x - radius), min(SIZE, x + radius + 1)):
                    if board[ny][nx] == EMPTY:
                        cells.add((nx, ny))
    return sorted(cells)


def candidates(board, color=None, rules=FREESTYLE):
    """候選點：棋子附近兩格內的空點，去掉 color 的禁手。"""
    cells = _near_cells(board, 2)
    if not cells and board[SIZE // 2][SIZE // 2] == EMPTY:
        return [(SIZE // 2, SIZE // 2)]
    if color is None or not rules.active:
        return cells
    allowed = [p for p in cells if not forbidden(board, *p, color, rules)]
    if not allowed:  # 附近全是禁手時，改找棋盤上其他能下的點
        allowed = [(x, y) for y in range(SIZE) for x in range(SIZE) if legal(board, x, y, color, rules)]
    return allowed


def five_points(board, color, rules=FREESTYLE):
    """color 下一手就能連五的空點（禁長連時黑棋六子以上不算）。"""
    return [p for p in _near_cells(board, 1) if makes_five(board, p[0], p[1], color, rules)]


def _win_points_after(board, x, y, color, rules=FREESTYLE):
    """(x, y) 已放上 color 後，經過它的四條線上能連五的空點。"""
    points = set()
    for dx, dy in DIRS:
        if _shape(_line(board, x, y, dx, dy, color)) < FOUR:
            continue
        for k in range(-4, 5):
            cx, cy = x + k * dx, y + k * dy
            if (0 <= cx < SIZE and 0 <= cy < SIZE and board[cy][cx] == EMPTY
                    and _is_five(_count_run(board, cx, cy, dx, dy, color), color, rules)):
                points.add((cx, cy))
    return points


def _blocked_by_ban(board, wins, color, rules):
    """color 剛衝四、只剩一個連五點 wins 時，對手（黑棋）能不能擋：那一點是對手的禁手就擋不住，回傳禁手名稱。"""
    if len(wins) != 1:
        return None
    (wx, wy), = wins
    return forbidden(board, wx, wy, other(color), rules)


def _placed(board, x, y, color):
    b = [row[:] for row in board]
    b[y][x] = color
    return b


def find_vcf(board, color, node_limit=VCF_NODE_LIMIT, rules=FREESTYLE):
    """搜尋連續衝四取勝（VCF）。

    找到時回傳手順 [己方, 對方被迫的擋點, 己方, ...]，最後一手之後對方已擋不住（擋不完，或唯一的擋點是黑棋的禁手）。
    黑棋的每一手都不能是禁手。回傳 None 只代表「在搜尋上限內未發現」，不代表確定沒有。
    """
    wins = five_points(board, color, rules)
    if wins:
        return [wins[0]]
    b = [row[:] for row in board]
    return _vcf(b, color, set(five_points(b, other(color), rules)), [node_limit], 0, rules)


def _vcf(b, color, opp_fives, budget, depth, rules):
    if budget[0] <= 0 or depth >= VCF_MAX_DEPTH or len(opp_fives) > 1:
        return None
    budget[0] -= 1
    opp = other(color)
    # 對手若已有連五點，這一手必須擋在那裡，而且要同時衝四才能繼續
    moves = sorted(opp_fives) if opp_fives else _near_cells(b, 2, color)
    for x, y in moves:
        if max(shapes_at(b, x, y, color)) < FOUR or forbidden(b, x, y, color, rules):
            continue
        b[y][x] = color
        wins = _win_points_after(b, x, y, color, rules)
        if not wins:  # 禁長連時黑棋的「四」可能只能連成六子，不算四
            b[y][x] = EMPTY
            continue
        if len(wins) >= 2 or _blocked_by_ban(b, wins, color, rules):  # 活四或雙四，或唯一的擋點是禁手：對手擋不住
            b[y][x] = EMPTY
            return [(x, y)]
        (wx, wy), = wins
        b[wy][wx] = opp
        rest = None
        if not makes_five(b, wx, wy, opp, rules):
            rest = _vcf(b, color, _win_points_after(b, wx, wy, opp, rules), budget, depth + 1, rules)
        b[wy][wx] = EMPTY
        b[y][x] = EMPTY
        if rest is not None:
            return [(x, y), (wx, wy)] + rest
    return None


def vcf_text(name, line, board, color, rules=FREESTYLE):
    """把連續衝四手順寫成主詞清楚的句子，避免語言模型混淆是哪一方的棋。board 是搜尋的局面，color 是 name 那一方。
    find_vcf 在已經能連五時也只回傳那一點：要分開寫，否則「下一手就連五」會寫成「形成活四或雙四」（2026-11-16 情境測試）。"""
    if len(line) == 1 and makes_five(board, *line[0], color, rules):
        return f"{name}下 {coord(*line[0])} 就能直接連成五子"
    if len(line) == 1:
        wins = _win_points_after(_placed(board, *line[0], color), *line[0], color, rules)
        ban = _blocked_by_ban(_placed(board, *line[0], color), wins, color, rules)
        if ban:
            (wx, wy), = wins
            return (f"{name}下 {coord(*line[0])} 衝四，對方只能擋在 {coord(wx, wy)}，"
                    f"但那裡是{COLOR_NAMES[other(color)]}的禁手（{ban}），擋不住")
        return f"{name}下 {coord(*line[0])} 就形成活四或雙四，對方擋不完"
    steps = "→".join(coord(x, y) for x, y in line)
    return f"{name}可以從 {coord(*line[0])} 開始連續衝四取勝（{steps}）"


def ranked_candidates(board, color, rules=FREESTYLE):
    """依引擎估計由好到壞排列候選點（不含禁手）；同分時選比較靠近中央的點。"""
    center = SIZE // 2
    return sorted(candidates(board, color, rules), key=lambda p: (point_value(board, *p, color, rules),
                                                                  -max(abs(p[0] - center), abs(p[1] - center))),
                  reverse=True)


def choose_move(board, color, rules=FREESTYLE):
    """決定 color 的下一手，回傳 {"move", "reason", "points"}；points 是附證據種類的事實。

    AI 對局由 Agent 下（見 agent.py），這裡是 Agent 失敗時的代下、復盤與提問的標準。黑棋一定不會選禁手。
    """
    me, opp = COLOR_NAMES[color], COLOR_NAMES[other(color)]
    if not any(any(row) for row in board):
        x, y = candidates(board)[0]
        return _decision((x, y), "opening", [
            (ESTIMATE, f"第一手下在天元 {coord(x, y)}，四個方向的發展空間最大。")])

    wins = five_points(board, color, rules)
    if wins:
        x, y = wins[0]
        return _decision(wins[0], "win", [(RULE, f"{me}下在 {coord(x, y)} 連成五子，獲勝。")])

    blocks = five_points(board, other(color), rules)
    banned = {p: forbidden(board, *p, color, rules) for p in blocks}
    allowed = [p for p in blocks if not banned[p]]
    if allowed:
        best = max(allowed, key=lambda p: point_value(board, *p, color, rules))
        points = [(RULE, f"{opp}在 {coord(*best)} 有連五的機會，{me}必須先擋住。")]
        if len(blocks) > 1:
            others = "、".join(coord(*p) for p in blocks if p != best)
            points.append((RULE, f"但{opp}還可以在 {others} 連五，這一手已經擋不完。"))
        return _decision(best, "block_five", points)
    if blocks:  # 唯一的擋點都是自己的禁手：擋不住，下引擎估計最好的一點
        best = ranked_candidates(board, color, rules)[0]
        return _decision(best, "no_defense", [
            (RULE, f"{opp}在 {coord(*p)} 有連五的機會，但那裡是{me}的禁手（{banned[p]}），{me}不能擋。")
            for p in blocks] + [
            (ESTIMATE, f"依引擎估計，{coord(*best)} 的作用：{'、'.join(move_labels(board, *best, color, rules))}。")])

    vcf = find_vcf(board, color, rules=rules)
    if vcf:
        return _decision(vcf[0], "vcf", [
            (SOLVED, f"在搜尋範圍內已確認，{vcf_text(me, vcf, board, color, rules)}。")])

    ranked = ranked_candidates(board, color, rules)
    threat = find_vcf(board, other(color), rules=rules)
    for p in ranked[:SAFETY_CHECKS]:
        if find_vcf(_placed(board, *p, color), other(color), rules=rules) is None:
            points = []
            if threat:
                points.append((SOLVED, f"在搜尋範圍內已確認，如果不處理，"
                                       f"{vcf_text(opp, threat, board, other(color), rules)}。"))
                points.append((ESTIMATE, f"{me}下在 {coord(*p)}，在搜尋範圍內未發現{opp}還能連續衝四。"))
            points.append((ESTIMATE, f"依引擎估計，{coord(*p)} 的作用：{'、'.join(move_labels(board, *p, color, rules))}。"))
            alts = [q for q in ranked[:3] if q != p][:2]
            if alts:
                alt_text = "；".join(f"{coord(*q)}（{'、'.join(move_labels(board, *q, color, rules))}）" for q in alts)
                points.append((ESTIMATE, f"其他考慮過的候選：{alt_text}。"))
            return _decision(p, "defend_vcf" if threat else "heuristic", points)

    best = ranked[0]
    points = [(ESTIMATE, f"依引擎估計，{coord(*best)} 的作用：{'、'.join(move_labels(board, *best, color, rules))}。")]
    if threat:
        points.insert(0, (SOLVED, f"在搜尋範圍內已確認，{vcf_text(opp, threat, board, other(color), rules)}，"
                                  f"{me}前幾名候選都擋不住。"))
    return _decision(best, "no_defense", points)


def _decision(move, reason, points):
    return {"move": list(move), "reason": reason,
            "points": [{"evidence": ev, "text": text} for ev, text in points]}


# ---- 復盤 ----

def judge_move(board, x, y, color, rules=FREESTYLE):
    """判斷 color 在 board 上下 (x, y) 是否為關鍵失誤；不是時回傳 None。

    只有在能指出具體證據時才判定；局面已經擋不住時，不把敗因歸給這一手。
    黑棋唯一的擋點是自己的禁手時本來就擋不住，也不怪罪。
    """
    opp = other(color)
    me, them = COLOR_NAMES[color], COLOR_NAMES[opp]
    played = coord(x, y)

    wins = five_points(board, color, rules)
    if wins:
        if (x, y) in wins:
            return None
        return _moment("missed_win", "錯過直接連五", (x, y), wins[0], [
            (RULE, f"這時{me}在 {coord(*wins[0])} 就能連成五子，但下了 {played}。")])

    blocks = five_points(board, opp, rules)
    if len(blocks) == 1 and (x, y) not in blocks and not forbidden(board, *blocks[0], color, rules):
        return _moment("missed_block", "沒有擋住對手的衝四", (x, y), blocks[0], [
            (RULE, f"{them}已經衝四，下一手可以在 {coord(*blocks[0])} 連五；{played} 沒有擋住。")])
    if blocks:
        return None  # 擋了，或是雙四已經擋不完，或擋點是自己的禁手

    vcf = find_vcf(board, color, rules=rules)
    if vcf:
        if _keeps_vcf(board, x, y, color, rules):
            return None
        return _moment("missed_vcf", "錯過連續衝四取勝", (x, y), vcf[0], [
            (SOLVED, f"在搜尋範圍內已確認，這時{vcf_text(me, vcf, board, color, rules)}。"),
            (ESTIMATE, f"下了 {played} 之後，在搜尋範圍內未找到同樣的取勝手順。")])

    after = _placed(board, x, y, color)
    threat = find_vcf(after, opp, rules=rules)
    if threat:
        best = tuple(choose_move(board, color, rules)["move"])
        if best != (x, y) and find_vcf(_placed(board, *best, color), opp, rules=rules) is None:
            return _moment("allowed_vcf", "讓對手可以連續衝四", (x, y), best, [
                (SOLVED, f"在搜尋範圍內已確認，{me}下 {played} 之後，{vcf_text(them, threat, after, opp, rules)}。"),
                (ESTIMATE, f"如果{me}改下 {coord(*best)}，在搜尋範圍內未發現{them}還能連續衝四。")])
        return None  # 前幾名候選都擋不住，敗勢不是這一手造成的

    played_value = point_value(board, x, y, color, rules)
    top_value = max(point_value(board, *p, color, rules) for p in candidates(board, color, rules))
    if top_value < SUGGEST_MIN_VALUE or played_value >= top_value * SUGGEST_RATIO:
        return None  # 沒有明顯更好的點
    # 建議必須是 AI 自己也會下的那一手，避免建議一手被安全檢查排除的棋
    decision = choose_move(board, color, rules)
    best = tuple(decision["move"])
    if decision["reason"] == "no_defense" or best == (x, y):
        return None
    best_value = point_value(board, *best, color, rules)
    facts = [(ESTIMATE, f"依引擎估計，{coord(*best)} 的作用：{'、'.join(move_labels(board, *best, color, rules))}。"),
             (ESTIMATE, f"實際下的 {played}：{'、'.join(move_labels(board, x, y, color, rules))}。")]
    if top_value >= GAP_MIN_VALUE and played_value * 2 < best_value:
        return _moment("gap", "有明顯更好的選擇", (x, y), best, facts)
    if played_value < best_value * SUGGEST_RATIO:
        return _moment("suggest", f"建議：引擎推薦 {coord(*best)}", (x, y), best, facts + [
            (ESTIMATE, f"差距不大，不算失誤；{coord(*best)} 是引擎比較推薦的下法。")])
    return None


def _keeps_vcf(board, x, y, color, rules=FREESTYLE):
    """下了 (x, y) 之後，是否仍走在連續衝四取勝的路上。"""
    if forbidden(board, x, y, color, rules):
        return False
    b = _placed(board, x, y, color)
    wins = _win_points_after(b, x, y, color, rules)
    if not wins:
        return False  # 連續衝四的每一手都必須是四
    if len(wins) >= 2 or _blocked_by_ban(b, wins, color, rules):
        return True
    wx, wy = wins.pop()
    b[wy][wx] = other(color)
    return not makes_five(b, wx, wy, other(color), rules) and find_vcf(b, color, rules=rules) is not None


def _moment(kind, title, played, best, points):
    return {"type": kind, "level": LEVELS[kind], "title": title, "played": list(played), "best": list(best),
            "points": [{"evidence": ev, "text": text} for ev, text in points]}


def rules_fact(rules):
    """這盤用的規則，寫進提問的事實，避免模型照自由規則回答；自由規則回傳 None。"""
    if not rules.active:
        return None
    return (RULE, f"這盤黑棋有禁手：{'、'.join(rules.names())}（禁手點黑棋不能下；連成剛好五子時連五優先），白棋沒有禁手。")


def explain_position(board, focus=(), next_move=None, rules=FREESTYLE):
    """回答玩家提問用：整理目前局面的事實。

    focus 是玩家問到的點（最多 3 個），next_move 是棋譜中實際的下一手（沒有則為 None）。
    """
    color = BLACK if sum(v != EMPTY for row in board for v in row) % 2 == 0 else WHITE
    opp = other(color)
    me, them = COLOR_NAMES[color], COLOR_NAMES[opp]
    points = [(RULE, f"目前輪到{me}下。")]
    if rules_fact(rules):
        points.append(rules_fact(rules))
    if dead_board_fact(board):
        points.append(dead_board_fact(board))

    decision = choose_move(board, color, rules)
    best = tuple(decision["move"])
    first = decision["points"][0]
    points.append((first["evidence"], f"引擎會建議{me}下 {coord(*best)}，理由如下。"))
    points += [(p["evidence"], p["text"]) for p in decision["points"]]
    if decision["reason"] not in ("block_five", "defend_vcf", "no_defense"):
        threat = find_vcf(board, opp, rules=rules)
        if threat:
            points.append((SOLVED, f"在搜尋範圍內已確認，如果輪到{them}下，{vcf_text(them, threat, board, opp, rules)}。"))
        else:
            points.append((ESTIMATE, f"在搜尋範圍內未發現{them}目前有連續衝四。"))

    points += continuation(board, *best, color, rules=rules)
    for x, y in focus[:3]:
        points += check_point(board, x, y, color, rules)
        if (x, y) == best:
            points.append((ESTIMATE, f"{coord(x, y)} 就是引擎建議的那一手。"))
        else:
            points += continuation(board, x, y, color, rules=rules)

    if next_move is not None:
        points += _move_facts(board, *next_move, color, "棋譜中實際的下一手", rules)

    points = list(dict.fromkeys(points))  # 問到的點、實際的下一手和引擎建議常是同一點，後續變化不重複列
    return [{"evidence": ev, "text": text} for ev, text in points]


def check_point(board, x, y, color, rules=FREESTYLE):
    """檢查 color 下在 (x, y) 的事實，回傳 [(證據種類, 文字)]。提問與 Agent 的 check_move 工具共用。"""
    opp = other(color)
    me, them = COLOR_NAMES[color], COLOR_NAMES[opp]
    c = coord(x, y)
    if board[y][x] != EMPTY:
        return [(RULE, f"{c} 已經有{COLOR_NAMES[board[y][x]]}的棋子。")]
    ban = forbidden(board, x, y, color, rules)
    if ban:
        return [(RULE, f"{me}不能下 {c}：這一點是{me}的禁手（{ban}）。"),
                (ESTIMATE, f"如果換{them}下 {c}：{'、'.join(move_labels(board, x, y, opp, rules))}。")]
    points = [(ESTIMATE, f"如果{me}下 {c}：{'、'.join(move_labels(board, x, y, color, rules))}。"),
              (ESTIMATE, f"如果換{them}下 {c}：{'、'.join(move_labels(board, x, y, opp, rules))}。")]
    blocks = five_points(board, opp, rules)
    if makes_five(board, x, y, color, rules):
        points.append((RULE, f"{me}下 {c} 會連成五子。"))
    elif blocks and (x, y) not in blocks:
        points.append((RULE, f"{them}已經衝四，{me}下 {c} 沒有擋住 {coord(*blocks[0])}，{them}下一手就能連五。"))
    else:
        after = _placed(board, x, y, color)
        threat = find_vcf(after, opp, rules=rules)
        if threat:
            points.append((SOLVED, f"在搜尋範圍內已確認，{me}下 {c} 之後，{vcf_text(them, threat, after, opp, rules)}。"))
        else:
            points.append((ESTIMATE, f"{me}下 {c} 之後，在搜尋範圍內未發現{them}能連續衝四。"))
    return points


def continuation(board, x, y, color, plies=CONTINUATION_PLIES, rules=FREESTYLE):
    """回答「為什麼」用：color 下 (x, y) 之後的後續變化，回傳 [(證據種類, 文字)]。
    2026-11-18 使用者回報：AI 教練只說「依引擎估計 I7 比較好、H5 效果較弱」，沒有說明為什麼。原本的事實只有一手棋
    當下形成什麼棋形，沒有下完之後會發生什麼，模型只能重複引擎的結論。這裡補上對方怎麼應、應完之後各得到什麼：
    衝四逼對方擋的那一手是規則確認；之後雙方各下引擎會選的一手（choose_move，和 AI 對局、復盤同一個標準）是引擎估計。"""
    opp = other(color)
    me, them = COLOR_NAMES[color], COLOR_NAMES[opp]
    c = coord(x, y)
    if (board[y][x] != EMPTY or makes_five(board, x, y, color, rules) or five_points(board, opp, rules)
            or forbidden(board, x, y, color, rules)):
        return []  # 已經有子、直接連五、對方已經衝四或是禁手（check_point 已寫明），沒有後續可談
    b = _placed(board, x, y, color)
    points = []
    wins = _win_points_after(b, x, y, color, rules)
    if len(wins) >= 2:
        return [(RULE, f"{me}下 {c} 之後有 {'、'.join(coord(*p) for p in sorted(wins))} 好幾個連五點，{them}擋不完。")]
    ban = _blocked_by_ban(b, wins, color, rules)
    if ban:
        (bx, by), = wins
        return [(RULE, f"{me}下 {c} 是衝四，{them}只能擋在 {coord(bx, by)}，但那裡是{them}的禁手（{ban}），"
                       f"{them}擋不住，{me}下一手就連五。")]
    side = opp
    if wins:  # 衝四：對方只能擋在連五點
        (bx, by), = wins
        block = coord(bx, by)
        points.append((RULE, f"{me}下 {c} 是衝四，{them}下一手只能擋在 {block}，否則{me}就連五。"))
        gains = [label for label in move_labels(b, bx, by, opp, rules) if "連五" not in label]  # 擋住連五是一定的，不用再說
        points.append((ESTIMATE, f"{them}擋在 {block} 這一手同時：{'、'.join(gains)}。" if gains
                       else f"依引擎估計，{them}擋在 {block} 這一手只是擋住，沒有幫自己形成棋形。"))
        b = _placed(b, bx, by, opp)
        if not _win_points_after(b, x, y, color, rules):
            points.append((RULE, f"{them}擋住 {block} 之後，{me}在 {c} 的這個四就用掉了，沒有連五的點，"
                                 f"又輪到{me}下。"))
        side, plies = color, plies - 1
        start = f"{them}擋住 {block} 之後"
    else:
        start = f"{me}下 {c} 之後"
    line = []
    for _ in range(plies):
        name = COLOR_NAMES[side]
        if not candidates(b, side, rules):
            break  # 棋盤下滿了（快下滿時問「為什麼」，原本在這裡找不到點而出錯，2026-11-27）
        decision = choose_move(b, side, rules)
        mx, my = decision["move"]
        if makes_five(b, mx, my, side, rules):
            line.append(f"{name}下 {coord(mx, my)} 連成五子")
            break
        line.append(f"{name}下 {coord(mx, my)}（{'、'.join(move_labels(b, mx, my, side, rules))}）")
        b = _placed(b, mx, my, side)
        side = other(side)
    if line:
        points.append((ESTIMATE, f"依引擎估計，{start}的後續：" + "，接著".join(line) + "。"))
    return points


def explain_move(moves, n, rules=FREESTYLE):
    """回答提問用：棋譜第 n 手（從 1 起算）的事實，供玩家用「第 n 手」提問時使用。"""
    board, _ = build_board(moves[:n - 1], rules)
    x, y = moves[n - 1]
    color = BLACK if (n - 1) % 2 == 0 else WHITE
    return [{"evidence": ev, "text": text} for ev, text in _move_facts(board, x, y, color, f"第 {n} 手", rules)]


def _move_facts(board, x, y, color, name, rules=FREESTYLE):
    points = [(RULE, f"{name}是{COLOR_NAMES[color]} {coord(x, y)}。")]
    if rules_fact(rules):
        points.append(rules_fact(rules))
    if dead_board_fact(board):
        points.append(dead_board_fact(board))
    m = judge_move(board, x, y, color, rules)
    if m:
        label = {"key": "關鍵時刻", "minor": "小失誤", "suggest": "建議"}[m["level"]]
        points.append((m["points"][0]["evidence"], f"引擎把{name}列為{label}：{m['title']}。"))
        points += [(p["evidence"], p["text"]) for p in m["points"]]
    else:
        points.append((ESTIMATE, f"引擎沒有在{name}找到問題。"))
    points += continuation(board, x, y, color, rules=rules)
    if m and m.get("best") and tuple(m["best"]) != (x, y):  # 和引擎建議的下法比：各自下完之後怎麼發展
        points += continuation(board, *m["best"], color, rules=rules)
    return points


def style_points(moves, color, rules=FREESTYLE):
    """整盤下法的統計（提問「我的策略是什麼」用，2026-11-27）：color 的每一手是進攻（自己形成活三以上）
    還是防守（擋住對方眠三以上），以及衝四、活三的次數。偏向哪邊由程式依次數判斷，模型只改寫。"""
    board = new_board()
    total = attack = defend = both = fours = threes = 0
    for i, (x, y) in enumerate(moves):
        mover = BLACK if i % 2 == 0 else WHITE
        if mover == color:
            mine = shapes_at(board, x, y, color)
            ban = forbidden(board, x, y, other(color), rules)  # 對方本來就不能下的點不算擋
            attacks = max(mine) >= OPEN_THREE
            defends = not ban and max(shapes_at(board, x, y, other(color))) >= THREE
            total += 1
            attack += attacks
            defend += defends
            both += attacks and defends
            fours += any(s in (FOUR, OPEN_FOUR) for s in mine)
            threes += OPEN_THREE in mine
        board[y][x] = mover
    if not total:
        return []
    name = COLOR_NAMES[color]
    if attack >= 3 and attack >= 2 * defend:
        lean = "偏向進攻：形成自己棋形的手明顯比擋對方的多"
    elif defend >= 3 and defend >= 2 * attack:
        lean = "偏向防守：擋對方的手明顯比形成自己棋形的多"
    else:
        lean = "攻守都有，沒有明顯偏向哪一邊"
    return [(RULE, f"你（{name}）這盤下了 {total} 手：自己形成活三以上的進攻手 {attack} 手，"
                   f"擋住對方眠三以上的防守手 {defend} 手（其中 {both} 手同時做到），其餘 {total - attack - defend + both} 手兩者都不是。"),
            (RULE, f"你下出衝四或活四 {fours} 次、活三 {threes} 次。"),
            (ESTIMATE, f"依這些次數，你這盤的下法{lean}（進攻手至少是防守手的兩倍才算偏向進攻，反過來算偏向防守）。")]


def review(moves, color, rules=FREESTYLE):
    """找出 color 方的所有問題（關鍵時刻與小失誤），依手數排序。"""
    board = new_board()
    moments = []
    for i, (x, y) in enumerate(moves):
        mover = BLACK if i % 2 == 0 else WHITE
        if mover == color:
            m = judge_move(board, x, y, color, rules)
            if m:
                m["index"] = i
                moments.append(m)
        board[y][x] = mover
    return moments
