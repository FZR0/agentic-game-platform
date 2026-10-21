"""所有遊戲共用的頁面與 API：/<遊戲>/ 與 /api/<遊戲>/...；遊戲本身的規則都透過 GameModule。"""
import json
import re
import time
from collections import Counter
from pathlib import Path

from django.contrib.staticfiles import finders
from django.core.paginator import Paginator
from django.db.models import Count
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from . import agent, games, narrator
from .base import EVIDENCE_TYPES, RULE
from .models import AiNote, Game, ModelRun

QUESTION_MAX_LENGTH = 200
MOVE_REF_RE = re.compile(r"第\s*(\d{1,3})\s*[手步](?!牌)")  # 撲克的「第 3 手牌」不是第 3 步


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


def _settings(game):
    """交給遊戲模組的設定：存檔的設定加上只在伺服器的 secret（撲克的洗牌種子）。"""
    return {**game.settings, "secret": game.secret}


def _public(module, state, moves):
    """AI 思路送到網頁前先過濾：撲克這手牌還沒結束時遮住含底牌的思路。"""
    return module.public_notes(state, moves)


def _set_outcome(module, game, state):
    result = module.outcome(state)
    if result:
        game.winner, game.draw = result


def _ai_turns(module, game, state, model, steps):
    """輪到 AI 的座位時一直由 Agent 行動，直到輪到玩家或對局結束。回傳 (局面, AI 行動清單)。"""
    moves = []
    while not module.outcome(state) and module.to_move(state) != game.human_seat:
        seat = module.to_move(state)
        result = agent.agent_move(module, state, game.actions, model, steps)
        state = module.apply(state, result["action"])
        game.actions.append(result["action"])
        moves.append({"index": len(game.actions) - 1, "seat": seat, "action": result["action"],
                      "facts": result["facts"], "narration": result["narration"], "fallback": result["fallback"],
                      "run": result["run"], "frame": module.frame(state, game.human_seat)})
    _set_outcome(module, game, state)
    return state, moves


def _save_and_hide(module, game, state, moves):
    """存檔後回傳可以送到網頁的 AI 行動（思路已過濾）。"""
    game.save()
    for m in moves:
        AiNote.objects.create(game=game, index=m["index"], seat=m["seat"], facts=m["facts"], narration=m["narration"])
        run = m.pop("run")
        if run:  # 不需要模型的步驟（例如第一手）沒有紀錄
            ModelRun.record("agent", module.key, run, game, m["index"])
    return _public(module, state, moves)


def _narrate(module, facts, model, game=None, index=None):
    """請語言模型說明，同時記下這次用模型的秒數與結果（ModelRun）。"""
    start = time.perf_counter()
    result = narrator.narrate(facts, model, module)
    seconds = round(time.perf_counter() - start, 2)
    if result["source"] == "llm":
        outcome = "ok"
    else:
        outcome = "error" if result["note"].startswith(narrator.UNAVAILABLE) else "fallback"
    ModelRun.record(facts["kind"], module.key, {"model": model, "seconds": seconds, "model_seconds": seconds,
                                                "calls": 1, "outcome": outcome, "problem": result.get("note", "")},
                    game, index)
    return result


def _frames(module, game):
    state = module.new_state(_settings(game))
    frames = [module.frame(state, game.human_seat)]
    for action in game.actions:
        state = module.apply(state, action)
        frames.append(module.frame(state, game.human_seat))
    return frames


# ---- 頁面 ----

def _asset_version(*paths):
    """靜態檔最後修改時間，加在網址後面（?v=）。改了 JS 或 CSS 之後，瀏覽器一定會拿新版，不會沿用快取的舊檔。"""
    return int(max(Path(finders.find(p)).stat().st_mtime for p in paths))


RESULTS = {"win": "你贏", "loss": "AI 贏", "draw": "和局", "ended": "不計勝負", "unfinished": "未完成"}
HOME_RESUME = 3  # 首頁「繼續上次的對局」最多幾盤
HOME_RECENT = 5  # 首頁「最近對局」最多幾盤
HISTORY_PAGE_SIZE = 20


def _site_context(nav):
    """首頁、對局紀錄、怎麼玩共用：上方導覽列目前在哪一頁，以及靜態檔版本號。"""
    icons = [g.icon for g in games.GAMES.values()]
    return {"nav": nav, "games": games.GAMES.values(), "asset_version": _asset_version("arena/style.css", *icons)}


def _result(game):
    if game.ended:
        return "ended"
    if game.draw:
        return "draw"
    if game.winner:
        return "win" if game.winner == game.human_seat else "loss"
    return "unfinished"


def _played_games(**filters):
    """玩家至少下過一步的對局，最近下的在前（首頁與對局紀錄都用這個）。

    打開對局頁時如果 AI 先行動（例如撲克），對局一建立就存檔；玩家一步都沒下就離開的不列出，
    否則每打開一次撲克頁就多一盤「未完成」。AI 的每一步都有一筆 AiNote，所以行動數多於 AiNote 數就是玩家下過。
    """
    qs = Game.objects.filter(game_type__in=list(games.GAMES), **filters)
    ai_counts = dict(AiNote.objects.filter(game__in=qs).values("game").annotate(n=Count("id")).values_list("game", "n"))
    return [g for g in qs if len(g.actions) > ai_counts.get(g.id, 0)]


def _card(game):
    """對局清單的一列：遊戲、時間、玩家執哪一方、結果、手數，以及復盤／接著下的連結。"""
    module = games.get(game.game_type)
    result = _result(game)
    url = reverse("arena:play", args=[module.key])
    return {"id": game.id, "module": module, "when": game.updated_at, "steps": len(game.actions),
            # 撲克玩家固定 1 號位，寫「執 1 號位」沒有意義
            "seat": module.seat_names.get(game.human_seat, "") if len(module.seat_choices) > 1 else "",
            "result": result, "result_text": RESULTS[result], "review_url": f"{url}?review={game.id}",
            "resume_url": f"{url}?resume={game.id}" if result == "unfinished" else "",
            "end_url": reverse("arena:end_game", args=[game.id]) if result == "unfinished" else ""}


def home(request):
    played = _played_games()
    unfinished = [g for g in played if not g.finished]
    stats = []
    for module in games.GAMES.values():
        count = Counter(_result(g) for g in played if g.game_type == module.key)
        finished = count["win"] + count["loss"] + count["draw"]
        stats.append({"module": module, "win": count["win"], "loss": count["loss"], "draw": count["draw"],
                      "finished": finished, "rate": round(100 * count["win"] / finished) if finished else None})
    return render(request, "arena/home.html", {
        **_site_context("home"),
        "resume": [_card(g) for g in unfinished[:HOME_RESUME]],
        "more_unfinished": max(0, len(unfinished) - HOME_RESUME),
        "recent": [_card(g) for g in played if g.finished][:HOME_RECENT],
        "stats": stats, "any_finished": any(s["finished"] for s in stats)})


def history(request):
    game_key = request.GET.get("game", "")
    result = request.GET.get("result", "")
    game_key = game_key if game_key in games.GAMES else ""
    result = result if result in RESULTS else ""
    played = _played_games(**({"game_type": game_key} if game_key else {}))
    if result:
        played = [g for g in played if _result(g) == result]
    page = Paginator(played, HISTORY_PAGE_SIZE).get_page(request.GET.get("page"))
    return render(request, "arena/history.html", {
        **_site_context("history"), "page": page, "cards": [_card(g) for g in page],
        "game_key": game_key, "result": result, "results": RESULTS})


def guide(request):
    return render(request, "arena/guide.html", _site_context("guide"))


def _end(game):
    """「結束（不計勝負）」：對局停在這裡，不算輸贏、不進戰績、不能再下，仍可復盤（使用者要求，2026-10-21）。"""
    if game.finished:
        raise ValueError("這盤棋已經結束")
    game.ended = True
    game.save(update_fields=["ended", "updated_at"])


@require_POST
def end_game(request, game_id):
    """首頁、對局紀錄清單上的「結束」按鈕（表單送出）；結束後回到原本的頁面。"""
    game = get_object_or_404(Game, pk=game_id)
    if not game.finished:
        _end(game)
    target = request.POST.get("next", "")
    if not url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}):
        target = reverse("arena:home")
    return redirect(target)


def play(request, game_key):
    module = _module(game_key)
    return render(request, "arena/play.html", {
        "module": module,
        "asset_version": _asset_version("arena/style.css", "arena/app.js", module.board_script),
        "config": {"key": module.key, "name": module.name, "seatNames": module.seat_names,
                   "coordPattern": module.coord_pattern, "defaultSteps": module.default_steps,
                   "minSteps": agent.MIN_STEPS, "maxSteps": agent.MAX_STEPS, "stepWord": module.step_word}})


# ---- API ----

@require_GET
def api_models(request, game_key):
    return JsonResponse(narrator.models_status(_module(game_key).key))


@require_http_methods(["GET", "POST"])
def api_games(request, game_key):
    module = _module(game_key)
    if request.method == "GET":
        return JsonResponse({"games": [g.summary() for g in Game.objects.filter(game_type=module.key)[:30]]})
    try:
        data = _read_json(request)
        seat = int(data["seat"])
        if seat not in module.seat_names:
            raise ValueError(f"座位必須是 {'、'.join(map(str, module.seat_names))} 其中之一")
        settings = module.clean_settings(data.get("settings") or {})
        model, steps = _agent_options(data, module)
    except (ValueError, KeyError, TypeError) as exc:
        return _error(exc)
    game = Game(game_type=module.key, human_seat=seat, settings=settings, secret=module.new_secret())
    state = module.new_state(_settings(game))
    first_frame = module.frame(state, seat)
    state, moves = _ai_turns(module, game, state, model, steps)
    moves = _save_and_hide(module, game, state, moves)
    return JsonResponse({"game": game.summary(), "frame": first_frame, "ai_moves": moves})


@require_GET
def api_game_detail(request, game_key, game_id):
    module = _module(game_key)
    game = get_object_or_404(Game, pk=game_id, game_type=module.key)
    notes = _public(module, module.replay(_settings(game), game.actions), [n.to_dict() for n in game.ai_notes.all()])
    return JsonResponse({"game": game.summary(), "actions": game.actions, "frames": _frames(module, game),
                         "notes": notes})


@require_POST
def api_game_move(request, game_key, game_id):
    """玩家行動：伺服器驗證後存檔，再由 AI 的座位行動，直到又輪到玩家。"""
    module = _module(game_key)
    game = get_object_or_404(Game, pk=game_id, game_type=module.key)
    try:
        if game.finished:
            raise ValueError("這盤棋已經結束")
        data = _read_json(request)
        model, steps = _agent_options(data, module)
        state = module.replay(_settings(game), game.actions)
        if module.to_move(state) != game.human_seat:
            raise ValueError("現在不是你的回合")
        action = module.normalize(data["action"])
        if action is None:
            raise ValueError(f"行動格式不對：{data['action']}")
        state = module.apply(state, action)  # 不合法會丟出 ValueError
    except (ValueError, KeyError, TypeError) as exc:
        return _error(exc)
    game.actions.append(action)
    frame = module.frame(state, game.human_seat)
    state, moves = _ai_turns(module, game, state, model, steps)
    moves = _save_and_hide(module, game, state, moves)
    return JsonResponse({"action": action, "frame": frame, "ai_moves": moves,
                         "winner": game.winner, "draw": game.draw})


@require_POST
def api_game_end(request, game_key, game_id):
    """對局頁的「結束（不計勝負）」。"""
    game = get_object_or_404(Game, pk=game_id, game_type=_module(game_key).key)
    try:
        _end(game)
    except ValueError as exc:
        return _error(exc)
    return JsonResponse({"game": game.summary()})


@require_POST
def api_note_narrate(request, game_key, game_id, index):
    """產生 AI 某一步的說明。模型成功改寫後存起來，之後（例如復盤時）直接回傳同一段說明。"""
    module = _module(game_key)
    note = get_object_or_404(AiNote, game_id=game_id, game__game_type=module.key, index=index)
    state = module.replay(_settings(note.game), note.game.actions)
    if _public(module, state, [note.to_dict()])[0].get("hidden"):
        return _error("這一步的思路含底牌，這手牌結束後才能看")
    if note.narration:
        return JsonResponse(note.narration)
    try:
        model = narrator.model_config(_read_json(request).get("model"))["name"]
    except (ValueError, TypeError) as exc:
        return _error(exc)
    result = _narrate(module, note.facts, model, note.game, note.index)
    if result["source"] == "llm":  # 範本說明不存，Ollama 恢復後還能再試
        note.narration = result
        note.save(update_fields=["narration"])
    return JsonResponse(result)


LEVEL_NAMES = {"key": "關鍵時刻", "minor": "小失誤", "suggest": "建議", "good": "做得好的地方"}


def _main_evidence(moment):
    """一則問題主要的證據種類：規則確認以外最常見的那種（例如撲克是條件式計算）。"""
    kinds = Counter(p["evidence"] for p in moment["points"] if p["evidence"] != RULE)
    return kinds.most_common(1)[0][0] if kinds else RULE


def _overall(module, game, moments):
    """整體評論的事實（使用者要求，2026-10-11）：結果、各級別的數量、最常出現的問題、做得好的地方，加上遊戲自己的統計。
    語言模型只根據這些事實寫評論，跟其他說明一樣經過 narrator.check_text。"""
    state = module.replay(_settings(game), game.actions)
    result = module.outcome(state)
    if result:
        winner, draw = result
        verdict = "和局" if draw else "你贏了" if winner == game.human_seat else f"{module.seat_names[winner]}（AI）贏了"
        detail = module.frame(state, game.human_seat).get("result")
        points = [(RULE, f"結果：{verdict}" + (f"（{detail}）" if detail else "") + "。")]
    elif game.ended:
        points = [(RULE, "這盤沒有下完，是你選擇結束、不計勝負。")]
    else:
        points = [(RULE, "這盤還沒有下完。")]
    points.append((RULE, f"這盤一共 {len(game.actions)} {module.step_word}。"))
    counts = Counter(m["level"] for m in moments)
    found = "、".join(f"{name} {counts[level]} 個" for level, name in LEVEL_NAMES.items() if counts[level])
    points.append((RULE, f"問題檢查的結果：{found or '沒有找到任何問題'}。"))
    for levels, label, top in ((("key", "minor"), "最常出現的問題", 3), (("good",), "做得好的地方", 2)):
        group = [m for m in moments if m["level"] in levels]
        for pattern, n in Counter(m.get("pattern", m["title"]) for m in group).most_common(top):
            same = [m for m in group if m.get("pattern", m["title"]) == pattern]
            steps = "、".join(f"第 {m['index'] + 1} {module.step_word}" for m in same[:3])
            points.append((_main_evidence(same[0]), f"{label}：「{pattern}」{n} 次（{steps}）。"))
    points += module.overall_points(_settings(game), game.actions, game.human_seat)
    return {"kind": "overall", "points": [{"evidence": e, "text": t} for e, t in points]}


@require_GET
def api_game_review(request, game_key, game_id):
    module = _module(game_key)
    game = get_object_or_404(Game, pk=game_id, game_type=module.key)
    moments = module.review(_settings(game), game.actions, game.human_seat)
    overall = _overall(module, game, moments)
    for m in moments:
        m["facts"] = {"kind": "review", "points": m.pop("points")}
    return JsonResponse({"moments": moments, "overall": overall})


@require_POST
def api_game_ask(request, game_key, game_id):
    """復盤提問：先用引擎分析第 at 步之後的局面與問題中提到的座標或步數，再請語言模型依事實回答。"""
    module = _module(game_key)
    game = get_object_or_404(Game, pk=game_id, game_type=module.key)
    try:
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

    state = module.replay(_settings(game), game.actions[:at])
    result = module.outcome(state)
    if result:
        winner, _ = result
        ended = f"{module.seat_names[winner]}獲勝" if winner else "和局"
        points = [{"evidence": RULE, "text": f"這時對局已經結束（{ended}）。"}]
    else:
        next_action = game.actions[at] if at < len(game.actions) else None
        points = module.explain_position(state, module.question_focus(question), next_action)
    # 玩家常用「第 N 手」（撲克「第 N 步」）指稱；第 at+1 步就是上面的「實際下一步」，不重複
    refs = []
    for s in MOVE_REF_RE.findall(question):
        n = int(s)
        if 1 <= n <= len(game.actions) and n != at + 1 and n not in refs:
            refs.append(n)
    for n in refs[:2]:
        points += module.explain_action(_settings(game), game.actions, n)
    facts = {"kind": "ask", "question": question, "points": points}
    return JsonResponse({**_narrate(module, facts, model, game, at), "facts": facts})


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
    return JsonResponse(_narrate(module, facts, model))
