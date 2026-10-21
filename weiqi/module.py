"""圍棋的遊戲模組：rules.py（規則，事實屬於規則確認）＋KataGo（分析，屬於引擎估計）。

行動記法：GTP 座標（例如 "D4"）、"pass"（虛手）、"resign"（認輸）。座位 1＝黑棋（先手）、2＝白棋。
"""
import re
import threading
from collections import OrderedDict

from arena.base import ESTIMATE, RULE, GameModule

from . import rules
from .katago import ENGINE, KataGoUnavailable
from .rules import BLACK, COLOR_NAMES, PASS, RESIGN, WHITE

ACTION_RE = re.compile(r"^[A-HJ-T](?:1[0-9]|[1-9])$")
TOOL_VISITS = 100    # Agent 工具每次分析的搜尋次數（19 路約 0.8 秒）
ENGINE_VISITS = 200  # 引擎代下
REVIEW_VISITS = 25   # 復盤時每個局面（整盤一次送出）
# 復盤用目數損失而不是勝率：圍棋的勝率很快就飽和（9 路貼 7.5 目時，黑棋一開局就只有約 3%），
# 用勝率判斷會讓落後的一方再也找不到失誤。目數損失本身就是證據，所以局面已經落後時照樣列出，
# 但會註明勝負大致已定、這一手不是輸棋的原因。
LOSS_THRESHOLD = 4.0  # 損失 4 目以上是關鍵時刻
MINOR_THRESHOLD = 2.0  # 2–4 目是小失誤（使用者要求問題全部列出並分級，2026-10-11）
SUGGEST_THRESHOLD = 1.0  # 1–2 目不算失誤，列為「建議」（KataGo 更推薦的下法）；再小就在 KataGo 低搜尋次數的誤差內
DECIDED_BELOW, DECIDED_BEHIND = 0.05, 10.0  # 勝率低於 5% 且落後 10 目以上才說勝負大致已定（只看勝率會飽和）
CANDIDATES = 5


def pct(winrate):
    return f"{winrate * 100:.0f}%"


def lead_text(lead):
    return f"領先約 {lead:.1f} 目" if lead >= 0 else f"落後約 {-lead:.1f} 目"


def stones_text(stones):
    names = sorted(rules.coord(x, y) for x, y in stones)
    return "、".join(names) if len(names) <= 3 else f"{names[0]} 等 {len(names)} 子"


class WeiqiModule(GameModule):
    key = "weiqi"
    name = "圍棋"
    seat_names = COLOR_NAMES
    coord_pattern = r"(?<![A-Za-z0-9])([A-HJ-T](?:1[0-9]|[1-9]))(?![0-9])"
    default_steps = 3
    board_script = "weiqi/board.js"
    icon = "weiqi/icon.svg"
    tagline = "9／13／19 路，貼 7.5 目。復盤會算出每一手大約損失幾目。"
    rules = [
        {"title": "目標", "items": ["黑白兩方輪流在棋盤的交叉點放棋子，最後誰圍住的地方（加上盤上的子）比較多，誰就贏。"]},
        {"title": "怎麼下", "items": ["黑棋先下，白棋最後會多加 7.5 目（貼目），補償後下的不利。",
                                   "點棋盤上的空交叉點就是下在那裡；也可以按「虛手」這一步不下，或按「認輸」結束。",
                                   "棋盤可以選 19 路、13 路或 9 路；9 路一盤比較快，適合練習。"]},
        {"title": "氣與提子", "items": ["一顆子或連在一起的一群子，旁邊（上下左右）的空點叫「氣」。",
                                     "把對方一群子的氣全部堵住，就可以把它們提走（拿掉）。只剩一口氣叫「被叫吃」，要趕快逃或補。",
                                     "不能下在下完自己就沒有氣的地方（禁止自殺），除非這一步能提掉對方的子。"]},
        {"title": "劫", "items": ["同一個盤面不能重複出現：被提掉一顆子後，通常不能馬上提回來，要先在別的地方下一手。"]},
        {"title": "結束與數子", "items": ["雙方連續虛手，對局就結束，照盤面數子：自己盤上的子，加上只被自己的子圍住的空點。黑棋減掉白棋的貼目 7.5 後比大小。",
                                     "這裡的數法是「盤上的子一律算活」，不會另外判斷死活；所以虛手之前，要先把對方已經救不活的子提掉，不然它們也會被算進對方的分數。",
                                     "對手虛手時，畫面最上面會提醒你這件事。"]},
        {"title": "座標", "items": ["直的是 A 到 T（跳過 I，避免和 1 搞混），橫的是 1 到 19，第 1 列在最下面。說明裡的座標可以點，棋盤上會標出位置。"]},
    ]
    settings_fields = [{"name": "size", "label": "棋盤大小",
                        "options": [[19, "19 路"], [13, "13 路"], [9, "9 路"]], "default": 19}]
    benchmark_samples = [  # 19 路：開局、小目定式中、星位對角
        ["Q16", "D4", "Q4"],
        ["Q16", "D4", "Q4", "D16", "R10"],
        ["Q16", "D4", "Q4", "D16", "C14", "E16"],
    ]
    tools = [
        {"type": "function", "function": {
            "name": "find_threats",
            "description": "查雙方被叫吃（只剩 1 氣）或只剩 2 氣的棋塊，以及現在能提掉的子。",
            "parameters": {"type": "object", "properties": {}}}},
        {"type": "function", "function": {
            "name": "list_candidates",
            "description": f"用 KataGo 列出最好的前 {CANDIDATES} 個候選點，附勝率與目差。",
            "parameters": {"type": "object", "properties": {}}}},
        {"type": "function", "function": {
            "name": "check_move",
            "description": "檢查某一點：合不合法、下完後的氣數與提子，以及 KataGo 估計的勝率變化。",
            "parameters": {"type": "object", "properties": {
                "coord": {"type": "string", "description": "座標，例如 D4"}}, "required": ["coord"]}}},
        {"type": "function", "function": {
            "name": "play_move",
            "description": "決定落子或虛手。",
            "parameters": {"type": "object", "properties": {
                "coord": {"type": "string", "description": "座標，例如 D4；虛手請填 pass"},
                "reason": {"type": "string", "description": "一句話說明為什麼下這裡"}},
                "required": ["coord", "reason"]}}},
    ]

    def __init__(self):
        self.analyzer = ENGINE  # 測試時換成假的分析器
        self._cache = OrderedDict()  # 同一個局面在一次 Agent 決策中常被查好幾次
        self._lock = threading.Lock()  # runserver 是多執行緒；KataGo 本來就一次只算一個查詢

    # ---- 規則 ----

    def clean_settings(self, data):
        size = int(data.get("size", 19))
        if size not in (9, 13, 19):
            raise ValueError("棋盤大小必須是 9、13 或 19 路")
        return {"size": size, "komi": 7.5}

    def new_state(self, settings):
        s = settings or self.clean_settings({})
        return rules.GoState(s["size"], s["komi"])

    def normalize(self, text):
        t = str(text or "").strip()
        if t.lower() in (PASS, "虛手"):
            return PASS
        if t.lower() in (RESIGN, "認輸"):
            return RESIGN
        return t.upper() if ACTION_RE.match(t.upper()) else None

    def apply(self, state, action):
        return rules.play(state, action)

    def to_move(self, state):
        return state.to_move

    def outcome(self, state):
        r = rules.result(state)
        return (r[0], False) if r else None

    def frame(self, state, seat):
        r = rules.result(state)
        return {"size": state.size, "board": "".join(".BW"[v] for row in state.board for v in row),
                "last": state.actions[-1] if state.actions else None, "to_move": state.to_move,
                "captures": state.captures, "passes": state.passes, "result": r[1] if r else None}

    # ---- 分析 ----

    def _analysis(self, state, extra=None, visits=TOOL_VISITS):
        actions = state.actions + ([extra] if extra else [])
        key = (state.size, state.komi, tuple(actions), visits)
        with self._lock:
            if key not in self._cache:
                turn = len(actions)
                self._cache[key] = self.analyzer.analyze(state.size, state.komi, actions, [turn], visits)[turn]
                while len(self._cache) > 64:
                    self._cache.popitem(last=False)
            return self._cache[key]

    @staticmethod
    def _for(color, winrate=None, lead=None):
        """黑棋觀點 → color 的觀點。"""
        if winrate is not None:
            return winrate if color == BLACK else 1 - winrate
        return lead if color == BLACK else -lead

    def _threats(self, state):
        color, opp = state.to_move, rules.other(state.to_move)
        me, them = COLOR_NAMES[color], COLOR_NAMES[opp]
        points = []
        for owner in (color, opp):
            weak = [g for g in rules.groups(state.board, owner) if len(g[1]) == 1 or (len(g[1]) == 2 and len(g[0]) >= 2)]
            for stones, libs in sorted(weak, key=lambda g: (len(g[1]), -len(g[0])))[:4]:
                names = "、".join(sorted(rules.coord(x, y) for x, y in libs))
                if len(libs) == 2:
                    points.append((RULE, f"{COLOR_NAMES[owner]} {stones_text(stones)} 只剩 2 氣（{names}）。"))
                elif owner == color:
                    points.append((RULE, f"{me} {stones_text(stones)} 被叫吃（只剩 {names} 1 氣），"
                                         f"{them}下一手下 {names} 就能提掉 {len(stones)} 子。"))
                elif rules.is_legal(state, names):
                    points.append((RULE, f"{them} {stones_text(stones)} 只剩 1 氣，{me}現在下 {names} 可以提掉 {len(stones)} 子。"))
                else:
                    points.append((RULE, f"{them} {stones_text(stones)} 只剩 1 氣（{names}），但{me}現在下 {names} 不合法（劫）。"))
        if not points:
            points.append((RULE, "雙方都沒有被叫吃或只剩 2 氣的棋塊（2 子以上）。"))
        if state.passes:
            points.append((RULE, f"{COLOR_NAMES[opp]}上一手虛手；{COLOR_NAMES[color]}也虛手的話對局結束並數子。"))
        return points

    def _candidates(self, state):
        a = self._analysis(state)
        color = state.to_move
        items = "；".join(
            f"{i}. {m['move']}（勝率 {pct(self._for(color, winrate=m['winrate']))}，"
            f"{lead_text(self._for(color, lead=m['scoreLead']))}）"
            for i, m in enumerate(a["moves"][:CANDIDATES], 1))
        now = self._for(color, winrate=a["root"]["winrate"])
        return [(ESTIMATE, f"依引擎估計（KataGo {TOOL_VISITS} 次搜尋），{COLOR_NAMES[color]}目前勝率約 {pct(now)}；"
                           f"候選依好壞排序：{items}。")]

    def _rule_facts(self, state, action):
        """下 action 的規則事實：合不合法、提子、下完後自己棋塊的氣、叫吃到的對方棋塊。"""
        me = COLOR_NAMES[state.to_move]
        try:
            after = rules.play(state, action)
        except ValueError as exc:
            return None, [(RULE, f"{me}不能下 {action}：{exc}。")]
        if action == PASS:
            return after, [(RULE, f"{me}虛手。")]
        x, y = rules.parse(action, state.size)
        stones, libs = rules.group(after.board, x, y)
        captured = after.captures[state.to_move] - state.captures[state.to_move]
        points = [(RULE, f"{me}下 {action} 後，這塊棋（{len(stones)} 子）有 {len(libs)} 氣"
                         + (f"，並提掉 {captured} 子。" if captured else "。"))]
        if len(libs) == 1 and len(stones) >= 2:
            points.append((RULE, f"{me}下 {action} 會讓自己 {len(stones)} 子只剩 1 氣，對方下一手可以提掉。"))
        for ostones, olibs in rules.groups(after.board, rules.other(state.to_move)):
            if len(olibs) == 1 and any(p in ostones for p in rules.neighbors(x, y, state.size)):
                points.append((RULE, f"{me}下 {action} 叫吃對方 {stones_text(ostones)}。"))
        return after, points

    def _check(self, state, action):
        after, points = self._rule_facts(state, action)
        if after is None or after.over:
            return points
        color = state.to_move
        try:
            best = self._for(color, winrate=self._analysis(state)["root"]["winrate"])
            res = self._analysis(state, extra=action)["root"]
            points.append((ESTIMATE, f"依引擎估計，{COLOR_NAMES[color]}下 {action} 後勝率約 "
                                     f"{pct(self._for(color, winrate=res['winrate']))}（下之前最佳約 {pct(best)}），"
                                     f"{lead_text(self._for(color, lead=res['scoreLead']))}。"))
        except KataGoUnavailable as exc:
            points.append((ESTIMATE, f"KataGo 無法使用，沒有勝率估計（{exc}）。"))
        return points

    # ---- Agent ----

    def agent_prompt(self, seat):
        return f"""你是圍棋 AI 棋手，執{COLOR_NAMES[seat]}。用工具查證之後再落子。
規則：禁止自殺、同一盤面不能重複（劫）、貼目 7.5。終局採數子：盤上的子一律算活，雙方連續虛手就照盤面數子，
所以在虛手之前要把對方的死子提掉。座標是 GTP 格式：欄 A–T（沒有 I），第 1 列在最下方，例如 D4、Q16。
建議流程：先呼叫 find_threats 看有沒有被叫吃或可以提的子；再用 list_candidates 看 KataGo 推薦的候選；
需要時用 check_move 檢查一兩個點；最後呼叫 play_move。
硬性規定：只能下合法的點；只有在 KataGo 的最佳候選是 pass 時才能虛手；不能認輸。違反時會被退回。
play_move 的 reason 用繁體中文一句話說明，只能提到工具結果裡出現過的座標。"""

    def board_text(self, state, actions):
        stones = {c: [rules.coord(x, y) for y in range(state.size) for x in range(state.size)
                      if state.board[y][x] == c] for c in (BLACK, WHITE)}
        lines = [f"{state.size} 路棋盤，貼目 {state.komi:g}。提子：黑棋 {state.captures[BLACK]}、白棋 {state.captures[WHITE]}。"]
        lines += [f"{COLOR_NAMES[c]}（{len(s)} 子）：{'、'.join(s) or '（無）'}" for c, s in stones.items()]
        if actions:
            lines.append(f"最近幾手：{'、'.join(actions[-6:])}")
        lines.append(f"現在輪到你（{COLOR_NAMES[state.to_move]}）下第 {len(actions) + 1} 手。")
        return "\n".join(lines)

    def action_from(self, args):
        return self.normalize(args.get("coord"))

    def run_tool(self, state, name, args):
        try:
            if name == "find_threats":
                return "查叫吃與氣數", self._threats(state)
            if name == "list_candidates":
                return "看 KataGo 候選", self._candidates(state)
            if name == "check_move":
                action = self.normalize(args.get("coord"))
                if action is None or action == RESIGN:
                    return None
                return f"檢查 {action}", self._check(state, action)
        except KataGoUnavailable as exc:
            return name, [(ESTIMATE, f"KataGo 無法使用（{exc}），這個工具沒有結果。")]
        return None

    def rejection(self, state, action):
        if action is None:
            return "座標格式不對，請用 GTP 座標（例如 D4）或 pass"
        if action == RESIGN:
            return "Agent 不能認輸，請下一手或虛手"
        if action == PASS:
            try:
                top = self._analysis(state)["moves"][0]["move"]
            except (KataGoUnavailable, IndexError, KeyError):
                return None
            if top.lower() != PASS:
                return f"KataGo 認為還有價值的落點（例如 {top}），現在虛手太早"
            return None
        try:
            rules.play(state, action)
        except ValueError as exc:
            return str(exc)
        return None

    def forced(self, state):
        empty = (rules.coord(x, y) for y in range(state.size) for x in range(state.size) if state.board[y][x] == rules.EMPTY)
        if not any(rules.is_legal(state, p) for p in empty):  # 找到第一個合法點就停
            return {"action": PASS, "points": [(RULE, f"{COLOR_NAMES[state.to_move]}沒有合法的落點，只能虛手。")]}
        return None

    def engine_move(self, state):
        try:
            a = self._analysis(state, visits=ENGINE_VISITS)
            for m in a["moves"]:
                move = self.normalize(m["move"])
                if move and rules.is_legal(state, move):
                    color = state.to_move
                    return {"action": move, "points": [
                        (ESTIMATE, f"依引擎估計（KataGo {ENGINE_VISITS} 次搜尋），{move} 是最佳候選，"
                                   f"勝率約 {pct(self._for(color, winrate=m['winrate']))}。")]}
        except KataGoUnavailable as exc:
            return self._simple_move(state, str(exc))
        return {"action": PASS, "points": [(ESTIMATE, "KataGo 沒有合法的候選，改為虛手。")]}

    def _simple_move(self, state, why):
        """KataGo 無法使用時的簡單選點：能提就提、被叫吃就逃、否則下在上一手附近。品質很低，要標明。"""
        note = (ESTIMATE, f"KataGo 無法使用（{why}），這一手只用簡單規則選點，品質很低。")
        color, opp = state.to_move, rules.other(state.to_move)
        for owner in (opp, color):  # 先提對方，再救自己
            for stones, libs in sorted(rules.groups(state.board, owner), key=lambda g: -len(g[0])):
                if len(libs) == 1:
                    move = rules.coord(*next(iter(libs)))
                    if rules.is_legal(state, move):
                        return {"action": move, "points": [note]}
        legal = rules.legal_points(state)
        if not legal:
            return {"action": PASS, "points": [note]}
        last = rules.parse(state.actions[-1], state.size) if state.actions and state.actions[-1] != PASS else None
        center = (state.size // 2, state.size // 2)
        tx, ty = last or center
        move = min(legal, key=lambda m: abs(rules.parse(m, state.size)[0] - tx) + abs(rules.parse(m, state.size)[1] - ty))
        return {"action": move, "points": [note]}

    # ---- 復盤與提問 ----

    def review(self, settings, actions, human_seat):
        state = self.new_state(settings)
        states = [state]
        for action in actions:
            state = rules.play(state, action)
            states.append(state)
        played = [a for a in actions if a != RESIGN]
        turns = [t for t, a in enumerate(played) if states[t].to_move == human_seat]
        if not turns:
            return []
        try:
            results = self.analyzer.analyze(state.size, state.komi, played,
                                            sorted(set(turns) | {t + 1 for t in turns}), REVIEW_VISITS)
        except KataGoUnavailable:
            return []
        moments = []
        for t in turns:
            before, after = results.get(t), results.get(t + 1)
            if not before or not after or not before["moves"]:
                continue
            color, name = states[t].to_move, COLOR_NAMES[states[t].to_move]
            lead_before = self._for(color, lead=before["root"]["scoreLead"])
            lead_after = self._for(color, lead=after["root"]["scoreLead"])
            best = before["moves"][0]
            loss = lead_before - lead_after
            if played[t] == best["move"] or loss < SUGGEST_THRESHOLD:
                continue
            level = "key" if loss >= LOSS_THRESHOLD else "minor" if loss >= MINOR_THRESHOLD else "suggest"
            _, rule_points = self._rule_facts(states[t], played[t])
            points = [
                (ESTIMATE, f"依引擎估計（KataGo），{name}下 {played[t]} 前{lead_text(lead_before)}，"
                           f"下完後{lead_text(lead_after)}，約損失 {loss:.1f} 目。"),
                (ESTIMATE, f"KataGo 建議下 {best['move']}（{lead_text(self._for(color, lead=best['scoreLead']))}）。"),
            ]
            wr_before = self._for(color, winrate=before["root"]["winrate"])
            if wr_before < DECIDED_BELOW and lead_before <= -DECIDED_BEHIND:
                points.append((ESTIMATE, f"這時{name}已{lead_text(lead_before)}、勝率依引擎估計低於 5%，勝負大致已定；"
                                         f"這一手的問題是損失目數，不是輸棋的原因。"))
            if level == "suggest":
                points.append((ESTIMATE, f"差距約 {loss:.1f} 目，不算失誤；{best['move']} 是 KataGo 比較推薦的下法。"))
            title = f"建議：KataGo 推薦 {best['move']}" if level == "suggest" else f"損失約 {loss:.0f} 目"
            moments.append({"index": t, "type": "gap", "level": level, "title": title, "played": played[t],
                            "pattern": "建議" if level == "suggest" else "目數損失",
                            "best": best["move"], "loss": loss,
                            "points": [{"evidence": ev, "text": text} for ev, text in points + rule_points]})
        return moments

    def explain_position(self, state, focus, next_action):
        points = [(RULE, f"目前輪到{COLOR_NAMES[state.to_move]}下。")] + self._threats(state)
        try:
            points += self._candidates(state)
        except KataGoUnavailable as exc:
            points.append((ESTIMATE, f"KataGo 無法使用，沒有候選分析（{exc}）。"))
        for action in focus[:3]:
            points += self._check(state, action)
        if next_action:
            points.append((RULE, f"紀錄中實際的下一手是{COLOR_NAMES[state.to_move]} {next_action}。"))
            if next_action != RESIGN and next_action not in focus[:3]:  # 問題已經問到這一手時不重複
                points += self._check(state, next_action)
        return [{"evidence": ev, "text": text} for ev, text in points]

    def explain_action(self, settings, actions, n):
        state = self.replay(settings, actions[:n - 1])
        action = actions[n - 1]
        points = [(RULE, f"第 {n} 手是{COLOR_NAMES[state.to_move]} {action}。")]
        if action != RESIGN:
            points += self._check(state, action)
        return [{"evidence": ev, "text": text} for ev, text in points]


MODULE = WeiqiModule()
