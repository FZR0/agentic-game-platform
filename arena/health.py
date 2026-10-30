"""AI 伺服器（Ollama）出問題時的檢測與故障報告（使用者要求，2026-10-30）。

不定時連線檢查：呼叫失敗時（narrator.chat、narrator.installed_models）才檢查一次，結果保留到下一次呼叫成功
（narrator.chat 成功時 clear）。暫停呼叫期間（OLLAMA_RETRY_AFTER）請求不再連線，畫面一直顯示這次的結果。
- 原因多半從原本的錯誤就看得出來（被拒絕、找不到名稱、伺服器回覆的錯誤），不必再連線；
  只有逾時要多查一次：連得到埠嗎、/api/tags 有回應嗎、模型在不在，才分得出伺服器不在、Ollama 卡住、還是模型太慢。
- 查得出原因（known）：寫原因與排除步驟。查不出：寫可能原因，附故障報告。
- 每次查到新的問題都存一份故障報告到 settings.FAULT_REPORT_DIR（同一個問題持續時不重複存）。
畫面可以出現 Ollama 等技術字眼（使用者決定，2026-10-30），但研究用伺服器的位址不寫進畫面、紀錄或報告，
一律用 narrator.server_label 的代稱（_redact）。
"""
import errno
import json
import logging
import math
import os
import platform
import shutil
import socket
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from django.conf import settings

from . import narrator

log = logging.getLogger(__name__)

DEFAULT_PORT = 11434
OLLAMA_EXE = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
UNREACHABLE_ERRNOS = {errno.ENETUNREACH, errno.EHOSTUNREACH, 10051, 10065}  # 後兩個是 Windows 的代碼
ADDRESS_SETTING = "AI 伺服器位址設定在 private/settings.json（受邀成員）或 config/local.json 的 OLLAMA_URL，改完要重新啟動網站"

_current = None  # 目前的問題；None 表示沒有問題（或還沒發現）


def current():
    return _current


def clear():
    """呼叫成功：之前的問題已經解決。"""
    global _current
    if _current:
        log.info("AI 伺服器恢復正常（之前的問題：%s）", _current["title"])
    _current = None


def report_failure(exc, model):
    """呼叫失敗時檢查一次原因，記成目前的問題並回傳。"""
    return _remember(diagnose(exc, model), exc, model)


def check(model=None):
    """啟動時主動檢查一次（tools/launch.py）：伺服器有沒有回應、預設模型在不在；沒問題回傳 None。"""
    model = model or settings.OLLAMA_MODEL
    try:
        installed = _installed()
    except (OSError, ValueError, KeyError) as exc:
        return report_failure(exc, model)
    if model not in installed:
        return _remember(_model_missing(model, ["查詢已安裝的模型：有回應，但沒有這個模型"]), None, model)
    clear()
    return None


def diagnose(exc, model):
    """依呼叫失敗的錯誤判斷原因。回傳 {code, title, detail, steps, causes, checks}：查得出原因時有 steps，
    查不出時有 causes（畫面會再附故障報告）。"""
    where = narrator.server_label()
    host, port = _host_port()
    local = host in narrator.LOCAL_HOSTS
    reason = exc if isinstance(exc, urllib.error.HTTPError) else getattr(exc, "reason", exc)
    checks = [f"原本的錯誤：{type(exc).__name__}：{exc}"]

    if isinstance(exc, urllib.error.HTTPError):
        message = _error_body(exc)
        checks.append(f"伺服器回覆 HTTP {exc.code}：{message}")
        if exc.code == 404 and "not found" in message.lower():
            return _model_missing(model, checks)
        return _problem("server_error", f"{where}的 Ollama 回覆錯誤（HTTP {exc.code}）", checks, detail=message, causes=[
            f"伺服器的記憶體或顯示卡記憶體不夠，載入不了模型 {model}",
            "模型檔損壞或下載不完整",
            "Ollama 版本太舊，不支援這個模型",
            "Ollama 內部錯誤"])
    if isinstance(reason, (ValueError, KeyError)):
        return _not_ollama(checks)
    if isinstance(reason, socket.gaierror):
        return _problem("dns", f"找不到 {where}（伺服器名稱查不到）", checks, steps=[
            "確認這台電腦有連上網路（打開任何一個網站試試）",
            "確認位址裡的伺服器名稱沒有打錯：" + ADDRESS_SETTING,
            "伺服器名稱只在校內網路查得到時，要先連上校內網路或 VPN"])
    if isinstance(reason, ConnectionRefusedError):
        return _refused(model, checks, where, local, port)
    if isinstance(reason, OSError) and (reason.errno in UNREACHABLE_ERRNOS
                                        or getattr(reason, "winerror", None) in UNREACHABLE_ERRNOS):
        return _problem("network", f"這台電腦連不到 {where}所在的網路", checks, causes=[
            "這台電腦沒有連上網路",
            "伺服器在校內網路，這台電腦在校外，要先連 VPN",
            "網路設定或防火牆擋住了"])
    if isinstance(reason, TimeoutError):
        return _timed_out(model, checks, where, host, port, local)
    if isinstance(reason, (ConnectionResetError, ConnectionAbortedError)):
        return _problem("reset", f"和 {where}的連線被中斷", checks, causes=[
            "Ollama 在回答途中當掉或重新啟動",
            "伺服器記憶體不夠，載入模型時被系統結束",
            "網路不穩，或中間的防火牆、代理伺服器切斷了連線"])
    return _problem("unknown", f"呼叫 {where}失敗，原因不明", checks, causes=[
        "網路或伺服器暫時出問題，過幾分鐘會自動再試",
        "請把下面的故障報告交給專案負責人"])


def _refused(model, checks, where, local, port):
    port_note = f"位址設定的埠是 {port}，Ollama 預設是 {DEFAULT_PORT}：確認沒有寫錯（{ADDRESS_SETTING}）"
    if not local:
        return _problem("remote_refused", f"{where}有開機，但 Ollama 沒有在接收連線", checks,
                        detail=f"連到埠 {port} 被拒絕：伺服器在，但這個埠沒有程式在接收連線。", steps=[
                            "請伺服器管理者確認 Ollama 正在執行（在伺服器上執行「ollama ps」要有回應）",
                            "Ollama 要設定 OLLAMA_HOST=0.0.0.0 才接受其他電腦連線，改完要重新啟動 Ollama",
                            port_note])
    if OLLAMA_EXE.exists() or shutil.which("ollama"):
        steps = ["從開始選單打開 Ollama（右下角會出現 Ollama 圖示）",
                 "或在命令提示字元執行「ollama serve」",
                 "或改用「啟動本機測試.bat」，它會自動啟動這台電腦的 Ollama"]
        if port != DEFAULT_PORT:
            steps.append(port_note)
        return _problem("local_not_running", "這台電腦的 Ollama 沒有啟動", checks,
                        detail=f"埠 {port} 沒有程式在接收連線。", steps=steps)
    checks.append(f"找不到 Ollama 程式：{OLLAMA_EXE} 不存在，PATH 裡也沒有 ollama")
    return _problem("local_not_installed", "這台電腦沒有安裝 Ollama", checks, steps=[
        "平常使用研究用伺服器時不需要這台電腦的 Ollama：確認 AI 伺服器位址不是本機（" + ADDRESS_SETTING + "）",
        "要在這台電腦跑模型的話，到 https://ollama.com/download 下載安裝 Ollama",
        f"安裝後執行「ollama pull {model}」下載模型"])


def _timed_out(model, checks, where, host, port, local):
    """逾時：伺服器不在、Ollama 卡住、模型太慢都會逾時，要再查一次才分得出來。"""
    state, info = _connect(host, port)
    checks.append(f"連線到埠 {port}：{info}")
    if state == "refused":
        return _refused(model, checks, where, local, port)
    if state != "ok":
        causes = (["這台電腦的 Ollama 卡住了", "防毒軟體或防火牆擋住了本機連線"] if local else [
            "伺服器關機、休眠或正在重新開機",
            "這台電腦不在能連到伺服器的網路（在校外要先連 VPN）",
            f"防火牆擋住了埠 {port}",
            "位址設定錯了，連到不存在的電腦（" + ADDRESS_SETTING + "）"])
        return _problem("no_answer", f"連不到 {where}（沒有回應）", checks, causes=causes)
    try:
        installed = _installed()
    except (ValueError, KeyError) as exc:
        checks.append(f"查詢已安裝的模型：回應的格式不對（{exc}）")
        return _not_ollama(checks)
    except OSError as exc:
        checks.append(f"查詢已安裝的模型：沒有回應（{exc}）")
        restart = ("請伺服器管理者重新啟動伺服器上的 Ollama（研究用伺服器 2026-10-11、10-14 卡住時都是這樣恢復）"
                   if not local else "結束 Ollama（右下角圖示 › Quit Ollama）再重新打開")
        return _problem("hung", f"{where}的 Ollama 沒有回應（可能卡住了）", checks,
                        detail="連得到伺服器，但 Ollama 連最簡單的查詢都沒有回答。", steps=[
                            restart, f"重新啟動後約 {_retry_minutes()} 分鐘內網站會自動恢復，不用重開網站"])
    checks.append(f"查詢已安裝的模型：有回應，共 {len(installed)} 個")
    if model not in installed:
        return _model_missing(model, checks)
    return _problem("slow", f"{where}連得上，但模型 {model} 沒有在 {settings.OLLAMA_TIMEOUT} 秒內回答", checks, causes=[
        "模型第一次載入比較久（8 到 75 秒），再試一次通常就好",
        "伺服器正忙：別人也在用同一台伺服器",
        "伺服器的記憶體不夠，模型載入很慢或卡住",
        "模型卡住了：請伺服器管理者重新啟動 Ollama"])


def _model_missing(model, checks):
    where = narrator.server_label()
    local = _host_port()[0] in narrator.LOCAL_HOSTS
    pull = (f"在這台電腦執行「ollama pull {model}」下載模型" if local
            else f"請伺服器管理者在伺服器上執行「ollama pull {model}」")
    return _problem("model_missing", f"{where}上沒有模型 {model}", checks,
                    steps=[pull, "或到「設定 › 進階設定」換一個已經有的模型"])


def _not_ollama(checks):
    return _problem("not_ollama", "這個位址有回應，但不是 Ollama", checks,
                    detail="位址或埠號可能指到別的服務了。", steps=[ADDRESS_SETTING, f"Ollama 預設的埠是 {DEFAULT_PORT}"])


def _problem(code, title, checks, detail="", steps=None, causes=None):
    return {"code": code, "title": title, "detail": detail, "known": causes is None,
            "steps": steps or [], "causes": causes or [], "checks": checks}


def _remember(problem, exc, model):
    """記成目前的問題；同一個問題持續時沿用原本的（不重複存報告、不重複寫紀錄）。"""
    global _current
    problem = {k: _redact(v) for k, v in problem.items()}
    if _current and _current["title"] == problem["title"]:
        return _current
    problem["retry_minutes"] = _retry_minutes()
    problem["report"] = _report(problem, model)
    problem["report_file"] = _save(problem["report"])
    _current = problem
    log.warning("AI 伺服器檢測：%s%s", problem["title"],
                f"；故障報告存在 {problem['report_file']}" if problem["report_file"] else "")
    return problem


def _report(problem, model):
    host, port = _host_port()
    lines = ["益智遊戲 AI 教練：AI 伺服器故障報告",
             f"時間：{datetime.now():%Y-%m-%d %H:%M:%S}",
             f"電腦：{platform.node()}",
             f"AI 伺服器：{narrator.server_label()}（{urllib.parse.urlsplit(settings.OLLAMA_URL).scheme}，埠 {port}）",
             f"模型：{model}",
             "檢查結果：", *[f"- {c}" for c in problem["checks"]],
             f"判斷：{problem['title']}"]
    if problem["detail"]:
        lines.append(problem["detail"])
    if problem["known"]:
        lines += ["排除步驟：", *[f"{i}. {s}" for i, s in enumerate(problem["steps"], 1)]]
    else:
        lines += ["可能原因：", *[f"- {c}" for c in problem["causes"]]]
    lines.append(f"設定：逾時 {settings.OLLAMA_TIMEOUT} 秒、連不上後暫停 {settings.OLLAMA_RETRY_AFTER} 秒、"
                 f"num_ctx {settings.OLLAMA_NUM_CTX}")
    return _redact("\n".join(lines))


def _save(text):
    folder = settings.FAULT_REPORT_DIR
    if folder is None:  # 測試時不寫檔
        return ""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{datetime.now():%Y%m%d-%H%M%S}.txt"
    path.write_text(text, encoding="utf-8")
    return f"{folder.parent.name}/{folder.name}/{path.name}"


def as_text(problem):
    """給啟動畫面（tools/launch.py）印的說明。"""
    lines = [f"AI 伺服器有問題：{problem['title']}"]
    if problem["detail"]:
        lines.append(problem["detail"])
    if problem["known"]:
        lines += ["排除步驟：", *[f"  {i}. {s}" for i, s in enumerate(problem["steps"], 1)]]
    else:
        lines += ["可能原因：", *[f"  - {c}" for c in problem["causes"]]]
        if problem["report_file"]:
            lines.append(f"故障報告存在 {problem['report_file']}，請交給專案負責人。")
    lines.append("沒有 AI 時電腦照常下棋，說明改顯示分析原文；修好後網站會自動恢復。")
    return "\n".join(lines)


def _host_port():
    url = urllib.parse.urlsplit(settings.OLLAMA_URL)
    return url.hostname or "", url.port or DEFAULT_PORT


def _retry_minutes():
    return math.ceil(settings.OLLAMA_RETRY_AFTER / 60)


def _redact(value):
    """研究用伺服器的位址換成代稱（錯誤訊息裡可能帶著位址）。"""
    if isinstance(value, list):
        return [_redact(v) for v in value]
    if not isinstance(value, str):
        return value
    host = _host_port()[0]
    if host and host not in narrator.LOCAL_HOSTS:
        value = value.replace(settings.OLLAMA_URL, narrator.server_label()).replace(host, narrator.server_label())
    return value


def _error_body(exc):
    try:
        body = exc.read().decode("utf-8", "replace")
    except Exception:  # 沒有內文（例如測試建的 HTTPError）
        return str(exc.reason)
    try:
        return json.loads(body).get("error") or body
    except (ValueError, AttributeError):
        return body or str(exc.reason)


def _connect(host, port):
    """只連 TCP，不送請求：分出「伺服器不在」和「伺服器在、Ollama 不回答」。"""
    try:
        with socket.create_connection((host, port), timeout=3):
            return "ok", "連得上"
    except ConnectionRefusedError:
        return "refused", "被拒絕"
    except OSError as exc:
        return "failed", f"連不上（{exc or type(exc).__name__}）"


def _installed():
    with urllib.request.urlopen(f"{settings.OLLAMA_URL}/api/tags", timeout=5) as resp:
        return {m["name"] for m in json.load(resp)["models"]}
