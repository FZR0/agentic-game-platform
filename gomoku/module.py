"""五子棋的遊戲模組：把 engine.py 接上 arena 的共用介面（研究計畫 3.3.1）。

行動記法是座標字串，例如 "H8"，或 "resign"（認輸，2026-10-21 使用者要求能結束對局）；座位 1＝黑棋（先手）、2＝白棋。
"""
import re

from arena.base import ESTIMATE, RULE, SOLVED, GameModule

from . import engine

CANDIDATE_COUNT = 5
ACTION_RE = re.compile(r"^[A-O](?:1[0-5]|[1-9])$")
RESIGN = "resign"


class State:
    """五子棋局面：棋盤、已下的座標與勝者。由 GomokuModule.apply 產生新的，不就地修改。
    認輸不是一顆棋子，不放進 moves，只記在 resigned（認輸的一方）。"""

    def __init__(self, board=None, moves=(), winner=None, resigned=None, rules=engine.FREESTYLE):
        self.board = board or engine.new_board()
        self.moves = list(moves)  # [(x, y), ...]
        self.winner = winner
        self.resigned = resigned
        self.rules = rules  # 黑棋禁手（engine.Rules），來自開局設定


def cell(action):
    return engine.COLS.index(action[0]), int(action[1:]) - 1


class GomokuModule(GameModule):
    key = "gomoku"
    agentic_requests = True
    name = "五子棋"
    seat_names = engine.COLOR_NAMES
    coord_pattern = r"(?<![A-Za-z0-9])([A-O](?:1[0-5]|[1-9]))(?![0-9])"
    default_steps = 6
    board_script = "gomoku/board.js"
    icon = "gomoku/icon.svg"
    tagline = "15×15，橫、直、斜先連成五子就贏；可以設定黑棋禁手。"
    rules = [
        {"title": "目標", "items": ["黑白兩方輪流在棋盤的交叉點放一顆棋子，先讓自己的棋子連成一直線五顆（橫、直、斜都可以）的一方獲勝。",
                                  "預設是自由規則：沒有禁手，連成六顆以上也算贏。"]},
        {"title": "黑棋禁手（可以在設定裡打開）", "items": [
            "黑棋先下比較有利，所以可以限制黑棋不能下某些點，叫「禁手」。三三、四四、長連各自可以設定禁止或不禁止；白棋永遠沒有禁手。",
            "三三：黑棋一手同時做出兩個活三。活三是再下一顆就能變成兩頭都空的四顆；如果那一顆本身是禁手、或被擋住變不成，就不算活三。",
            "四四：黑棋一手同時做出兩個活四（兩頭都空的四顆）。只差一顆、但有一頭被擋住的四不算。",
            "長連：黑棋連成六顆以上。禁止時不算贏，那一點也不能下。",
            "黑棋下在禁手點會被拒絕，棋盤上的禁手點會標禁止符號。同一手剛好連成五顆時，連五優先，黑棋直接獲勝。",
            "白棋可以利用禁手：衝四時如果黑棋唯一能擋的點是禁手，黑棋就擋不住了。"]},
        {"title": "怎麼下", "items": ["棋盤是 15×15，黑棋先下。",
                                   "點棋盤上的空交叉點就是下在那裡；下了就不能悔棋。",
                                   "棋盤下滿還沒有人連成五顆，就是和局。",
                                   "也可以按「認輸」結束，這盤算對方贏。"]},
        {"title": "座標", "items": ["直的是 A 到 O，橫的是 1 到 15，第 1 列在最下面；正中央是 H8。說明裡的座標可以點，棋盤上會標出位置。"]},
        {"title": "小技巧", "items": ["「活三」是兩頭都空的三顆，下一步就能變成擋不住的四顆，看到對方有活三要先擋。",
                                   "「衝四」是只差一顆就五連，對方一定要擋；連續衝四可以一路逼到贏。"]},
    ]
    # 黑棋禁手（2026-11-20 使用者要求，三項各自開關；預設不禁止＝原本的自由規則）。值寫進 Game.settings，engine.Rules.of 讀取
    settings_fields = [
        {"name": name, "label": f"黑棋禁手：{label}", "options": [["allow", "不禁止"], ["ban", "禁止"]], "default": "allow"}
        for name, label in engine.FORBIDDEN_NAMES.items()]
    benchmark_samples = [  # 一般佈局、防守活三、連續衝四取勝
        ["H8", "I9", "H9"],
        ["H8", "A15", "I8", "O15", "J8"],
        ["H8", "G8", "I8", "A15", "J8", "O15", "K9", "O1", "K10", "A1"],
    ]
    tools = [
        {"type": "function", "function": {
            "name": "find_threats",
            "description": "查雙方的立即威脅：誰下一手能連五、誰有連續衝四取勝的手順。",
            "parameters": {"type": "object", "properties": {}}}},
        {"type": "function", "function": {
            "name": "list_candidates",
            "description": f"列出引擎評估最好的前 {CANDIDATE_COUNT} 個候選點與各自的作用。",
            "parameters": {"type": "object", "properties": {}}}},
        {"type": "function", "function": {
            "name": "check_move",
            "description": "用引擎檢查某一點：雙方下在這裡的作用，以及下完之後對手能不能連續衝四。",
            "parameters": {"type": "object", "properties": {
                "coord": {"type": "string", "description": "座標，例如 H8"}}, "required": ["coord"]}}},
        {"type": "function", "function": {
            "name": "play_move",
            "description": "決定落子。",
            "parameters": {"type": "object", "properties": {
                "coord": {"type": "string", "description": "座標，例如 H8"},
                "reason": {"type": "string", "description": "一句話說明為什麼下這裡"}},
                "required": ["coord", "reason"]}}},
    ]

    # ---- 規則 ----

    # 對手觀察的應對建議（2026-11-05）：key 是 engine._moment 的標題
    opponent_tips = {
        "錯過直接連五": "對手有時看不到自己已經能連成五顆，代表他沒有每一步都檢查自己的取勝點；局面線條多、同時有好幾個威脅時他比較容易漏看。",
        "沒有擋住對手的衝四": "對手會漏擋衝四：有機會時可以多做衝四，特別是斜線或和其他棋子連在一起、同時形成兩個威脅的形狀。",
        "錯過連續衝四取勝": "對手不太會找連續衝四的取勝手順：他的攻勢常只有一兩步，守住第一波之後通常可以慢慢反擊。",
        "讓對手可以連續衝四": "對手防守時常留下連續衝四的機會給你：輪到你時可以先找找看有沒有每一步都是四、讓他只能被動擋的手順。",
        "有明顯更好的選擇": "對手常下在作用比較小的點：你可以下得穩一點、先擋住他的活三，等他自己累積失誤。",
    }

    def clean_settings(self, data):
        cleaned = {}
        for name, label in engine.FORBIDDEN_NAMES.items():
            value = data.get(name, "allow")
            if value not in ("allow", "ban"):
                raise ValueError(f"黑棋禁手（{label}）只能是「不禁止」或「禁止」")
            cleaned[name] = value
        return cleaned

    def new_state(self, settings):
        return State(rules=engine.Rules.of(settings))

    def normalize(self, text):
        action = str(text or "").strip().upper()
        if action in (RESIGN.upper(), "認輸"):
            return RESIGN
        return action if ACTION_RE.match(action) else None

    def apply(self, state, action):
        if self.normalize(action) != action:
            raise ValueError(f"座標格式不對：{action}")
        if action == RESIGN:
            color = self.to_move(state)
            return State(state.board, state.moves, engine.other(color), resigned=color, rules=state.rules)
        x, y = cell(action)
        if state.board[y][x] != engine.EMPTY:
            raise ValueError(f"{action} 已有棋子")
        color = self.to_move(state)
        ban = engine.forbidden(state.board, x, y, color, state.rules)
        if ban:
            raise ValueError(f"{action} 是黑棋的禁手（{ban}），不能下這裡")
        board = [row[:] for row in state.board]
        board[y][x] = color
        winner = color if engine.makes_five(board, x, y, color, state.rules) else None
        return State(board, state.moves + [(x, y)], winner, rules=state.rules)

    def to_move(self, state):
        return engine.color_to_move(state.moves)

    def outcome(self, state):
        if state.winner:
            return state.winner, False
        if len(state.moves) == engine.SIZE * engine.SIZE:
            return None, True
        if state.rules.active and not engine.has_legal_move(state.board, self.to_move(state), state.rules):
            return None, True  # 黑棋剩下的空點全是禁手：沒有棋可下，和局
        return None

    def frame(self, state, seat):
        # 由第 1 列（下方）往上、每列由 A 到 O；. 空、B 黑、W 白
        board = "".join(".BW"[v] for row in state.board for v in row)
        last = engine.coord(*state.moves[-1]) if state.moves else None
        frame = {"board": board, "last": last, "to_move": self.to_move(state)}
        if self.to_move(state) == engine.BLACK and not state.winner and state.rules.active:
            # 黑棋的禁手點由伺服器算好，board.js 只畫禁止符號、不讓點（網頁不算規則）
            frame["forbidden"] = [engine.coord(*p) for p in engine.forbidden_points(state.board, state.rules)]
        if state.resigned:
            loser = engine.COLOR_NAMES[state.resigned]
            frame["result"] = f"{loser}認輸，{engine.COLOR_NAMES[engine.other(state.resigned)]}獲勝"
        return frame

    # ---- Agent ----

    def agent_prompt(self, seat):
        return f"""你是五子棋 AI 棋手，執{engine.COLOR_NAMES[seat]}。用工具查證之後再落子。
規則：15x15 棋盤，連成五子以上獲勝；這盤如果有黑棋禁手，局面說明會寫出來，禁手點黑棋不能下。座標是欄 A–O 加列 1–15，第 1 列在最下方。
建議流程：先呼叫 find_threats 看有沒有必須立刻處理的事；再用 list_candidates 看引擎推薦的候選；
需要時用 check_move 檢查一兩個點；最後呼叫 play_move 落子。
硬性規定：自己能連五就下那裡；對手能連五就擋住；只能下在空點；黑棋不能下禁手。違反時落子會被退回。
play_move 的 reason 用繁體中文一句話說明為什麼下這裡，只能提到工具結果裡出現過的座標。"""

    def board_text(self, state, actions):
        color = self.to_move(state)
        stones = {engine.BLACK: [], engine.WHITE: []}
        for y in range(engine.SIZE):
            for x in range(engine.SIZE):
                if state.board[y][x]:
                    stones[state.board[y][x]].append(engine.coord(x, y))
        lines = [f"{engine.COLOR_NAMES[c]}：{'、'.join(stones[c]) or '（無）'}" for c in (engine.BLACK, engine.WHITE)]
        if state.rules.active:
            lines.append(engine.rules_fact(state.rules)[1])
        if actions:
            lines.append(f"上一手：{engine.COLOR_NAMES[engine.other(color)]} {actions[-1]}")
        lines.append(f"現在輪到你（{engine.COLOR_NAMES[color]}）下第 {len(actions) + 1} 手。")
        return "\n".join(lines)

    def action_from(self, args):
        return self.normalize(args.get("coord"))

    def run_tool(self, state, name, args):
        color = self.to_move(state)
        if name == "find_threats":
            return "查雙方威脅", find_threats(state.board, color, state.rules)
        if name == "list_candidates":
            return "看引擎候選", list_candidates(state.board, color, state.rules)
        if name == "check_move":
            action = self.normalize(args.get("coord"))
            if action is None:
                return None
            return f"檢查 {action}", engine.check_point(state.board, *cell(action), color, state.rules)
        return None

    def rejection(self, state, action):
        if action is None:
            return "座標格式不對，請用 A–O 加 1–15，例如 H8"
        if action == RESIGN:
            return "Agent 不能認輸，請下一手"
        x, y = cell(action)
        if state.board[y][x] != engine.EMPTY:
            return f"{action} 已經有棋子"
        color = self.to_move(state)
        ban = engine.forbidden(state.board, x, y, color, state.rules)
        if ban:
            return f"{action} 是你的禁手（{ban}），不能下"
        wins = engine.five_points(state.board, color, state.rules)
        if wins:  # 自己能連五就贏了，不必再擋對手
            return None if (x, y) in wins else f"你可以在 {engine.coord(*wins[0])} 連五，必須下那裡"
        # 擋點是自己的禁手時本來就擋不住，不要求去擋（否則每一手都被退回）
        blocks = [p for p in engine.five_points(state.board, engine.other(color), state.rules)
                  if not engine.forbidden(state.board, *p, color, state.rules)]
        if blocks and (x, y) not in blocks:
            return f"對手可以在 {engine.coord(*blocks[0])} 連五，必須擋住"
        return None

    def forced(self, state):
        if not state.moves:  # 第一手沒有可查證的內容，直接下天元
            return self.engine_move(state)
        return None

    def engine_move(self, state):
        decision = engine.choose_move(state.board, self.to_move(state), state.rules)
        return {"action": engine.coord(*decision["move"]), "points": decision["points"]}

    # ---- 復盤與提問 ----

    # 認輸一定是最後一個行動，而且不是一顆棋子：復盤與提問只看它之前的棋子

    def review(self, settings, actions, human_seat):
        moments = engine.review([cell(a) for a in actions if a != RESIGN], human_seat, engine.Rules.of(settings))
        for m in moments:
            m["played"] = engine.coord(*m["played"])
            m["best"] = engine.coord(*m["best"])
        return moments

    def style_points(self, settings, actions, human_seat):
        return engine.style_points([cell(a) for a in actions if a != RESIGN], human_seat, engine.Rules.of(settings))

    def explain_position(self, state, focus, next_action):
        return engine.explain_position(state.board, [cell(f) for f in focus],
                                       cell(next_action) if next_action and next_action != RESIGN else None, state.rules)

    def explain_action(self, settings, actions, n):
        rules = engine.Rules.of(settings)
        if actions[n - 1] == RESIGN:
            color = engine.BLACK if (n - 1) % 2 == 0 else engine.WHITE
            # 「我認輸時局面真的很差嗎？」：只寫「第 n 手是認輸」時模型沒有依據、自己說局面很差（2026-11-16 情境測試），附上認輸前的局面
            board, _ = engine.build_board([cell(a) for a in actions[:n - 1]], rules)
            return ([{"evidence": RULE, "text": f"第 {n} 手是{engine.COLOR_NAMES[color]}認輸；以下是認輸前的局面。"}]
                    + engine.explain_position(board, rules=rules))
        return engine.explain_move([cell(a) for a in actions if a != RESIGN], n, rules)


# ---- Agent 工具：回傳 [(證據種類, 文字)] ----

def find_threats(board, color, rules=engine.FREESTYLE):
    opp = engine.other(color)
    me, them = engine.COLOR_NAMES[color], engine.COLOR_NAMES[opp]
    names = lambda pts: "、".join(engine.coord(*p) for p in pts)
    points = []
    wins, blocks = engine.five_points(board, color, rules), engine.five_points(board, opp, rules)
    if wins:
        points.append((RULE, f"{me}下一手可以在 {names(wins)} 連五。"))
    banned = [p for p in blocks if engine.forbidden(board, *p, color, rules)]
    if len(banned) < len(blocks):
        points.append((RULE, f"{them}下一手可以在 {names([p for p in blocks if p not in banned])} 連五，{me}必須擋住。"))
    for p in banned:
        points.append((RULE, f"{them}下一手可以在 {engine.coord(*p)} 連五，但那裡是{me}的禁手"
                             f"（{engine.forbidden(board, *p, color, rules)}），{me}不能擋。"))
    for c, name in ((color, me), (opp, them)):
        line = engine.find_vcf(board, c, rules=rules)
        if line:
            points.append((SOLVED, f"在搜尋範圍內已確認，如果輪到{name}下，{engine.vcf_text(name, line, board, c, rules)}。"))
        else:
            points.append((ESTIMATE, f"在搜尋範圍內未發現{name}有連續衝四。"))
    if engine.dead_board_fact(board):
        points.append(engine.dead_board_fact(board))
    return points


def list_candidates(board, color, rules=engine.FREESTYLE):
    ranked = engine.ranked_candidates(board, color, rules)[:CANDIDATE_COUNT]
    items = "；".join(f"{i}. {engine.coord(*p)}（{'、'.join(engine.move_labels(board, *p, color, rules))}）"
                     for i, p in enumerate(ranked, 1))
    points = [(ESTIMATE, f"引擎候選（依評估由好到壞）：{items}。")]
    if engine.dead_board_fact(board):
        points.append(engine.dead_board_fact(board))
    banned = engine.forbidden_points(board, rules) if color == engine.BLACK else []
    if banned:
        points.append((RULE, f"黑棋現在的禁手點（不能下）：{'、'.join(engine.coord(*p) for p in banned)}。"))
    return points


MODULE = GomokuModule()
