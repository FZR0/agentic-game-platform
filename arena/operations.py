"""平台的操作：開局、下棋、復盤、提問、各頁面的資料。

這些是 AI 助理（arena/orchestrator.py）的工具，也是它沒完成時的退回做法；views 只負責解析 HTTP 與輸出。
從 views.py 搬過來時行為不變（2026-10-28）；唯一的差別是開局與玩家下棋拆成「存檔」和「AI 行動」兩步，
讓 AI 助理一步一步呼叫，所以會多存一次檔。
"""
import re
import time
from collections import Counter
from pathlib import Path

from django.contrib.staticfiles import finders
from django.core.paginator import Paginator
from django.db.models import Count
from django.urls import reverse

from . import agent, games, narrator
from .base import RULE
from .models import AiNote, Game, ModelRun

MOVE_REF_RE = re.compile(r"第\s*(\d{1,3})\s*[手步](?!牌)")  # 撲克的「第 3 手牌」不是第 3 步


def settings_of(game):
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


def narrate(module, facts, model, game=None, index=None):
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
    state = module.new_state(settings_of(game))
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


def home_context():
    played = _played_games()
    unfinished = [g for g in played if not g.finished]
    stats = []
    for module in games.GAMES.values():
        count = Counter(_result(g) for g in played if g.game_type == module.key)
        finished = count["win"] + count["loss"] + count["draw"]
        stats.append({"module": module, "win": count["win"], "loss": count["loss"], "draw": count["draw"],
                      "finished": finished, "rate": round(100 * count["win"] / finished) if finished else None})
    return {**_site_context("home"),
            "resume": [_card(g) for g in unfinished[:HOME_RESUME]],
            "more_unfinished": max(0, len(unfinished) - HOME_RESUME),
            "recent": [_card(g) for g in played if g.finished][:HOME_RECENT],
            "stats": stats, "any_finished": any(s["finished"] for s in stats)}


def history_context(game_key, result, page):
    played = _played_games(**({"game_type": game_key} if game_key else {}))
    if result:
        played = [g for g in played if _result(g) == result]
    page = Paginator(played, HISTORY_PAGE_SIZE).get_page(page)
    return {**_site_context("history"), "page": page, "cards": [_card(g) for g in page],
            "game_key": game_key, "result": result, "results": RESULTS}


def guide_context():
    return _site_context("guide")


def play_context(module):
    return {"module": module,
            "asset_version": _asset_version("arena/style.css", "arena/app.js", module.board_script),
            "config": {"key": module.key, "name": module.name, "seatNames": module.seat_names,
                       "coordPattern": module.coord_pattern, "defaultSteps": module.default_steps,
                       "minSteps": agent.MIN_STEPS, "maxSteps": agent.MAX_STEPS, "stepWord": module.step_word}}


# ---- 對局 ----

def list_games(module):
    return {"games": [g.summary() for g in Game.objects.filter(game_type=module.key)[:30]]}


def start_game(module, seat, settings):
    """建立並存下新對局，回傳 (對局, 開局畫面)。AI 先行動的部分由 run_ai_turns 處理。"""
    game = Game(game_type=module.key, human_seat=seat, settings=settings, secret=module.new_secret())
    state = module.new_state(settings_of(game))
    game.save()
    return game, module.frame(state, seat)


def play_human_move(module, game, action):
    """玩家行動：伺服器驗證後存檔，回傳這一步之後的畫面。不合法時丟出 ValueError。"""
    if game.finished:
        raise ValueError("這盤棋已經結束")
    state = module.replay(settings_of(game), game.actions)
    if module.to_move(state) != game.human_seat:
        raise ValueError("現在不是你的回合")
    state = module.apply(state, action)  # 不合法會丟出 ValueError
    game.actions.append(action)
    _set_outcome(module, game, state)
    game.save()
    return module.frame(state, game.human_seat)


def run_ai_turns(module, game, model, steps):
    """由 AI 的座位行動，直到又輪到玩家或對局結束；存檔後回傳可以送到網頁的 AI 行動。"""
    state = module.replay(settings_of(game), game.actions)
    state, moves = _ai_turns(module, game, state, model, steps)
    return _save_and_hide(module, game, state, moves)


def game_detail(module, game):
    notes = _public(module, module.replay(settings_of(game), game.actions),
                    [n.to_dict() for n in game.ai_notes.all()])
    return {"game": game.summary(), "actions": game.actions, "frames": _frames(module, game), "notes": notes}


def end_game(game):
    """「結束（不計勝負）」：對局停在這裡，不算輸贏、不進戰績、不能再下，仍可復盤（使用者要求，2026-10-21）。"""
    if game.finished:
        raise ValueError("這盤棋已經結束")
    game.ended = True
    game.save(update_fields=["ended", "updated_at"])


def narrate_note(module, note, model):
    """AI 某一步的說明。模型成功改寫後存起來，之後（例如復盤時）直接回傳同一段說明。"""
    state = module.replay(settings_of(note.game), note.game.actions)
    if _public(module, state, [note.to_dict()])[0].get("hidden"):
        raise ValueError("這一步的思路含底牌，這手牌結束後才能看")
    if note.narration:
        return note.narration
    result = narrate(module, note.facts, model, note.game, note.index)
    if result["source"] == "llm":  # 範本說明不存，Ollama 恢復後還能再試
        note.narration = result
        note.save(update_fields=["narration"])
    return result


LEVEL_NAMES = {"key": "關鍵時刻", "minor": "小失誤", "suggest": "建議", "good": "做得好的地方"}


def _main_evidence(moment):
    """一則問題主要的證據種類：規則確認以外最常見的那種（例如撲克是條件式計算）。"""
    kinds = Counter(p["evidence"] for p in moment["points"] if p["evidence"] != RULE)
    return kinds.most_common(1)[0][0] if kinds else RULE


def _overall(module, game, moments):
    """整體評論的事實（使用者要求，2026-10-11）：結果、各級別的數量、最常出現的問題、做得好的地方，加上遊戲自己的統計。
    語言模型只根據這些事實寫評論，跟其他說明一樣經過 narrator.check_text。"""
    state = module.replay(settings_of(game), game.actions)
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
    points += module.overall_points(settings_of(game), game.actions, game.human_seat)
    return {"kind": "overall", "points": [{"evidence": e, "text": t} for e, t in points]}


def review(module, game):
    moments = module.review(settings_of(game), game.actions, game.human_seat)
    overall = _overall(module, game, moments)
    for m in moments:
        m["facts"] = {"kind": "review", "points": m.pop("points")}
    return {"moments": moments, "overall": overall}


# ---- 提問 ----

def position_points(module, game, at, focus):
    """第 at 步之後的局面事實；focus 是 module.question_focus 找到的座標或行動。"""
    state = module.replay(settings_of(game), game.actions[:at])
    result = module.outcome(state)
    if result:
        winner, _ = result
        ended = f"{module.seat_names[winner]}獲勝" if winner else "和局"
        return [{"evidence": RULE, "text": f"這時對局已經結束（{ended}）。"}]
    next_action = game.actions[at] if at < len(game.actions) else None
    return module.explain_position(state, focus, next_action)


def action_points(module, game, n):
    """第 n 步（從 1 起算）的事實。"""
    if not 1 <= n <= len(game.actions):
        raise ValueError(f"這盤只有 {len(game.actions)} {module.step_word}")
    return module.explain_action(settings_of(game), game.actions, n)


def ask_points_by_rules(module, game, question, at):
    """原本的做法：用正規表示式從問題找出座標與「第 N 手」，再查那些事實。"""
    points = position_points(module, game, at, module.question_focus(question))
    if module.outcome(module.replay(settings_of(game), game.actions[:at])):
        return points
    # 玩家常用「第 N 手」（撲克「第 N 步」）指稱；第 at+1 步就是上面的「實際下一步」，不重複
    refs = []
    for s in MOVE_REF_RE.findall(question):
        n = int(s)
        if 1 <= n <= len(game.actions) and n != at + 1 and n not in refs:
            refs.append(n)
    for n in refs[:2]:
        points += action_points(module, game, n)
    return points


def answer(module, game, question, at, points, model):
    """依查到的事實請語言模型回答提問。"""
    facts = {"kind": "ask", "question": question, "points": points}
    return {**narrate(module, facts, model, game, at), "facts": facts}
