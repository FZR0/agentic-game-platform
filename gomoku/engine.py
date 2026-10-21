"""五子棋規則與分析引擎。

規則：15x15 棋盤、自由規則（連成五子或以上即獲勝）、黑棋先行。
落子與所有判斷都由這裡的程式決定；語言模型只負責把這裡產生的事實寫成說明。
"""
from functools import lru_cache

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


def coord(x, y):
    return f"{COLS[x]}{y + 1}"


def other(color):
    return BLACK + WHITE - color


def color_to_move(moves):
    return BLACK if len(moves) % 2 == 0 else WHITE


def new_board():
    return [[EMPTY] * SIZE for _ in range(SIZE)]


def build_board(moves):
    """依著手序列（黑先）重建棋盤，回傳 (board, winner)。序列不合法時丟出 ValueError。"""
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
        board[y][x] = color
        if makes_five(board, x, y, color):
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


def makes_five(board, x, y, color):
    return any(_count_run(board, x, y, dx, dy, color) >= 5 for dx, dy in DIRS)


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


def point_value(board, x, y, color):
    """引擎估計：進攻價值加上阻擋對手的價值。"""
    attack = _shapes_score(shapes_at(board, x, y, color))
    defense = _shapes_score(shapes_at(board, x, y, other(color)))
    return attack + defense * 0.9


def move_labels(board, x, y, color):
    """把一手棋的作用寫成短句，例如「形成活三（橫向）」。"""
    opp = other(color)
    labels = []
    for name, mine, theirs in zip(DIR_NAMES, shapes_at(board, x, y, color), shapes_at(board, x, y, opp)):
        if mine >= OPEN_TWO:
            labels.append(f"形成{SHAPE_NAMES[mine]}（{name}）")
        if theirs >= THREE:
            labels.append(f"阻止{COLOR_NAMES[opp]}在此形成{SHAPE_NAMES[theirs]}（{name}）")
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


def candidates(board):
    cells = _near_cells(board, 2)
    if not cells and board[SIZE // 2][SIZE // 2] == EMPTY:
        return [(SIZE // 2, SIZE // 2)]
    return cells


def five_points(board, color):
    """color 下一手就能連五的空點。"""
    return [p for p in _near_cells(board, 1) if makes_five(board, p[0], p[1], color)]


def _win_points_after(board, x, y, color):
    """(x, y) 已放上 color 後，經過它的四條線上能連五的空點。"""
    points = set()
    for dx, dy in DIRS:
        if _shape(_line(board, x, y, dx, dy, color)) < FOUR:
            continue
        for k in range(-4, 5):
            cx, cy = x + k * dx, y + k * dy
            if (0 <= cx < SIZE and 0 <= cy < SIZE and board[cy][cx] == EMPTY
                    and _count_run(board, cx, cy, dx, dy, color) >= 5):
                points.add((cx, cy))
    return points


def _placed(board, x, y, color):
    b = [row[:] for row in board]
    b[y][x] = color
    return b


def find_vcf(board, color, node_limit=VCF_NODE_LIMIT):
    """搜尋連續衝四取勝（VCF）。

    找到時回傳手順 [己方, 對方被迫的擋點, 己方, ...]，最後一手之後對方已擋不住。
    回傳 None 只代表「在搜尋上限內未發現」，不代表確定沒有。
    """
    wins = five_points(board, color)
    if wins:
        return [wins[0]]
    b = [row[:] for row in board]
    return _vcf(b, color, set(five_points(b, other(color))), [node_limit], 0)


def _vcf(b, color, opp_fives, budget, depth):
    if budget[0] <= 0 or depth >= VCF_MAX_DEPTH or len(opp_fives) > 1:
        return None
    budget[0] -= 1
    opp = other(color)
    # 對手若已有連五點，這一手必須擋在那裡，而且要同時衝四才能繼續
    moves = sorted(opp_fives) if opp_fives else _near_cells(b, 2, color)
    for x, y in moves:
        if max(shapes_at(b, x, y, color)) < FOUR:
            continue
        b[y][x] = color
        wins = _win_points_after(b, x, y, color)
        if len(wins) >= 2:  # 活四或雙四：對手擋不完
            b[y][x] = EMPTY
            return [(x, y)]
        (wx, wy), = wins
        b[wy][wx] = opp
        rest = None
        if not makes_five(b, wx, wy, opp):
            rest = _vcf(b, color, _win_points_after(b, wx, wy, opp), budget, depth + 1)
        b[wy][wx] = EMPTY
        b[y][x] = EMPTY
        if rest is not None:
            return [(x, y), (wx, wy)] + rest
    return None


def vcf_text(name, line):
    """把連續衝四手順寫成主詞清楚的句子，避免語言模型混淆是哪一方的棋。"""
    if len(line) == 1:
        return f"{name}下 {coord(*line[0])} 就形成活四或雙四，對方擋不完"
    steps = "→".join(coord(x, y) for x, y in line)
    return f"{name}可以從 {coord(*line[0])} 開始連續衝四取勝（{steps}）"


def ranked_candidates(board, color):
    """依引擎估計由好到壞排列候選點；同分時選比較靠近中央的點。"""
    center = SIZE // 2
    return sorted(candidates(board), key=lambda p: (point_value(board, *p, color),
                                                    -max(abs(p[0] - center), abs(p[1] - center))), reverse=True)


def choose_move(board, color):
    """決定 color 的下一手，回傳 {"move", "reason", "points"}；points 是附證據種類的事實。

    AI 對局由 Agent 下（見 agent.py），這裡是 Agent 失敗時的代下、復盤與提問的標準。
    """
    me, opp = COLOR_NAMES[color], COLOR_NAMES[other(color)]
    cands = candidates(board)
    if not any(any(row) for row in board):
        x, y = cands[0]
        return _decision((x, y), "opening", [
            (ESTIMATE, f"第一手下在天元 {coord(x, y)}，四個方向的發展空間最大。")])

    wins = five_points(board, color)
    if wins:
        x, y = wins[0]
        return _decision(wins[0], "win", [(RULE, f"{me}下在 {coord(x, y)} 連成五子，獲勝。")])

    blocks = five_points(board, other(color))
    if blocks:
        best = max(blocks, key=lambda p: point_value(board, *p, color))
        points = [(RULE, f"{opp}在 {coord(*best)} 有連五的機會，{me}必須先擋住。")]
        if len(blocks) > 1:
            others = "、".join(coord(*p) for p in blocks if p != best)
            points.append((RULE, f"但{opp}還可以在 {others} 連五，這一手已經擋不完。"))
        return _decision(best, "block_five", points)

    vcf = find_vcf(board, color)
    if vcf:
        return _decision(vcf[0], "vcf", [
            (SOLVED, f"在搜尋範圍內已確認，{vcf_text(me, vcf)}。")])

    ranked = ranked_candidates(board, color)
    threat = find_vcf(board, other(color))
    for p in ranked[:SAFETY_CHECKS]:
        if find_vcf(_placed(board, *p, color), other(color)) is None:
            points = []
            if threat:
                points.append((SOLVED, f"在搜尋範圍內已確認，如果不處理，{vcf_text(opp, threat)}。"))
                points.append((ESTIMATE, f"{me}下在 {coord(*p)}，在搜尋範圍內未發現{opp}還能連續衝四。"))
            points.append((ESTIMATE, f"依引擎估計，{coord(*p)} 的作用：{'、'.join(move_labels(board, *p, color))}。"))
            alts = [q for q in ranked[:3] if q != p][:2]
            if alts:
                alt_text = "；".join(f"{coord(*q)}（{'、'.join(move_labels(board, *q, color))}）" for q in alts)
                points.append((ESTIMATE, f"其他考慮過的候選：{alt_text}。"))
            return _decision(p, "defend_vcf" if threat else "heuristic", points)

    best = ranked[0]
    points = [(ESTIMATE, f"依引擎估計，{coord(*best)} 的作用：{'、'.join(move_labels(board, *best, color))}。")]
    if threat:
        points.insert(0, (SOLVED, f"在搜尋範圍內已確認，{vcf_text(opp, threat)}，{me}前幾名候選都擋不住。"))
    return _decision(best, "no_defense", points)


def _decision(move, reason, points):
    return {"move": list(move), "reason": reason,
            "points": [{"evidence": ev, "text": text} for ev, text in points]}


# ---- 復盤 ----

def judge_move(board, x, y, color):
    """判斷 color 在 board 上下 (x, y) 是否為關鍵失誤；不是時回傳 None。

    只有在能指出具體證據時才判定；局面已經擋不住時，不把敗因歸給這一手。
    """
    opp = other(color)
    me, them = COLOR_NAMES[color], COLOR_NAMES[opp]
    played = coord(x, y)

    wins = five_points(board, color)
    if wins:
        if (x, y) in wins:
            return None
        return _moment("missed_win", "錯過直接連五", (x, y), wins[0], [
            (RULE, f"這時{me}在 {coord(*wins[0])} 就能連成五子，但下了 {played}。")])

    blocks = five_points(board, opp)
    if len(blocks) == 1 and (x, y) not in blocks:
        return _moment("missed_block", "沒有擋住對手的衝四", (x, y), blocks[0], [
            (RULE, f"{them}已經衝四，下一手可以在 {coord(*blocks[0])} 連五；{played} 沒有擋住。")])
    if blocks:
        return None  # 擋了，或是雙四已經擋不完

    vcf = find_vcf(board, color)
    if vcf:
        if _keeps_vcf(board, x, y, color):
            return None
        return _moment("missed_vcf", "錯過連續衝四取勝", (x, y), vcf[0], [
            (SOLVED, f"在搜尋範圍內已確認，這時{vcf_text(me, vcf)}。"),
            (ESTIMATE, f"下了 {played} 之後，在搜尋範圍內未找到同樣的取勝手順。")])

    threat = find_vcf(_placed(board, x, y, color), opp)
    if threat:
        best = tuple(choose_move(board, color)["move"])
        if best != (x, y) and find_vcf(_placed(board, *best, color), opp) is None:
            return _moment("allowed_vcf", "讓對手可以連續衝四", (x, y), best, [
                (SOLVED, f"在搜尋範圍內已確認，{me}下 {played} 之後，{vcf_text(them, threat)}。"),
                (ESTIMATE, f"如果{me}改下 {coord(*best)}，在搜尋範圍內未發現{them}還能連續衝四。")])
        return None  # 前幾名候選都擋不住，敗勢不是這一手造成的

    played_value = point_value(board, x, y, color)
    top_value = max(point_value(board, *p, color) for p in candidates(board))
    if top_value < SUGGEST_MIN_VALUE or played_value >= top_value * SUGGEST_RATIO:
        return None  # 沒有明顯更好的點
    # 建議必須是 AI 自己也會下的那一手，避免建議一手被安全檢查排除的棋
    decision = choose_move(board, color)
    best = tuple(decision["move"])
    if decision["reason"] == "no_defense" or best == (x, y):
        return None
    best_value = point_value(board, *best, color)
    facts = [(ESTIMATE, f"依引擎估計，{coord(*best)} 的作用：{'、'.join(move_labels(board, *best, color))}。"),
             (ESTIMATE, f"實際下的 {played}：{'、'.join(move_labels(board, x, y, color))}。")]
    if top_value >= GAP_MIN_VALUE and played_value * 2 < best_value:
        return _moment("gap", "有明顯更好的選擇", (x, y), best, facts)
    if played_value < best_value * SUGGEST_RATIO:
        return _moment("suggest", f"建議：引擎推薦 {coord(*best)}", (x, y), best, facts + [
            (ESTIMATE, f"差距不大，不算失誤；{coord(*best)} 是引擎比較推薦的下法。")])
    return None


def _keeps_vcf(board, x, y, color):
    """下了 (x, y) 之後，是否仍走在連續衝四取勝的路上。"""
    b = _placed(board, x, y, color)
    wins = _win_points_after(b, x, y, color)
    if not wins:
        return False  # 連續衝四的每一手都必須是四
    if len(wins) >= 2:
        return True
    wx, wy = wins.pop()
    b[wy][wx] = other(color)
    return not makes_five(b, wx, wy, other(color)) and find_vcf(b, color) is not None


def _moment(kind, title, played, best, points):
    return {"type": kind, "level": LEVELS[kind], "title": title, "played": list(played), "best": list(best),
            "points": [{"evidence": ev, "text": text} for ev, text in points]}


def explain_position(board, focus=(), next_move=None):
    """回答玩家提問用：整理目前局面的事實。

    focus 是玩家問到的點（最多 3 個），next_move 是棋譜中實際的下一手（沒有則為 None）。
    """
    color = BLACK if sum(v != EMPTY for row in board for v in row) % 2 == 0 else WHITE
    opp = other(color)
    me, them = COLOR_NAMES[color], COLOR_NAMES[opp]
    points = [(RULE, f"目前輪到{me}下。")]

    decision = choose_move(board, color)
    best = tuple(decision["move"])
    first = decision["points"][0]
    points.append((first["evidence"], f"引擎會建議{me}下 {coord(*best)}，理由如下。"))
    points += [(p["evidence"], p["text"]) for p in decision["points"]]
    if decision["reason"] not in ("block_five", "defend_vcf", "no_defense"):
        threat = find_vcf(board, opp)
        if threat:
            points.append((SOLVED, f"在搜尋範圍內已確認，如果輪到{them}下，{vcf_text(them, threat)}。"))
        else:
            points.append((ESTIMATE, f"在搜尋範圍內未發現{them}目前有連續衝四。"))

    for x, y in focus[:3]:
        points += check_point(board, x, y, color)
        if (x, y) == best:
            points.append((ESTIMATE, f"{coord(x, y)} 就是引擎建議的那一手。"))

    if next_move is not None:
        points += _move_facts(board, *next_move, color, "棋譜中實際的下一手")

    return [{"evidence": ev, "text": text} for ev, text in points]


def check_point(board, x, y, color):
    """檢查 color 下在 (x, y) 的事實，回傳 [(證據種類, 文字)]。提問與 Agent 的 check_move 工具共用。"""
    opp = other(color)
    me, them = COLOR_NAMES[color], COLOR_NAMES[opp]
    c = coord(x, y)
    if board[y][x] != EMPTY:
        return [(RULE, f"{c} 已經有{COLOR_NAMES[board[y][x]]}的棋子。")]
    points = [(ESTIMATE, f"如果{me}下 {c}：{'、'.join(move_labels(board, x, y, color))}。"),
              (ESTIMATE, f"如果換{them}下 {c}：{'、'.join(move_labels(board, x, y, opp))}。")]
    blocks = five_points(board, opp)
    if makes_five(board, x, y, color):
        points.append((RULE, f"{me}下 {c} 會連成五子。"))
    elif blocks and (x, y) not in blocks:
        points.append((RULE, f"{them}已經衝四，{me}下 {c} 沒有擋住 {coord(*blocks[0])}，{them}下一手就能連五。"))
    else:
        threat = find_vcf(_placed(board, x, y, color), opp)
        if threat:
            points.append((SOLVED, f"在搜尋範圍內已確認，{me}下 {c} 之後，{vcf_text(them, threat)}。"))
        else:
            points.append((ESTIMATE, f"{me}下 {c} 之後，在搜尋範圍內未發現{them}能連續衝四。"))
    return points


def explain_move(moves, n):
    """回答提問用：棋譜第 n 手（從 1 起算）的事實，供玩家用「第 n 手」提問時使用。"""
    board, _ = build_board(moves[:n - 1])
    x, y = moves[n - 1]
    color = BLACK if (n - 1) % 2 == 0 else WHITE
    return [{"evidence": ev, "text": text} for ev, text in _move_facts(board, x, y, color, f"第 {n} 手")]


def _move_facts(board, x, y, color, name):
    points = [(RULE, f"{name}是{COLOR_NAMES[color]} {coord(x, y)}。")]
    m = judge_move(board, x, y, color)
    if m:
        label = {"key": "關鍵時刻", "minor": "小失誤", "suggest": "建議"}[m["level"]]
        points.append((m["points"][0]["evidence"], f"引擎把{name}列為{label}：{m['title']}。"))
        points += [(p["evidence"], p["text"]) for p in m["points"]]
    else:
        points.append((ESTIMATE, f"引擎沒有在{name}找到問題。"))
    return points


def review(moves, color):
    """找出 color 方的所有問題（關鍵時刻與小失誤），依手數排序。"""
    board = new_board()
    moments = []
    for i, (x, y) in enumerate(moves):
        mover = BLACK if i % 2 == 0 else WHITE
        if mover == color:
            m = judge_move(board, x, y, color)
            if m:
                m["index"] = i
                moments.append(m)
        board[y][x] = mover
    return moments
