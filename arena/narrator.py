"""把引擎產生的事實交給 Ollama 改寫成自然的說明（所有遊戲共用）。

語言模型只負責措辭：說明送出前會檢查，若提到事實裡沒有的座標，或在只有引擎估計時
使用確定的用語，就改用事實原文（範本說明）。Ollama 無法使用時也是如此。
"""
import json
import logging
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from django.conf import settings

from .base import CONDITIONAL, ESTIMATE

log = logging.getLogger(__name__)

CERTAIN_WORDS = ("必勝", "一定", "必然", "絕對")
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
UNAVAILABLE = "Ollama 無法使用"  # 呼叫不到模型時的說明備註；模型使用紀錄靠它分辨「呼叫不到」和「沒通過檢查」
# 思考模式的推理過程也算在生成長度內，需要比較大的上限
NUM_PREDICT = {False: 300, True: 3000}

SYSTEM_PROMPT = """你是{game}教練，負責把分析引擎提供的事實改寫成自然、易懂的說明。
輸出固定兩行：
重點：一句話（25 字以內），直接說出最重要的結論。
說明：1 到 3 句，補充理由。
規則：
1. 使用繁體中文（台灣用語），不要條列、不要 Markdown 符號。
2. 不要逐條複述事實，也不要從「目前輪到誰下」開始講；只挑和要說明的事最相關的一兩點。
3. 只能使用提供的事實，不可自行推測新的棋步或座標；提到的座標必須出現在事實中。
4. 依證據種類用語：「規則確認」可以直接陳述；「求解確認」要保留「在搜尋範圍內已確認」；「條件式計算」要說出所依據的假設；「引擎估計」要說「依引擎估計」，不可說「必勝」或「一定」。"""

INTROS = {
    "ai_move": "以下是 AI 這一手的分析事實，請用 AI 第一人稱說明這一手的思路：",
    "review": "以下是玩家這一手的復盤事實，請用教練的口吻對玩家說明問題在哪裡，以及更好的下法：",
    # 整體評論（使用者要求，2026-10-11）：只根據列出的統計與問題下結論
    "overall": ("以下是玩家這一整盤的檢查結果與統計。請用教練的口吻寫整體評論：「重點」一句話總結這盤整體表現；"
                "「說明」寫最值得改進的一兩點、以後可以怎麼做，再提一個做得好的地方（沒有就不用提）。"
                "只能根據下面列出的事實；數字好不好以事實裡寫的評語為準，事實沒有評語的數字只能照實說，不要自己判斷好壞："),
}
ASK_INTRO = ("請直接回答上面這個問題：「重點」那一句必須正面回應玩家問的事，不要先描述局面。"
             "如果玩家問的是自己的習慣或感受（例如為什麼常看不到某個點），就指出那個點的威脅是什麼、"
             "在哪個方向，並給一個下次可以檢查的習慣。只根據下面的引擎事實回答；"
             "事實不足以回答時，就直接說「引擎目前無法判斷」，不要猜測。")
# 模型輸出的「重點：…」與「說明：…」，容許前後的 Markdown 符號與同一行接續
KEY_RE = re.compile(r"^\W*重點\W*?[:：]\s*(?P<key>.+?)\s*(?:\n|(?=說明\W*?[:：])|\Z)(?P<body>.*)$", re.S)
BODY_LABEL_RE = re.compile(r"^\W*說明\W*?[:：]\s*")


def model_config(name=None):
    """回傳設定中的模型；name 為 None 時用預設模型。不在清單中的模型丟出 ValueError。"""
    name = name or settings.OLLAMA_MODEL
    for config in settings.OLLAMA_MODELS:
        if config["name"] == name:
            return config
    raise ValueError(f"不支援的模型：{name}")


def narrate(facts, model, module):
    config = model_config(model)
    template = "".join(p["text"] for p in facts["points"])
    try:
        text = _ask_ollama(facts, config, module.name).replace("**", "")
    except (OSError, ValueError, KeyError) as exc:
        return {"key": None, "text": template, "source": "template", "note": f"{UNAVAILABLE}（{exc}）"}
    problem = check_text(text, facts, module.coord_re)
    if problem:
        return {"key": None, "text": template, "source": "template", "note": problem}
    key, body = split_key_point(text)
    return {"key": key, "text": body, "source": "llm", "model": config["name"], "label": config["label"]}


def split_key_point(text):
    """拆出模型回答中的「重點」那一句；格式不符時 key 為 None、全文當說明。"""
    m = KEY_RE.match(text.strip())
    if not m:
        return None, text.strip()
    body = " ".join(BODY_LABEL_RE.sub("", line).strip() for line in m.group("body").splitlines() if line.strip())
    return m.group("key").strip(), body


def check_text(text, facts, coord_re):
    """說明檢查：回傳問題描述，沒有問題時回傳 None。coord_re 是該遊戲的座標格式。"""
    allowed = set(coord_re.findall("".join(p["text"] for p in facts["points"])))
    extra = set(coord_re.findall(text)) - allowed
    if extra:
        return f"模型提到事實中沒有的座標：{'、'.join(sorted(extra))}"
    only_estimates = all(p["evidence"] in (ESTIMATE, CONDITIONAL) for p in facts["points"])
    if only_estimates and any(w in text for w in CERTAIN_WORDS):
        return "只有引擎估計或條件式計算，模型卻使用了確定的用語"
    return None


def request_body(facts, config, game_name):
    if facts["kind"] == "ask":
        lines = [f"玩家的問題：「{facts['question']}」", ASK_INTRO, "引擎事實："]
    else:
        lines = [INTROS[facts["kind"]]]
    lines += [f"- [{p['evidence']}] {p['text']}" for p in facts["points"]]
    return {
        "model": config["name"],
        "stream": False,
        "think": config["think"],
        "keep_alive": settings.OLLAMA_KEEP_ALIVE,
        "options": {"temperature": 0.3, "num_predict": NUM_PREDICT[config["think"]],
                    "num_ctx": settings.OLLAMA_NUM_CTX},
        "messages": [{"role": "system", "content": SYSTEM_PROMPT.format(game=game_name)},
                     {"role": "user", "content": "\n".join(lines)}],
    }


_retry_at = 0.0  # 呼叫 Ollama 連不上或逾時後，在這個時間（time.monotonic）以前不再呼叫


def chat(body):
    """送出 /api/chat 並回傳結果（Agent 與說明共用）。連不上或逾時後的 OLLAMA_RETRY_AFTER 秒內直接丟出 OSError，
    讓呼叫端立刻改用引擎：伺服器卡住時只有第一次要等到逾時，不會每一步都等 OLLAMA_TIMEOUT 秒。"""
    global _retry_at
    where = f"{server_label()}的 {body.get('model')}"
    wait = _retry_at - time.monotonic()
    if wait > 0:
        log.warning("沒有呼叫%s：AI 伺服器剛才沒有回應，暫停呼叫，約 %d 秒後再試", where, math.ceil(wait))
        raise OSError(f"AI 伺服器剛才沒有回應，約 {math.ceil(wait / 60)} 分鐘後再試")
    req = urllib.request.Request(f"{settings.OLLAMA_URL}/api/chat", data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=settings.OLLAMA_TIMEOUT) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        # 伺服器有回應（例如模型不存在），不是卡住，不必暫停
        log.warning("呼叫%s 失敗（伺服器回覆錯誤，%.1f 秒）：%s", where, time.monotonic() - started, exc)
        raise
    except OSError as exc:
        _retry_at = time.monotonic() + settings.OLLAMA_RETRY_AFTER
        log.warning("呼叫%s 失敗（連不上或逾時，%.1f 秒）：%s；之後 %d 秒內不再呼叫", where,
                    time.monotonic() - started, exc, settings.OLLAMA_RETRY_AFTER)
        raise
    log.info("呼叫%s：%.1f 秒", where, time.monotonic() - started)
    return data


def server_label():
    """紀錄裡寫的 Ollama 伺服器：本機、或 OLLAMA_SERVER_NAMES 的代稱（研究用伺服器的位址不能寫出來）。"""
    host = urllib.parse.urlsplit(settings.OLLAMA_URL).hostname
    if host in LOCAL_HOSTS:
        return "本機"
    return f"{settings.OLLAMA_SERVER_NAMES.get(host, '未命名的')} 伺服器"


def _ask_ollama(facts, config, game_name):
    data = chat(request_body(facts, config, game_name))
    # 開啟思考模式時，推理過程放在 message.thinking，content 只有答案
    text = re.sub(r"<think>.*?</think>", "", data["message"]["content"], flags=re.S).strip()
    if not text:
        raise ValueError("模型沒有回傳內容")
    return text


def installed_models():
    """Ollama 已安裝的模型名稱；Ollama 沒開時回傳 None。"""
    try:
        with urllib.request.urlopen(f"{settings.OLLAMA_URL}/api/tags", timeout=3) as resp:
            return {m["name"] for m in json.load(resp)["models"]}
    except (OSError, ValueError, KeyError):
        return None


def benchmark_file():
    """目前 Ollama 伺服器的測速檔（{遊戲: {模型: 實測結果}}）。
    同名模型在不同機器上的速度不同，所以每台伺服器各存一個檔；本機沿用 model_benchmarks.json。"""
    host = urllib.parse.urlsplit(settings.OLLAMA_URL).hostname
    if host in LOCAL_HOSTS:
        return Path(__file__).with_name("model_benchmarks.json")
    # 用伺服器的代稱命名（settings.OLLAMA_SERVER_NAMES），公開的儲存庫裡才不會露出位址（2026-10-21）
    return Path(__file__).with_name(f"model_benchmarks_{settings.OLLAMA_SERVER_NAMES.get(host, host)}.json")


def load_benchmarks():
    try:
        return json.loads(benchmark_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def models_status(game_key):
    """給前端的模型清單：依設定順序（智慧程度由高到低），附安裝狀態與這個遊戲的實測速度。"""
    installed = installed_models()
    benchmarks = load_benchmarks().get(game_key, {})
    return {
        "online": installed is not None,
        "default": settings.OLLAMA_MODEL,
        "models": [{"name": c["name"], "label": c["label"], "think": c["think"], "agent": c.get("agent", True),
                    "installed": bool(installed and c["name"] in installed),
                    "benchmark": benchmarks.get(c["name"])} for c in settings.OLLAMA_MODELS],
    }
