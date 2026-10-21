"""Stop hook：Claude 的工作結束時，如果留下還沒存檔的改動，請 Claude 問成員要不要存檔或送審（2026-10-21 使用者要求）。

- 同樣的改動只問一次：改動的指紋存在這個資料夾的 git 資料裡（claude_ask_save.json，不進任何儲存庫）。
  成員選「先不要」之後，除非又有新的改動，不會再問；選「這次對話不要再問」就記下這次對話的編號（--mute）。
- stop_hook_active（Claude 已經因為這個 hook 在繼續）時不再擋，免得一直迴圈。
- 程式（公開儲存庫）與 .md 文件（私人儲存庫 private/.git）的改動都算；工作紀錄不算，它每次回應完都會變。
- 只決定「要不要問」，問與存檔由 Claude 照 .claude/skills/save-work/SKILL.md 做（CLAUDE.md Rule 5：判斷交給程式）。
"""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

STATE = "claude_ask_save.json"
WORK_LOG = "private/工作紀錄.md"
LIST_MAX = 15  # 改動的檔案很多時只列前面幾個


def git(project, *args):
    result = subprocess.run(["git", "-c", "core.quotepath=false", *args], cwd=project, capture_output=True)
    return result.stdout.decode("utf-8", "replace") if result.returncode == 0 else None


def pending(project):
    """(git 資料夾, 分支, 程式改動, 文件改動, 指紋)；不是 git 專案時回傳 None。"""
    git_dir = git(project, "rev-parse", "--absolute-git-dir")
    if git_dir is None:
        return None
    code = git(project, "status", "--porcelain").splitlines()
    diff = git(project, "diff", "HEAD") or ""
    docs = []
    private = project / "private" / ".git"
    if private.exists():
        p = (f"--git-dir={private}", f"--work-tree={project}")
        docs = [line for line in git(project, *p, "status", "--porcelain").splitlines() if not line.endswith(WORK_LOG)]
        diff += git(project, *p, "diff", "HEAD", "--", ".", f":(exclude){WORK_LOG}") or ""
    fingerprint = hashlib.sha256("\n".join(code + docs + [diff]).encode("utf-8")).hexdigest()
    return git_dir.strip(), (git(project, "branch", "--show-current") or "").strip(), code, docs, fingerprint


def listing(lines):
    shown = "\n".join(f"  {line}" for line in lines[:LIST_MAX])
    return shown + (f"\n  ……還有 {len(lines) - LIST_MAX} 個" if len(lines) > LIST_MAX else "")


def decide(data, project):
    """要請 Claude 問成員時回傳 Stop hook 的回應（decision: block），否則回傳 None。"""
    if data.get("stop_hook_active"):
        return None
    found = pending(project)
    if not found:
        return None
    git_dir, branch, code, docs, fingerprint = found
    if not code and not docs:
        return None
    state_file = Path(git_dir) / STATE
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    session = data.get("session_id", "")
    if state.get("fingerprint") == fingerprint or session in state.get("muted", []):
        return None
    state["fingerprint"] = fingerprint
    state_file.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    # 只有文件改動時直接存進私人儲存庫、不開工作分支（git_menu.save_documents），提示要和實際做法一致
    on_main = "（主線，存檔時會先開工作分支）" if branch == "main" and code else ""
    parts = [f"工作結束前：專案還有沒存檔的改動（目前分支 {branch}{on_main}）。"]
    if code:
        parts.append(f"程式（公開儲存庫，要經過送審）：\n{listing(code)}")
    if docs:
        parts.append(f"文件（私人儲存庫，不用送審）：\n{listing(docs)}")
    options = ["「存檔」：選項說明寫上你依這次改動擬好的存檔訊息（一句話）"]
    if code:
        options.append("「存檔並送審」：存檔後上傳給負責人審核（會公開在 GitHub，送審前會跑完整測試）")
    options.append("「先不要」：同樣的改動不會再問")
    if session:  # 沒有對話編號就記不住是哪一次對話，不提供這個選項
        options.append(f"「這次對話不要再問」：選了就執行 `.venv/Scripts/python.exe .claude/hooks/ask_save.py --mute {session}`")
    parts.append("請用 AskUserQuestion 問成員要怎麼處理，選項：\n" + "\n".join(f"- {o}" for o in options))
    parts.append("照成員的選擇用 save-work 技能（.claude/skills/save-work/SKILL.md）的步驟做。"
                 "這些改動如果不是這次工作造成的（例如別的工作階段改的），問的時候要說明。")
    return {"decision": "block", "reason": "\n\n".join(parts)}


def mute(project, session):
    """「這次對話不要再問」：記下對話編號。"""
    git_dir = git(project, "rev-parse", "--absolute-git-dir")
    if git_dir is None:
        return
    state_file = Path(git_dir.strip()) / STATE
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    state.setdefault("muted", []).append(session)
    state_file.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    print("這次對話不會再問要不要存檔。")


def main():
    project = Path(os.environ.get("CLAUDE_PROJECT_DIR") or ".")
    if sys.argv[1:2] == ["--mute"]:
        # 少了編號時清楚說明，不要掉到下面去讀 stdin（會印出看不懂的 JSON 錯誤；2026-10-21 審核 #1 時發現）
        if len(sys.argv) != 3 or not sys.argv[2].strip():
            sys.stderr.reconfigure(encoding="utf-8")
            print("用法：ask_save.py --mute <對話編號>。編號在存檔提醒的「這次對話不要再問」那一行；"
                  "沒有編號就無法記住是哪一次對話，這次沒有設定。", file=sys.stderr)
            sys.exit(2)
        mute(project, sys.argv[2])
        return
    data = json.loads(sys.stdin.buffer.read().decode("utf-8"))  # Windows 管線預設是 cp950，Claude Code 送的是 UTF-8
    response = decide(data, Path(os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or "."))
    if response:
        sys.stdout.reconfigure(encoding="utf-8")
        print(json.dumps(response, ensure_ascii=False))


if __name__ == "__main__":
    main()
