"""所有遊戲共用的頁面與 API：/<遊戲>/ 與 /api/<遊戲>/...；遊戲本身的規則都透過 GameModule。

每個請求都交給 AI 助理（orchestrator.handle）處理（使用者決定，2026-10-28）；這裡只解析 HTTP、驗證輸入格式，
再把結果交給模板或轉成 JSON。回應都多一個 handled：AI 助理沒完成時，畫面依它的 notice 提示使用者。
"""
import json

from django.contrib import messages
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from . import agent, games, health, narrator, operations, orchestrator, realtime
from .base import EVIDENCE_TYPES
from .models import AiNote, Game
from .operations import RESULTS

QUESTION_MAX_LENGTH = 200


def _module(game_key):
    try:
        return games.get(game_key)
    except KeyError:
        raise Http404(f"沒有 {game_key} 這個遊戲")


def _read_json(request):
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        raise ValueError("請求內容不是有效的 JSON")
    if not isinstance(data, dict):
        raise ValueError("請求內容必須是 JSON 物件")
    return data


def _error(exc):
    return JsonResponse({"error": str(exc)}, status=400)


def _agent_options(data, module):
    """請求中指定的 Agent 模型與每手查證次數；不支援時丟出 ValueError。"""
    config = narrator.model_config(data.get("model"))
    if not config.get("agent", True):
        raise ValueError(f"{config['label']} 太慢，不適合擔任 Agent 棋手")
    steps = int(data.get("steps") or module.default_steps)
    if not agent.MIN_STEPS <= steps <= agent.MAX_STEPS:
        raise ValueError(f"每手查證次數必須在 {agent.MIN_STEPS} 到 {agent.MAX_STEPS} 之間")
    return config["name"], steps


def _game(request, module, game_id, watch=False):
    """這位使用者看得到的對局（自己的；舊對局只給管理員），別人的一律當成不存在。
    watch：也包括觀戰中的對局（只有打開對局用；觀戰的人不能下棋、不能用 AI 教練）。"""
    found = Game.watchable_by(request.user) if watch else Game.visible_to(request.user)
    return get_object_or_404(found, pk=game_id, game_type=module.key)


def _with_user(request, ctx):
    """請求的帳號，以及他在這盤坐的座位（真人對戰時每個人的畫面、復盤都從自己的座位看）。"""
    ctx = {**ctx, "user": request.user}
    if "game" in ctx:
        ctx["seat"] = ctx["game"].seat_of(request.user)
    return ctx


def _json(request, kind, ctx):
    """交給 AI 助理處理，回傳 JSON；請求不成立（ValueError，例如不合法的一步）時回覆錯誤。"""
    try:
        data, handled = orchestrator.handle(kind, _with_user(request, ctx))
    except ValueError as exc:
        return _error(exc)
    # ai_problem：AI 伺服器目前的問題（health，2026-10-30），沒有問題時是 None，畫面據此顯示或收起說明
    return JsonResponse({**data, "handled": handled, "ai_problem": health.current()})


def _page(request, template, kind, ctx):
    data, handled = orchestrator.handle(kind, _with_user(request, ctx))
    return render(request, template, {**data, "handled": handled, "ai_problem": health.current()})


# ---- 頁面 ----

def home(request):
    return _page(request, "arena/home.html", "home", {"params": {}})


def history(request):
    game_key = request.GET.get("game", "")
    result = request.GET.get("result", "")
    params = {"game": game_key if game_key in games.GAMES else "", "result": result if result in RESULTS else "",
              "page": request.GET.get("page") or 1}
    return _page(request, "arena/history.html", "history", {"params": params})


def guide(request):
    return _page(request, "arena/guide.html", "guide", {"params": {}})


@require_POST
def end_game(request, game_id):
    """首頁、對局紀錄清單上的「結束」按鈕（表單送出）；結束後回到原本的頁面。"""
    game = get_object_or_404(Game.visible_to(request.user), pk=game_id)
    if not game.finished:
        try:
            _, handled = orchestrator.handle("end_form", _with_user(request, {"game": game, "params": {"game_id": game.id}}))
        except ValueError as exc:  # 真人對戰不能「不計勝負」
            handled = {"notice": str(exc)}
        if handled.get("notice"):  # 轉址後的頁面才看得到，所以放進 messages
            messages.warning(request, handled["notice"])
    target = request.POST.get("next", "")
    if not url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}):
        target = reverse("arena:home")
    return redirect(target)


def join(request, code=""):
    """加入真人對戰（2026-11-05）：邀請連結 /join/<房間密碼>/ 先顯示確認頁，按「加入」才坐進座位
    （用 GET 直接加入的話，別的網站放一張圖片就能讓你坐進陌生的對局）。"""
    error = ""
    if request.method == "POST":
        code = request.POST.get("code", "")
        watch = request.POST.get("action") == "watch"  # 「觀戰」按鈕（2026-11-08）；其他都是「加入」
        try:
            data, handled = orchestrator.handle("watch" if watch else "join",
                                                _with_user(request, {"params": {"code": code.strip().upper()}}))
        except ValueError as exc:
            error = str(exc)
        else:
            if handled.get("notice"):
                messages.warning(request, handled["notice"])
            if watch and data["seat"] is None:
                return redirect(f"{reverse('arena:play', args=[data['module'].key])}?watch={data['game'].id}")
            if not watch:
                realtime.notify(data["game"])
            return redirect(f"{reverse('arena:play', args=[data['module'].key])}?resume={data['game'].id}")
    data, handled = orchestrator.handle("join_page", _with_user(request, {"params": {}}))
    return render(request, "arena/join.html", {**data, "handled": handled, "ai_problem": health.current(),
                                               "code": code.strip().upper(), "error": error})


def play(request, game_key):
    module = _module(game_key)
    return _page(request, "arena/play.html", "play_page", {"module": module, "params": {"game": module.key}})


# ---- API ----

@require_GET
def api_models(request, game_key):
    module = _module(game_key)
    return _json(request, "models", {"module": module, "params": {"game": module.key}})


@require_http_methods(["GET", "POST"])
def api_games(request, game_key):
    module = _module(game_key)
    if request.method == "GET":
        return _json(request, "list_games", {"module": module, "params": {"game": module.key}})
    try:
        data = _read_json(request)
        seat = int(data["seat"])
        if seat not in module.seat_names:
            raise ValueError(f"座位必須是 {'、'.join(map(str, module.seat_names))} 其中之一")
        settings = module.clean_settings(data.get("settings") or {})
        mode = data.get("mode") or "ai"
        if mode not in Game.MODES:
            raise ValueError("對手只能是 AI 或真人")
        if mode == "ai" and settings.get("humans"):  # 撲克自選的真人座位只用在真人對戰
            raise ValueError("和 AI 對戰時只有你一位真人；要和朋友一起玩請選真人對戰")
        model, steps = _agent_options(data, module)
    except (ValueError, KeyError, TypeError) as exc:
        return _error(exc)
    return _json(request, "new_game", {"module": module, "params": {"game": module.key, "seat": seat, "settings": settings,
                                                                    "mode": mode, "model": model, "steps": steps}})


@require_GET
def api_game_detail(request, game_key, game_id):
    module = _module(game_key)
    game = _game(request, module, game_id, watch=True)
    return _json(request, "detail", {"module": module, "game": game, "params": {"game_id": game.id}})


@require_POST
def api_room_settings(request, game_key, game_id):
    """房主在朋友加入前改房間設定（2026-11-08）：同一個房間、同一組房間密碼，換成新的設定重新開局。"""
    module = _module(game_key)
    game = _game(request, module, game_id)
    try:
        data = _read_json(request)
        seat = int(data["seat"])
        if seat not in module.seat_names:
            raise ValueError(f"座位必須是 {'、'.join(map(str, module.seat_names))} 其中之一")
        settings = module.clean_settings(data.get("settings") or {})
    except (ValueError, KeyError, TypeError) as exc:
        return _error(exc)
    response = _json(request, "room_settings", {"module": module, "game": game,
                                                "params": {"game_id": game.id, "seat": seat, "settings": settings}})
    if response.status_code == 200:
        realtime.notify(game)  # 觀戰中的人跟著換成新的棋盤
    return response


@require_POST
def api_game_move(request, game_key, game_id):
    """玩家行動：伺服器驗證後存檔，再由 AI 的座位行動，直到又輪到玩家。"""
    module = _module(game_key)
    game = _game(request, module, game_id)
    try:
        if game.finished:
            raise ValueError("這盤棋已經結束")
        data = _read_json(request)
        model, steps = _agent_options(data, module)
        action = module.normalize(data["action"])
        if action is None:
            raise ValueError(f"行動格式不對：{data['action']}")
    except (ValueError, KeyError, TypeError) as exc:
        return _error(exc)
    response = _json(request, "move", {"module": module, "game": game,
                                       "params": {"game_id": game.id, "action": action, "model": model, "steps": steps}})
    if game.pvp and response.status_code == 200:
        realtime.notify(game)  # 同一盤的其他真人馬上看到這一步（WebSocket）
    return response


@require_POST
def api_game_end(request, game_key, game_id):
    """對局頁的「結束（不計勝負）」。"""
    module = _module(game_key)
    game = _game(request, module, game_id)
    return _json(request, "end", {"module": module, "game": game, "params": {"game_id": game.id}})


@require_POST
def api_note_narrate(request, game_key, game_id, index):
    """產生 AI 某一步的說明。模型成功改寫後存起來，之後（例如復盤時）直接回傳同一段說明。"""
    module = _module(game_key)
    note = get_object_or_404(AiNote, game=_game(request, module, game_id), index=index)
    try:
        operations.coach_allowed(note.game)
        model = narrator.model_config(_read_json(request).get("model"))["name"]
    except (ValueError, TypeError) as exc:
        return _error(exc)
    return _json(request, "note", {"module": module, "game": note.game, "note": note,
                          "params": {"game_id": note.game_id, "index": index, "model": model}})


@require_GET
def api_game_review(request, game_key, game_id):
    module = _module(game_key)
    game = _game(request, module, game_id)
    try:
        operations.coach_allowed(game)  # 真人對戰進行中不能復盤：先擋下，不必呼叫 AI 助理
    except ValueError as exc:
        return _error(exc)
    return _json(request, "review", {"module": module, "game": game, "params": {"game_id": game.id}})


@require_POST
def api_game_ask(request, game_key, game_id):
    """復盤提問：由 AI 助理決定要查哪些局面與步數（引擎分析），再請語言模型依事實回答。"""
    module = _module(game_key)
    game = _game(request, module, game_id)
    try:
        operations.coach_allowed(game)
        data = _read_json(request)
        question = str(data["question"]).strip()
        at = int(data["at"])
        model = narrator.model_config(data.get("model"))["name"]
        if not question or len(question) > QUESTION_MAX_LENGTH:
            raise ValueError(f"問題必須是 1 到 {QUESTION_MAX_LENGTH} 個字")
        if not 0 <= at <= len(game.actions):
            raise ValueError("局面超出這盤棋的步數")
    except (ValueError, KeyError, TypeError) as exc:
        return _error(exc)
    return _json(request, "ask", {"module": module, "game": game,
                         "params": {"game_id": game.id, "question": question, "at": at, "model": model}})


@require_POST
def api_narrate(request, game_key):
    module = _module(game_key)
    try:
        data = _read_json(request)
        facts = data["facts"]
        model = narrator.model_config(data.get("model"))["name"]
        if facts["kind"] not in narrator.INTROS:
            raise ValueError("未知的說明類型")
        for p in facts["points"]:
            if p["evidence"] not in EVIDENCE_TYPES or not isinstance(p["text"], str):
                raise ValueError("事實格式錯誤")
    except (ValueError, KeyError, TypeError) as exc:
        return _error(exc)
    return _json(request, "narrate", {"module": module, "params": {"facts": facts, "model": model}})
