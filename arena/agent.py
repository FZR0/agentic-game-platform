"""Agent 棋手（所有遊戲共用的迴圈）：語言模型透過工具使用引擎，自己決定要查什麼、最後下哪裡。

工具的內容由各遊戲模組提供，結果都是引擎算出來、附證據種類的事實。
程式守住底線：不合法或違反硬性規定的行動會被退回；最後一次只提供 play_move，逼 Agent 做決定；
模型出錯或仍在次數內沒有決定時，改由引擎代下並記下原因與已做的步驟。
"""
import json
import time

from django.conf import settings

from . import narrator
from .base import ESTIMATE, as_facts

MIN_STEPS, MAX_STEPS = 2, 10  # 「Agent 每手查證次數」設定的允許範圍
UNREACHABLE = "無法呼叫模型"  # 呼叫 Ollama 出錯時的代下原因；測速靠它分辨「模型答不出」和「根本呼叫不到」


def _chat(messages, config, tools):
    body = {"model": config["name"], "stream": False, "think": config["think"], "keep_alive": settings.OLLAMA_KEEP_ALIVE,
            "tools": tools, "messages": messages, "options": {"temperature": 0.2, "num_predict": 400,
                                                            "num_ctx": settings.OLLAMA_NUM_CTX}}
    return narrator.chat(body)["message"]


def _timed_chat(messages, config, tools, run):
    """呼叫模型，同時把次數與等待的秒數記進 run（出錯時也算）。"""
    start = time.perf_counter()
    run["calls"] += 1
    try:
        return _chat(messages, config, tools)
    finally:
        run["model_seconds"] += time.perf_counter() - start


def agent_move(module, state, actions, model=None, steps=None):
    """由 Agent 決定下一步。回傳 {"action", "facts", "narration", "fallback", "run"}。
    run 是這一手用了模型的紀錄（呼叫端存成 ModelRun）；不需要模型的步驟為 None。"""
    config = narrator.model_config(model)
    steps = steps or module.default_steps
    start = time.perf_counter()
    forced = module.forced(state)
    if forced:
        return _result(forced["action"], forced["points"], None, False)
    run = {"model": config["name"], "steps": steps, "calls": 0, "model_seconds": 0.0, "rejected": 0}

    seat = module.to_move(state)
    messages = [{"role": "system", "content": module.agent_prompt(seat)},
                {"role": "user", "content": module.board_text(state, actions)}]
    play_only = [t for t in module.tools if t["function"]["name"] == "play_move"]
    points, done = [], []
    for step in range(steps):
        tools = module.tools
        if step == steps - 1:
            # 查證次數用完時只給 play_move，逼 Agent 根據已查到的結果做決定，而不是直接換引擎代下
            tools = play_only
            messages.append({"role": "user", "content": "查證次數已用完，請根據目前的結果立刻呼叫 play_move。"})
        try:
            message = _timed_chat(messages, config, tools, run)
        except (OSError, ValueError, KeyError) as exc:
            why = f"{UNREACHABLE}：{exc}"
            return _fallback(module, state, why, done, _finish(run, start, "error", why))
        calls = message.get("tool_calls") or []
        messages.append({"role": "assistant", "content": message.get("content", ""), "tool_calls": calls})
        if not calls:
            messages.append({"role": "user", "content": "請呼叫工具查證，或呼叫 play_move。"})
            continue
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
            if name == "play_move":
                action = module.action_from(args)
                problem = module.rejection(state, action)
                if problem is None:
                    if not points:
                        points.append((ESTIMATE, f"Agent 沒有先用工具查證，就決定{module.step_text(action)}。"))
                    done.append(module.step_text(action))
                    reason = str(args.get("reason", "")).strip()
                    return _result(action, points, _narration(module, reason, points, done, config, start), False,
                                   _finish(run, start, "ok"))
                run["rejected"] += 1
                done.append(f"想{module.step_text(action or str(args))} 被退回（{problem}）")
                result = f"不能下：{problem}。請重新選擇。"
            else:
                found = module.run_tool(state, name, args)
                if found is None:
                    result = f"沒有 {name} 這個工具，或參數格式不對。"
                else:
                    label, tool_points = found
                    done.append(label)
                    points.extend(tool_points)
                    result = "\n".join(text for _, text in tool_points)
            messages.append({"role": "tool", "tool_name": name, "content": result})
    why = f"{steps} 次內沒有決定"
    return _fallback(module, state, why, done, _finish(run, start, "fallback", why))


def _narration(module, reason, points, done, config, start):
    facts = {"points": as_facts(points)}
    # Agent 自己寫的理由同樣要通過說明檢查：不能提到沒查證過的座標，也不能過度確定
    key = reason if reason and narrator.check_text(reason, facts, module.coord_re) is None else None
    return {"key": key, "text": f"查證過程：{'→'.join(done)}。", "source": "agent",
            "model": config["name"], "label": config["label"],
            "seconds": round(time.perf_counter() - start, 1)}


def _finish(run, start, outcome, problem=""):
    """補上這一手的總秒數與結果（outcome 的意思見 models.ModelRun.OUTCOMES）。"""
    run.update(seconds=round(time.perf_counter() - start, 2), model_seconds=round(run["model_seconds"], 2),
               outcome=outcome, problem=problem)
    return run


def _fallback(module, state, why, done, run=None):
    decision = module.engine_move(state)
    # 記下 Agent 已做的步驟，之後才查得出為什麼沒完成
    note = (ESTIMATE, f"Agent 沒有完成這一手（{why}；已做的步驟：{'→'.join(done) or '無'}），改由引擎代下。")
    return _result(decision["action"], [note] + list(decision["points"]), None, True, run)


def _result(action, points, narration, fallback, run=None):
    return {"action": action, "narration": narration, "fallback": fallback, "run": run,
            "facts": {"kind": "ai_move", "points": as_facts(points)}}
