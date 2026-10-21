"""Stop hook：每次 Claude 回應結束時，把時間、使用者的要求與 Claude 最後的回覆記到 工作紀錄.md。

同一個要求可能因為背景工作的通知而結束好幾次回應；這時更新同一筆紀錄，不重複新增。
紀錄有對話全文，不能放進公開的儲存庫（2026-10-21），所以寫在私人儲存庫 private/ 裡，由 版本管理.bat 和受邀成員同步；
沒有 private/ 的電腦寫在專案根目錄（.gitignore 已排除）。git worktree 裡沒有 private/，所以到主要的專案資料夾找。
"""
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

LOG_NAME = "工作紀錄.md"
HEADER = "# 工作紀錄\n\n每次 Claude 回應結束時由 `.claude/hooks/log_progress.py` 自動記錄。\n\n"
REQUEST_MAX = 200  # 貼上的長文字（例如伺服器紀錄）只留開頭


def text_of(content):
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content or [] if b.get("type") == "text")


def is_request(entry):
    """使用者親自送出的要求；排除工具結果、技能說明等系統插入的內容與背景工作通知。"""
    if entry.get("type") != "user" or entry.get("isMeta"):
        return False
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, list) and any(b.get("type") == "tool_result" for b in content):
        return False
    text = text_of(content).strip()
    return bool(text) and "<task-notification>" not in text


def log_path(project):
    common = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=project,
                            capture_output=True, text=True).stdout.strip()
    main_dir = Path(common).parent if common else project
    private = main_dir / "private"
    return (private if private.is_dir() else project) / LOG_NAME


def main():
    data = json.load(sys.stdin)
    with open(data["transcript_path"], encoding="utf-8") as f:
        entries = [json.loads(line) for line in f if line.strip()]
    start = max((i for i, e in enumerate(entries) if is_request(e)), default=None)
    if start is None:
        return
    request = entries[start]
    reply = ""
    for entry in entries[start + 1:]:
        if entry.get("type") == "assistant":
            reply = text_of((entry.get("message") or {}).get("content")).strip() or reply
    if not reply:
        return

    started = datetime.fromisoformat(request["timestamp"].replace("Z", "+00:00")).astimezone()
    ended = datetime.now().astimezone()
    minutes = max(1, round((ended - started).total_seconds() / 60))
    end_text = ended.strftime("%H:%M") if ended.date() == started.date() else ended.strftime("%Y-%m-%d %H:%M")
    asked = " ".join(text_of(request["message"]["content"]).split())
    if len(asked) > REQUEST_MAX:
        asked = asked[:REQUEST_MAX] + "…"
    # 回覆裡的標題降兩級，才不會和每筆紀錄的標題混在一起
    body = "\n".join("##" + line if line.startswith("#") else line for line in reply.splitlines())

    mark = f"<!-- request: {request['timestamp']} -->"
    entry = (f"{mark}\n## {started.strftime('%Y-%m-%d %H:%M')}–{end_text}（約 {minutes} 分鐘）\n\n"
             f"**要求：** {asked}\n\n**進度：**\n\n{body}\n\n---\n\n")

    log = log_path(Path(os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or "."))
    old =log.read_text(encoding="utf-8") if log.exists() else HEADER
    if mark in old:  # 同一個要求已經記過（一定是最後一筆），換成最新的結束時間與回覆
        old = old[:old.index(mark)]
    log.write_text(old + entry, encoding="utf-8")


if __name__ == "__main__":
    main()
