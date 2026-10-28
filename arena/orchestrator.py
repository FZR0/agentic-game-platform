"""AI 助理：使用者在網頁上的每個請求都交給 Agent 處理（使用者決定，2026-10-28，為了證明 Agentic 系統可行）。

Agent 透過工具完成請求，工具就是 operations 裡原本的操作。程式守住底線：
- 每種請求只提供用得到的工具，清單以外的呼叫一律退回；
- 按鈕帶來的參數（棋局、座位、玩家的那一步…）是鎖定的，Agent 用不同的值呼叫就退回、不執行；
- 完成與否由程式檢查（done）；模型出錯、次數用完或沒完成時，由原本的程式補完還沒做的部分（fallback），
  畫面上提示使用者，同時記成 ModelRun（kind 為 req_<請求>），用來量 Agent 處理請求的成功率與秒數。
Agent 只看得到工具回傳的摘要，摘要只含玩家本來就看得到的資訊（撲克的底牌、洗牌種子都不會交給它）。
請求本身不成立（例如不合法的一步）時，操作丟出的 ValueError 直接傳回 views，照原本的方式回覆錯誤。
"""
import json
import time

from django.conf import settings

from . import agent, narrator, operations
from .models import ModelRun

REQUEST_STEPS = 4  # 每個請求最多呼叫模型幾次
SYSTEM_PROMPT = """你是益智遊戲平台的 AI 助理。使用者在網頁上的每個操作都交給你處理。
請用提供的工具完成使用者的請求：工具參數要照請求裡的值填，只做請求要求的事。
需要的工具都呼叫完、請求完成後，直接回覆「完成」，不要再呼叫工具。"""
# 畫面上的提示（寫給玩家看，不用 Agent、Ollama 等字眼）
NOTICES = {"error": "AI 助理目前連不上 AI 伺服器，這次已改用一般方式完成。",
           "fallback": "AI 助理這次沒有完成，已改用一般方式完成。"}


def _tool(name, description, **params):
    """工具定義；params 是 {參數: (JSON 型別, 說明)}，全部必填。"""
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": {k: {"type": t, "description": d} for k, (t, d) in params.items()},
                       "required": list(params)}}}


def _same(got, want):
    """Agent 填的參數是否就是請求的值（大小寫、空白、12 與 12.0 不計較）。"""
    if want in (None, ""):
        return got in (None, "")
    if isinstance(want, int):
        try:
            return float(got) == want
        except (TypeError, ValueError):
            return False
    return str(got).strip().lower() == str(want).strip().lower()


def _chat(messages, tools):
    config = narrator.model_config()
    body = {"model": config["name"], "stream": False, "think": config["think"], "keep_alive": settings.OLLAMA_KEEP_ALIVE,
            "tools": tools, "messages": messages, "options": {"temperature": 0.1, "num_predict": 300,
                                                            "num_ctx": settings.OLLAMA_NUM_CTX}}
    return narrator.chat(body)["message"]


class Request:
    """一種請求。ctx 是這次請求的資料（module、game、params…），工具執行的結果也存在裡面。"""
    kind = ""            # ModelRun 記成 req_<kind>
    game_scoped = True   # 對局的請求：只有 module.agentic_requests 的遊戲才交給 Agent；頁面不分遊戲，一律交給 Agent
    auto_finish = True   # 完成條件一達成就結束，不必再問模型一次
    tools = []

    def task(self, ctx):
        """告訴 Agent 使用者做了什麼。"""
        raise NotImplementedError

    def locked(self, ctx):
        """{工具: {參數: 請求的值}}；Agent 必須照填。"""
        return {}

    def call(self, ctx, name, args):
        """執行工具（參數已檢查），回傳給 Agent 的摘要。"""
        raise NotImplementedError

    def done(self, ctx):
        raise NotImplementedError

    def fallback(self, ctx):
        """原本的做法：補完 Agent 還沒做的部分（已經做過的不重做）。"""
        raise NotImplementedError

    def respond(self, ctx):
        raise NotImplementedError

    def execute(self, ctx, name, args, run):
        if name not in {t["function"]["name"] for t in self.tools}:
            run["rejected"] += 1
            return f"這個請求不能使用 {name}，沒有執行。"
        for key, want in self.locked(ctx).get(name, {}).items():
            if not _same(args.get(key), want):
                run["rejected"] += 1
                return f"{key} 必須是 {want}（使用者的請求），這次沒有執行。"
        return self.call(ctx, name, args)


class OneStep(Request):
    """只要呼叫一個工具就完成的請求（載入頁面資料、結束對局、復盤…）。"""

    def __init__(self, kind, tool, task, op, params=None, game_scoped=True, summary=None):
        self.kind, self.task_text, self.op, self.game_scoped = kind, task, op, game_scoped
        self.params = params or {}  # {參數: (JSON 型別, 說明, ctx["params"] 的 key)}
        self.summary = summary or (lambda data: "完成。")
        self.tools = [_tool(tool, task, **{k: (t, d) for k, (t, d, _) in self.params.items()})]

    def task(self, ctx):
        return self.task_text.format(**ctx["params"])

    def locked(self, ctx):
        return {self.tools[0]["function"]["name"]: {k: ctx["params"][src] for k, (_, _, src) in self.params.items()}}

    def call(self, ctx, name, args):
        ctx["data"] = self.op(ctx)
        return self.summary(ctx["data"])

    def done(self, ctx):
        return "data" in ctx

    def fallback(self, ctx):
        if not self.done(ctx):
            ctx["data"] = self.op(ctx)

    def respond(self, ctx):
        return ctx["data"]


GAME_ID = ("integer", "棋局編號", "game_id")
GAME_KEY = ("string", "遊戲代號", "game")


class NewGame(Request):
    """「新對局」：存下新對局，再讓 AI 的座位行動到輪到玩家（例如撲克或玩家執白）。"""
    kind = "new_game"
    tools = [_tool("start_game", "建立新對局並存檔", game=GAME_KEY[:2], seat=("integer", "玩家的座位")),
             _tool("run_ai_turns", "讓 AI 的座位行動，直到輪到玩家或對局結束", game_id=GAME_ID[:2])]

    def task(self, ctx):
        p = ctx["params"]
        return (f"使用者在{ctx['module'].name}（遊戲代號 {p['game']}）按了「新對局」，選擇座位 {p['seat']}。"
                "請建立對局，再讓 AI 行動到輪到玩家。")

    def locked(self, ctx):
        p = ctx["params"]
        locked = {"start_game": {"game": p["game"], "seat": p["seat"]}}
        if "game" in ctx:  # 還沒建立時由 call 回覆「還沒建立對局」
            locked["run_ai_turns"] = {"game_id": ctx["game"].id}
        return locked

    def call(self, ctx, name, args):
        if name == "start_game":
            if "game" in ctx:
                return f"第 {ctx['game'].id} 盤已經建立，不重複建立。"
            self._start(ctx)
            return f"已建立第 {ctx['game'].id} 盤。"
        if "game" not in ctx:
            return "還沒建立對局，沒有執行。"
        self._ai(ctx)
        return _turn_summary(ctx)

    def _start(self, ctx):
        p = ctx["params"]
        ctx["game"], ctx["frame"] = operations.start_game(ctx["module"], p["seat"], p["settings"])

    def _ai(self, ctx):
        p = ctx["params"]
        ctx["ai_moves"] = operations.run_ai_turns(ctx["module"], ctx["game"], p["model"], p["steps"])

    def done(self, ctx):
        return "ai_moves" in ctx

    def fallback(self, ctx):
        if "game" not in ctx:
            self._start(ctx)
        if "ai_moves" not in ctx:
            self._ai(ctx)

    def respond(self, ctx):
        return {"game": ctx["game"].summary(), "frame": ctx["frame"], "ai_moves": ctx["ai_moves"]}


def _turn_summary(ctx):
    """給 Agent 的 AI 行動摘要：只說幾步、接下來輪到誰，不含任何底牌或思路。"""
    game = ctx["game"]
    end = "對局已結束" if game.finished else "輪到玩家"
    return f"AI 行動了 {len(ctx['ai_moves'])} 步，{end}。"


class Move(Request):
    """玩家下了一步：存下這一步，再讓 AI 的座位行動到又輪到玩家。"""
    kind = "move"
    tools = [_tool("play_human_move", "驗證並存下玩家的這一步", game_id=GAME_ID[:2], action=("string", "玩家的行動")),
             _tool("run_ai_turns", "讓 AI 的座位行動，直到輪到玩家或對局結束", game_id=GAME_ID[:2])]

    def task(self, ctx):
        p = ctx["params"]
        return (f"使用者在{ctx['module'].name}第 {p['game_id']} 盤下了「{p['action']}」。"
                "請存下這一步，再讓 AI 行動到又輪到玩家。")

    def locked(self, ctx):
        p = ctx["params"]
        return {"play_human_move": {"game_id": p["game_id"], "action": p["action"]},
                "run_ai_turns": {"game_id": p["game_id"]}}

    def call(self, ctx, name, args):
        if name == "play_human_move":
            if "frame" in ctx:
                return "這一步已經存下，不重複存。"
            self._move(ctx)
            return f"已存下第 {len(ctx['game'].actions)} {ctx['module'].step_word}。"
        if "frame" not in ctx:
            return "要先存下玩家的這一步，沒有執行。"
        self._ai(ctx)
        return _turn_summary(ctx)

    def _move(self, ctx):
        ctx["frame"] = operations.play_human_move(ctx["module"], ctx["game"], ctx["params"]["action"])

    def _ai(self, ctx):
        p = ctx["params"]
        ctx["ai_moves"] = operations.run_ai_turns(ctx["module"], ctx["game"], p["model"], p["steps"])

    def done(self, ctx):
        return "ai_moves" in ctx

    def fallback(self, ctx):
        if "frame" not in ctx:
            self._move(ctx)
        if "ai_moves" not in ctx:
            self._ai(ctx)

    def respond(self, ctx):
        game = ctx["game"]
        return {"action": ctx["params"]["action"], "frame": ctx["frame"], "ai_moves": ctx["ai_moves"],
                "winner": game.winner, "draw": game.draw}


class Ask(Request):
    """復盤提問：由 Agent 決定要查哪些局面與哪幾步（以前用正規表示式猜），查到的事實再交給語言模型回答。"""
    kind = "ask"
    auto_finish = False  # 要查多少由 Agent 決定
    tools = [_tool("explain_position", "查詢使用者正在看的局面；focus 是問題提到的位置或行動（例如座標），沒有就填空字串",
                   focus=("string", "問題提到的位置或行動")),
             _tool("explain_action", "查詢這盤棋第 n 步（從 1 起算）的分析", n=("integer", "第幾步"))]

    def task(self, ctx):
        p = ctx["params"]
        return (f"使用者在{ctx['module'].name}第 {p['game_id']} 盤（共 {len(ctx['game'].actions)} 步）復盤，"
                f"正在看第 {p['at']} 步之後的局面，問：「{p['question']}」。"
                "請用工具查出回答這個問題需要的分析（可以查不只一項），查完回覆「完成」。")

    def call(self, ctx, name, args):
        module, game, at = ctx["module"], ctx["game"], ctx["params"]["at"]
        try:
            if name == "explain_position":
                points = operations.position_points(module, game, at, module.question_focus(str(args.get("focus", ""))))
            else:
                points = operations.action_points(module, game, int(args.get("n")))
        except (TypeError, ValueError) as exc:
            return f"查不到：{exc}"
        found = ctx.setdefault("points", [])
        for p in points:
            if p not in found:
                found.append(p)
        return "\n".join(f"[{p['evidence']}] {p['text']}" for p in points) or "沒有找到相關的分析。"

    def done(self, ctx):
        return bool(ctx.get("points"))

    def fallback(self, ctx):
        if not self.done(ctx):
            p = ctx["params"]
            ctx["points"] = operations.ask_points_by_rules(ctx["module"], ctx["game"], p["question"], p["at"])

    def respond(self, ctx):
        p = ctx["params"]
        return operations.answer(ctx["module"], ctx["game"], p["question"], p["at"], ctx["points"], p["model"])


def _game(ctx):
    return ctx["game"]


REQUESTS = {r.kind: r for r in [
    OneStep("home", "load_home", "使用者打開了首頁，請載入首頁的資料（未完成的對局、最近對局、戰績）。",
            lambda ctx: operations.home_context(), game_scoped=False),
    OneStep("history", "load_history", "使用者打開了對局紀錄，篩選條件：遊戲「{game}」、結果「{result}」、第 {page} 頁。請載入這一頁。",
            lambda ctx: operations.history_context(ctx["params"]["game"], ctx["params"]["result"], ctx["params"]["page"]),
            game_scoped=False,
            params={"game": ("string", "遊戲代號，沒有篩選就填空字串", "game"),
                    "result": ("string", "結果，沒有篩選就填空字串", "result"),
                    "page": ("integer", "第幾頁", "page")}),
    OneStep("guide", "load_guide", "使用者打開了「怎麼玩」頁面，請載入各遊戲的規則。",
            lambda ctx: operations.guide_context(), game_scoped=False),
    OneStep("play_page", "open_game_page", "使用者打開了遊戲「{game}」的對局頁，請載入這個遊戲的頁面設定。",
            lambda ctx: operations.play_context(ctx["module"]), game_scoped=False, params={"game": GAME_KEY}),
    OneStep("end_form", "end_game", "使用者在對局清單上按了第 {game_id} 盤的「結束（不計勝負）」，請結束這盤。",
            lambda ctx: operations.end_game(_game(ctx)) or {}, game_scoped=False, params={"game_id": GAME_ID}),
    OneStep("models", "load_models", "使用者打開了遊戲「{game}」的設定，請載入可以選的 AI 模型與狀態。",
            lambda ctx: narrator.models_status(ctx["module"].key), params={"game": GAME_KEY}),
    OneStep("list_games", "list_games", "使用者要看遊戲「{game}」的對局清單，請載入。",
            lambda ctx: operations.list_games(ctx["module"]), params={"game": GAME_KEY}),
    OneStep("detail", "load_game", "使用者要打開第 {game_id} 盤（復盤或接著下），請載入這盤棋。",
            lambda ctx: operations.game_detail(ctx["module"], _game(ctx)), params={"game_id": GAME_ID}),
    OneStep("end", "end_game", "使用者在對局頁按了第 {game_id} 盤的「結束（不計勝負）」，請結束這盤。",
            lambda ctx: operations.end_game(_game(ctx)) or {"game": _game(ctx).summary()}, params={"game_id": GAME_ID}),
    OneStep("review", "review_game", "使用者按了第 {game_id} 盤的「復盤」，請檢查這盤棋的問題。",
            lambda ctx: operations.review(ctx["module"], _game(ctx)), params={"game_id": GAME_ID},
            summary=lambda data: f"找到 {len(data['moments'])} 個問題。"),
    OneStep("note", "explain_note", "使用者要看第 {game_id} 盤第 {index} 步 AI 的思路說明，請產生說明。",
            lambda ctx: operations.narrate_note(ctx["module"], ctx["note"], ctx["params"]["model"]),
            params={"game_id": GAME_ID, "index": ("integer", "第幾步（從 0 起算）", "index")}),
    OneStep("narrate", "explain_facts", "使用者要看一段分析的白話說明，請產生說明。",
            lambda ctx: operations.narrate(ctx["module"], ctx["params"]["facts"], ctx["params"]["model"])),
    NewGame(), Move(), Ask(),
]}


def handle(kind, ctx):
    """處理一個請求，回傳 (結果, handled)。handled 是 {"by": "agent"|"fallback"|"direct", "seconds", ...}，
    fallback 時另有給玩家看的 notice。"""
    spec = REQUESTS[kind]
    module = ctx.get("module")
    if spec.game_scoped and not module.agentic_requests:
        spec.fallback(ctx)
        return spec.respond(ctx), {"by": "direct"}
    start = time.perf_counter()
    run = {"model": narrator.model_config()["name"], "steps": REQUEST_STEPS, "calls": 0, "model_seconds": 0.0,
           "rejected": 0, "outcome": "ok", "problem": ""}
    try:
        outcome, problem = _run_agent(spec, ctx, run)
    except ValueError as exc:
        # 請求本身不成立（例如不合法的一步）：Agent 照請求呼叫了工具，是請求的問題，照原本的方式回覆錯誤
        _record(kind, module, ctx, run, start, "ok", f"請求不成立：{exc}")
        raise
    if outcome != "ok":
        try:
            spec.fallback(ctx)
        except ValueError:
            _record(kind, module, ctx, run, start, outcome, problem)
            raise
    _record(kind, module, ctx, run, start, outcome, problem)
    handled = {"by": "agent" if outcome == "ok" else "fallback", "seconds": run["seconds"]}
    if outcome != "ok":
        handled.update(notice=NOTICES[outcome], problem=problem)
    return spec.respond(ctx), handled


def _record(kind, module, ctx, run, start, outcome, problem):
    run.update(outcome=outcome, problem=problem, seconds=round(time.perf_counter() - start, 2),
               model_seconds=round(run["model_seconds"], 2))
    game = ctx.get("game")
    params = ctx["params"]
    ModelRun.record(f"req_{kind}", module.key if module else "platform", run, game if game and game.pk else None,
                    params.get("at", params.get("index")))


def _run_agent(spec, ctx, run):
    """Agent 迴圈，回傳 (outcome, problem)：ok／error（呼叫不到模型）／fallback（沒完成）。"""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": spec.task(ctx)}]
    for _ in range(REQUEST_STEPS):
        started = time.perf_counter()
        run["calls"] += 1
        try:
            message = _chat(messages, spec.tools)
        except (OSError, ValueError, KeyError) as exc:
            return "error", f"{agent.UNREACHABLE}：{exc}"
        finally:
            run["model_seconds"] += time.perf_counter() - started
        calls = message.get("tool_calls") or []
        messages.append({"role": "assistant", "content": message.get("content", ""), "tool_calls": calls})
        if not calls:
            break  # Agent 認為完成了
        for call in calls:
            name = call.get("function", {}).get("name", "")
            args = call.get("function", {}).get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            if not isinstance(args, dict):
                args = {}
            messages.append({"role": "tool", "tool_name": name, "content": spec.execute(ctx, name, args, run)})
        if spec.auto_finish and spec.done(ctx):
            break
    if spec.done(ctx):
        return "ok", ""
    return "fallback", f"{REQUEST_STEPS} 次內沒有完成" if run["calls"] >= REQUEST_STEPS else "Agent 說完成了，但請求還沒完成"
