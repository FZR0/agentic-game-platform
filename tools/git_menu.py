"""版本管理選單（由 版本管理.bat 呼叫；2026-10-21 使用者要求「把 git 流程簡化」）。

專案放在兩個 GitHub 儲存庫（docs/交接.md 4.8）：
- 公開的 <擁有者>/<名稱>：程式。主線 main 只能合併負責人核准過的送審（Pull Request）。
- 私人的 <擁有者>/<名稱>-private：所有 .md（CLAUDE.md、docs、技能說明）、工作紀錄與研究用伺服器的位址，
  只有受邀成員拿得到（2026-10-21 使用者決定 .md 只給成員看）。git 資料在 private/.git，但工作範圍是整個
  專案資料夾（core.worktree = ..），所以 .md 留在原本的位置，Claude Code 照樣讀得到；文件的改動不經過送審。
選單只是把常用的 git／gh 指令包起來：每個指令執行前都印出來，出錯時照樣顯示 git 的訊息，不自己猜原因。

--check-secrets 給 tools/githooks/pre-commit 用：存檔前擋下寫進公開檔案的機密與 .md。
--status、--save、--submit、--update 不問問題，給 Claude Code 用（工作結束時由它先問成員；.claude/skills/save-work）。
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from config import local  # noqa: E402

PRIVATE_DIR = local.MAIN_DIR / "private"
MAIN = "main"
GIT = ("git", "-c", "core.quotepath=false")  # 中文檔名照原樣顯示，不變成 \346\270... 這種跳脫碼
PRIVATE_GIT = (*GIT, f"--git-dir={PRIVATE_DIR / '.git'}", f"--work-tree={local.MAIN_DIR}")
# 私人儲存庫只管 .md 與 private/，其餘檔案歸公開的儲存庫；寫進 private/.git/info/exclude（不在任何儲存庫裡，每次使用時重寫）
PRIVATE_EXCLUDE = """# tools/git_menu.py 產生，不要手動改：私人儲存庫只管 .md 與 private/（docs/交接.md 4.8）
*
!*/
!*.md
!/private/**
/.venv/
/tools/katago/
/tools/pikafish/
/tools/stockfish/
/.claude/worktrees/
/obj/
/.vs/
"""
# 公開的儲存庫不管 .md 與 private/；寫進它的 .git/info/exclude，不能寫在 .gitignore：
# 根目錄的 .gitignore 兩個儲存庫都會讀，而且比 info/exclude 優先，寫在那裡私人儲存庫就看不到這些檔案了
PUBLIC_EXCLUDE = """# tools/git_menu.py 產生，不要手動改：文件（.md）與 private/ 歸私人儲存庫（docs/交接.md 4.8）
*.md
/private/
"""
WORK_LOG = "private/工作紀錄.md"
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
DECISIONS = {"APPROVED": "已核准", "CHANGES_REQUESTED": "已退回修改"}


def run(*args, cwd=ROOT):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")


def out(*args, cwd=ROOT):
    """執行並回傳輸出；失敗時回傳空字串。"""
    result = run(*args, cwd=cwd)
    return result.stdout.strip() if result.returncode == 0 else ""


def ok(*args, cwd=ROOT):
    return run(*args, cwd=cwd).returncode == 0


def show(*args, cwd=ROOT):
    """印出指令再執行，git／gh 的訊息直接顯示在視窗裡；回傳是否成功。"""
    if args[:len(PRIVATE_GIT)] == PRIVATE_GIT:
        shown = ("git（私人）",) + args[len(PRIVATE_GIT):]
    elif args[:len(GIT)] == GIT:
        shown = ("git",) + args[len(GIT):]
    else:
        shown = args
    print(">", " ".join(shown))
    return subprocess.run(args, cwd=cwd).returncode == 0


def ask(prompt):
    return input(prompt).strip()


def yes(prompt):
    return ask(f"{prompt}（Y/N）").lower() in ("y", "yes")


def repo_of(folder):
    """origin 的「擁有者/名稱」，例如 FZR0/agentic-game-platform；不是從 GitHub 下載的資料夾回傳 None。"""
    match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", out("git", "remote", "get-url", "origin", cwd=folder))
    return match[1] if match else None


def private_repo():
    public = repo_of(ROOT)
    return f"{public}-private" if public else None


def current_branch():
    return out(*GIT, "branch", "--show-current")


def changes():
    return out(*GIT, "status", "--porcelain").splitlines()


def private_changes(include_log=False):
    """還沒存進私人儲存庫的文件改動（沒有私人儲存庫時是空的）。工作紀錄每次 Claude 回應完都會變，
    平常不算進「改動」，否則每次都會被當成有東西要存；存檔時才一起存（include_log）。"""
    if not (PRIVATE_DIR / ".git").exists():
        return []
    lines = out(*PRIVATE_GIT, "status", "--porcelain").splitlines()
    return lines if include_log else [line for line in lines if not line.endswith(WORK_LOG)]


def merging():
    return ok(*GIT, "rev-parse", "-q", "--verify", "MERGE_HEAD")


def unresolved(git, base):
    """合併衝突還沒處理完的檔案（還留著 <<<<<<< 標記）。處理完但還沒 git add 的不算，存檔時會一起存。"""
    names = out(*git, "diff", "--name-only", "--diff-filter=U").splitlines()
    return [n for n in names
            if (base / n).exists() and "<<<<<<< " in (base / n).read_text(encoding="utf-8", errors="replace")]


def test_apps():
    """有 tests.py 的 app（新遊戲加進來就會自動包含）。"""
    return sorted(p.parent.name for p in ROOT.glob("*/tests.py"))


# ---- 私人儲存庫 ----

def write_exclude(git_dir, text):
    info = Path(git_dir) / "info"
    info.mkdir(exist_ok=True)
    (info / "exclude").write_text(text, encoding="utf-8")


def write_excludes():
    """兩個儲存庫各管哪些檔案。每次使用時重寫，規則改了舊電腦也會跟著更新。"""
    write_exclude(PRIVATE_DIR / ".git", PRIVATE_EXCLUDE)
    public = out(*GIT, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if public:
        write_exclude(public, PUBLIC_EXCLUDE)


def fetch_private():
    """受邀成員：下載私人儲存庫（文件、工作紀錄、伺服器位址）。已經有、沒有 gh、沒登入或沒有權限時什麼都不做
    （不會跳出登入視窗）。tools/launch.py 每次啟動也會呼叫，所以成員受邀後第一次啟動就會拿到文件與伺服器位址。"""
    if (PRIVATE_DIR / ".git").exists():
        write_excludes()
        return True
    if not shutil.which("git") or not shutil.which("gh"):
        return False
    repo = private_repo()
    if not repo or not ok("gh", "api", f"repos/{repo}", "--silent"):
        return False
    print(f"下載私人資料（{repo}）：文件、工作紀錄、伺服器位址...")
    # 先只下載 git 資料，把工作範圍設成整個專案資料夾後再取出檔案，.md 才會放在原本的位置
    if not show("gh", "repo", "clone", repo, str(PRIVATE_DIR), "--", "--no-checkout"):
        return False
    run("git", "config", "core.worktree", "..", cwd=PRIVATE_DIR)
    write_excludes()
    return show(*PRIVATE_GIT, "checkout", "-f", MAIN)


def save_private(message=None):
    """把文件與工作紀錄的改動存進私人儲存庫（只在這台電腦，同步時才上傳）。"""
    if private_changes(include_log=True):
        run(*PRIVATE_GIT, "add", "-A")
        return ok(*PRIVATE_GIT, "commit", "-m", message or f"文件與工作紀錄（{os.environ.get('COMPUTERNAME', '')}）")
    return False


def sync_private():
    """先存下這台電腦的文件與工作紀錄，再和其他成員的合併後上傳。文件不經過送審（.md 只給成員看，
    放在沒有保護的私人儲存庫）；工作紀錄只會往後接，多人同時記錄時由 git 自動把兩邊的新內容都留下
    （private/.gitattributes：merge=union）。"""
    if not fetch_private():
        return False
    print("同步私人資料（文件、工作紀錄、伺服器位址）...")
    if private_conflicts():
        return False
    save_private()
    if show(*PRIVATE_GIT, "pull", "--no-rebase", "--no-edit"):
        return show(*PRIVATE_GIT, "push")
    private_conflicts()
    return False


def save_documents(message=None):
    """只有文件改動時的存檔：存進私人儲存庫後直接上傳。文件不經過送審，不上傳的話其他成員一直看不到，
    而 Claude Code 的存檔提醒只看「還沒存檔」的改動，存了沒上傳的不會再提醒（2026-10-21 審核 #1 時發現）。"""
    if private_conflicts():
        return False
    if not save_private(message):
        print("沒有需要存檔的改動。")
        return True
    if sync_private():
        print("文件已存進私人儲存庫並上傳，其他成員「取得最新版本」就看得到。")
        return True
    print("文件已存在這台電腦，但上傳失敗（原因在上面），下次「取得最新版本」時會再上傳。")
    return False


def private_conflicts():
    """私人儲存庫的合併衝突還沒處理完時列出檔案；處理完之前不存檔，免得把衝突標記存進去。"""
    files = unresolved(PRIVATE_GIT, local.MAIN_DIR)
    if files:
        print("文件和其他成員的改動在同一個地方衝突，這些檔案要決定留哪一邊：")
        print("\n".join(f"  {f}" for f in files))
        print("可以請 Claude Code 幫忙（對它說「幫我解決私人儲存庫的合併衝突」），處理完再選「取得最新版本」。")
    return files


# ---- 第一次使用 ----

def setup():
    """檢查 git 與 gh、登入 GitHub、設定這個專案的作者資料與存檔前的機密檢查、下載私人資料。"""
    for tool, winget_id in (("git", "Git.Git"), ("gh", "GitHub.cli")):
        if not shutil.which(tool):
            print(f"這台電腦還沒有安裝 {tool}。")
            if yes(f"用 winget 安裝？（winget install -e --id {winget_id}）") and show("winget", "install", "-e", "--id", winget_id):
                print("安裝好了。請關掉這個視窗，再開一次 版本管理.bat。")
            return False
    if not repo_of(ROOT):
        print("這個資料夾不是從 GitHub 下載的專案。請用 gh repo clone <擁有者>/<名稱> 下載，再從那個資料夾開 版本管理.bat。")
        return False
    if not ok("gh", "auth", "status"):
        print("請先登入 GitHub（會打開瀏覽器）。")
        if not show("gh", "auth", "login", "--web", "--git-protocol", "https"):
            return False
    run("gh", "auth", "setup-git")  # git 上傳時用 gh 的登入，不再另外問帳號密碼
    run(*GIT, "config", "core.hooksPath", "tools/githooks")  # 存檔前的機密檢查（用 VS、Claude Code 存檔也會檢查）
    fetch_private()
    # 公開的儲存庫會顯示每筆存檔的作者信箱，所以這個專案改用 GitHub 的隱藏信箱（使用者 2026-10-21 決定）
    user = json.loads(out("gh", "api", "user") or "{}")
    for folder in (ROOT, PRIVATE_DIR):
        if user and (folder / ".git").exists() and not out("git", "config", "--local", "user.email", cwd=folder):
            run("git", "config", "user.name", user["login"], cwd=folder)
            run("git", "config", "user.email", f"{user['id']}+{user['login']}@users.noreply.github.com", cwd=folder)
    return True


# ---- 選單的五個動作 ----

def update(interactive=True):
    """取得最新版本：在主線就直接更新；在工作分支就把主線的新內容合併進來。私人資料一起同步。回傳是否成功。"""
    sync_private()
    if not show(*GIT, "fetch", "origin"):
        return False
    before = out(*GIT, "rev-parse", "HEAD")
    if current_branch() == MAIN:
        done = show(*GIT, "merge", "--ff-only", f"origin/{MAIN}")
    else:
        done = show(*GIT, "merge", "--no-edit", f"origin/{MAIN}")
    if not done:
        if merging():
            conflict(interactive)
        else:
            print("沒有更新（原因在上面）。常見原因：你改過的檔案別人也改了，先「存檔」再取得最新版本。")
        return False
    if "requirements.txt" in out(*GIT, "diff", "--name-only", before, "HEAD").splitlines():
        print("套件清單有更新，正在安裝...")
        show(sys.executable, "-m", "pip", "install", "-r", "requirements.txt")
    print("已經是最新版本。網站伺服器正在執行的話，關掉它的視窗再用 啟動遊戲平台.bat 重開（會一併更新資料庫）。")
    return True


def conflict(interactive=True):
    print("主線的新內容和你的改動在同一個地方衝突，這些檔案要決定留哪一邊：")
    print("\n".join(f"  {f}" for f in out(*GIT, "diff", "--name-only", "--diff-filter=U").splitlines()))
    print("可以請 Claude Code 幫忙（對它說「幫我解決合併衝突」），處理完再選「存檔」。")
    if interactive and yes("先放棄這次合併，回到合併前的樣子？"):
        show(*GIT, "merge", "--abort")


def create_work_branch(name):
    """從最新的主線開 work/<名稱>（名稱只留英文、數字、-；空的就用日期）；沒存檔的改動會一起帶過去。"""
    name = re.sub(r"[^A-Za-z0-9-]+", "-", name or "").strip("-").lower() or datetime.now().strftime("%Y%m%d-%H%M")
    if not show(*GIT, "fetch", "origin"):
        return None
    # --no-track：上傳時才不會被設成推到 main
    if not show(*GIT, "switch", "--no-track", "-c", f"work/{name}", f"origin/{MAIN}"):
        return None
    print(f"已開始新工作 work/{name}（從最新的主線開始）。")
    return f"work/{name}"


def start_work():
    """從最新的主線開一個工作分支；還沒存檔的改動會一起帶過去。"""
    if current_branch() != MAIN and changes():
        print("目前這項工作還有沒存檔的改動，會一起帶到新工作。")
        if not yes("確定要帶過去？（不要的話選 N，先「存檔」）"):
            return
    if create_work_branch(ask("這項工作的簡短名稱（英文、數字或 -，例如 xiangqi-flip；直接按 Enter 用日期）：")):
        print("改到一個段落就選「存檔」，做完選「送審」。")


def commit_all(message):
    """程式改動存進目前的分支、文件改動存進私人儲存庫（都只在這台電腦）。回傳是否成功。"""
    if not (show(*GIT, "add", "-A") and show(*GIT, "commit", "-m", message)):
        print("沒有存檔，原因在上面。")
        return False
    if not private_conflicts() and save_private(message):
        print("文件的改動也存進私人儲存庫了。")
    print("已存檔（還在這台電腦）。做完要給負責人審核時「送審」。")
    return True


def commit():
    """存檔（commit）：程式只存在這台電腦，送審時才上傳。文件（.md）存進私人儲存庫，不用開工作分支；
    只有文件改動時存完直接上傳（save_documents）。"""
    if merging() and unresolved(GIT, ROOT):
        conflict()
        return
    if not changes():
        save_documents()
        return
    if current_branch() == MAIN:
        print("主線只放審核過的版本，不能直接存檔。要先開始一項新工作（現在的改動會一起帶過去）。")
        if not yes("現在開始新工作？"):
            return
        start_work()
        if current_branch() == MAIN:
            return
    show(*GIT, "status", "--short")
    message = ask("這次改了什麼（一句話，會顯示在紀錄裡；直接按 Enter 取消）：")
    if message:
        commit_all(message)


NO_TESTS = "送審前沒有跑測試"


def submit():
    """送審：上傳這項工作並開一個 Pull Request，負責人會收到通知。已經送審過就更新同一個。"""
    if current_branch() == MAIN:
        print("主線沒有要送審的工作。先「開始新工作」，改完存檔再送審。")
        return
    if changes():
        print("還有沒存檔的改動。")
        if yes("先存檔？"):
            commit()
        if changes():
            print("還有沒存檔的改動，先處理再送審。")
            return
    commits = work_commits()
    if not commits:
        return
    if not handover_updated():
        print("提醒：這項工作沒有更新 docs/交接.md（CLAUDE.md 規定每個里程碑都要更新，下一個人才接得下去）。")
        if not yes("仍然要送審？"):
            return
    tests = NO_TESTS
    if yes("送審前先跑一次完整測試？（約 2 分鐘，建議）"):
        tests = run_tests()
        if not tests.startswith("通過") and not yes("測試沒有全部通過，仍然要送審？（審核的人會看到結果）"):
            return
    first = commits.splitlines()[0][2:]
    open_review(commits, tests, lambda: ask(f"送審的標題（直接按 Enter 用「{first}」）：") or first, "版本管理.bat")


def work_commits():
    """送審前的共同步驟：取得最新的主線、上傳私人資料（文件不在送審裡，先上傳審核的人才看得到），
    回傳這項工作的存檔清單（每行「- 說明」）；沒有存檔時回傳空字串。"""
    if not show(*GIT, "fetch", "origin"):
        return ""
    commits = out(*GIT, "log", "--reverse", "--format=- %s", f"origin/{MAIN}..HEAD")
    if not commits:
        print("這項工作還沒有任何存檔。")
        return ""
    sync_private()
    return commits


def open_review(commits, tests, title, via):
    """上傳工作分支並開送審；已經有開著的送審就只更新。title 是函式：真的要開新送審時才問標題。回傳是否成功。"""
    if not show(*GIT, "push", "-u", "origin", "HEAD"):
        return False
    existing = out("gh", "pr", "view", "--json", "url,state", "--jq", 'select(.state == "OPEN") | .url')
    if existing:
        if tests != NO_TESTS:
            run("gh", "pr", "comment", "--body", f"更新送審。測試：{tests}")
        print(f"已更新送審：{existing}")
        return True
    files = out(*GIT, "diff", "--name-only", f"origin/{MAIN}...HEAD").splitlines()
    body = (f"## 這次的改動\n\n{commits}\n\n改到 {len(files)} 個檔案。\n\n"
            f"## 測試\n\n{tests}\n\n（由 {via} 送出）")
    if not show("gh", "pr", "create", "--base", MAIN, "--head", current_branch(), "--title", title(), "--body", body):
        return False
    print("已送審，負責人會收到通知。審核完成前可以繼續改、存檔，再送審更新同一個送審。")
    return True


def handover_updated():
    """這項工作的第一筆存檔之後，私人儲存庫裡的 docs/交接.md 有沒有更新（文件不在送審裡，所以看私人儲存庫）。"""
    started = out(*GIT, "log", "--reverse", "--format=%ct", f"origin/{MAIN}..HEAD").split()
    updated = out(*PRIVATE_GIT, "log", "-1", "--format=%ct", "--", "docs/交接.md") if (PRIVATE_DIR / ".git").exists() else ""
    return bool(started and updated) and int(updated) >= int(started[0])


def run_tests():
    """跑完整測試，回傳一行結果（寫進送審說明）。"""
    print("測試中，約 2 分鐘，請稍候...")
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    result = subprocess.run([sys.executable, "manage.py", "test", *test_apps()], cwd=ROOT, env=env,
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    report = (result.stdout + result.stderr).strip()
    print("\n".join(report.splitlines()[-15:]))
    ran = re.search(r"^Ran \d+ tests? in [\d.]+s", report, re.M)
    verdict = re.search(r"^(OK|FAILED).*$", report, re.M)
    summary = f"{ran[0] if ran else '沒有執行完'}，{verdict[0] if verdict else '沒有結果'}"
    return ("通過：" if result.returncode == 0 else "沒有通過：") + summary


def review():
    """審核別人的送審：看改動、請 Claude Code 審核、核准並合併，或退回修改。"""
    prs = json.loads(out("gh", "pr", "list", "--json", "number,title,author,reviewDecision") or "[]")
    if not prs:
        print("目前沒有等待審核的送審。")
        return
    for pr in prs:
        print(f"  #{pr['number']} {pr['title']}（{pr['author']['login']}，{DECISIONS.get(pr['reviewDecision'], '等待審核')}）")
    number = ask("要審核哪一個？輸入編號（直接按 Enter 返回）：").lstrip("#")
    if not number:
        return
    show("gh", "pr", "view", number)
    # 不用 gh pr diff --name-only：中文檔名（例如 docs/交接.md）會變成 \344\272\244... 這種跳脫碼
    show("gh", "pr", "view", number, "--json", "files", "--jq", ".files[].path")
    while True:
        print("\n1. 在瀏覽器看完整改動\n2. 請 Claude Code 審核\n3. 核准並合併進主線\n4. 退回修改\n0. 返回")
        choice = ask("請選擇：")
        if choice == "1":
            show("gh", "pr", "view", number, "--web")
        elif choice == "2":
            print(f"在 Claude Code 打開這個專案，輸入：/review-pr {number}\n"
                  "Claude 會在另一個資料夾取出這個送審、跑測試、依 CLAUDE.md 檢查，再請你決定核准或退回。")
        elif choice == "3":
            merge(number)
            return
        elif choice == "4":
            reason = ask("要請對方修改什麼：")
            if reason:
                show("gh", "pr", "review", number, "--request-changes", "--body", reason)
            return
        elif choice == "0":
            return


def merge(number):
    if not yes(f"確定要把 #{number} 合併進主線？"):
        return
    me = out("gh", "api", "user", "--jq", ".login")
    author = out("gh", "pr", "view", number, "--json", "author", "--jq", ".author.login")
    if author == me:
        # GitHub 不讓作者核准自己的送審；儲存庫管理者可以略過核准直接合併（主線保護沒有限制管理者）
        print("這是你自己的送審，GitHub 不讓作者自己核准，改用管理者身分直接合併。")
        merged = show("gh", "pr", "merge", number, "--merge", "--delete-branch", "--admin")
    else:
        merged = show("gh", "pr", "review", number, "--approve") and \
            show("gh", "pr", "merge", number, "--merge", "--delete-branch")
    if merged and current_branch() == MAIN:
        update()


def invite():
    """邀請成員：一次加入公開與私人兩個儲存庫（可以上傳工作分支，但主線仍要經過審核）。"""
    name = ask("要邀請的成員的 GitHub 帳號（直接按 Enter 返回）：").lstrip("@")
    if not name:
        return
    for repo in (repo_of(ROOT), private_repo()):
        show("gh", "api", "-X", "PUT", f"repos/{repo}/collaborators/{name}", "-f", "permission=push", "--silent")
    print("已寄出邀請。對方在 GitHub 的通知或信箱按「接受」後，照 README.md 下載專案。")


ROLES = {"admin": "管理者", "maintain": "維護者", "write": "可上傳", "triage": "可分類", "read": "只能看"}


def members():
    """成員列表（2026-10-21 使用者要求）：兩個儲存庫各有誰、誰還沒接受邀請。"""
    joined = {}
    for label, repo in (("公開", repo_of(ROOT)), ("私人", private_repo())):
        people = run("gh", "api", f"repos/{repo}/collaborators?per_page=100")
        pending = run("gh", "api", f"repos/{repo}/invitations?per_page=100")
        print(f"\n{label}儲存庫 {repo}：")
        if people.returncode or pending.returncode:
            print("  讀不到（需要管理者權限，或網路不通）")
            continue
        for person in json.loads(people.stdout):
            print(f"  {person['login']}（{ROLES.get(person['role_name'], person['role_name'])}）")
            joined.setdefault(person["login"], set()).add(label)
        for invitation in json.loads(pending.stdout):
            login = (invitation.get("invitee") or {}).get("login", "?")
            print(f"  {login}（邀請中，還沒接受）")
            joined.setdefault(login, set()).add(label)
    missing = incomplete(joined)
    if missing:
        print("\n只加入其中一個儲存庫：" + "、".join(missing) + "。用「邀請成員」再邀請一次就會補上。")


def incomplete(joined):
    """只在一個儲存庫的成員：只在公開的拿不到文件與伺服器位址，只在私人的不能上傳程式。"""
    return [f"{login}（只在{''.join(labels)}儲存庫）" for login, labels in sorted(joined.items()) if len(labels) == 1]


# ---- 給 Claude Code 用的指令：不問問題，由 Claude Code 先問成員（2026-10-21 使用者要求；.claude/skills/save-work）----

def status_cli():
    """目前的分支、改動、存檔與送審，讓 Claude Code 決定要問成員什麼。"""
    branch = current_branch()
    code, docs = changes(), private_changes()
    print(f"分支：{branch}" + ("（主線：存檔時會先開工作分支）" if branch == MAIN else ""))
    print(f"程式改動（公開儲存庫，要送審）：{len(code)} 個" + "".join(f"\n  {line}" for line in code))
    print(f"文件改動（私人儲存庫，不用送審）：{len(docs)} 個" + "".join(f"\n  {line}" for line in docs))
    if branch != MAIN:
        commits = out(*GIT, "log", "--reverse", "--format=- %s", f"origin/{MAIN}..HEAD")
        print("這項工作的存檔：" + (f"\n{commits}" if commits else "還沒有"))
        pr = out("gh", "pr", "view", "--json", "url,state", "--jq", 'select(.state == "OPEN") | .url')
        print(f"送審：{pr or '還沒送審'}")
        print("docs/交接.md：這項工作開始後" + ("有更新" if handover_updated() else "還沒更新"))
    return 0


def ready_to_commit():
    """公開的儲存庫會顯示作者信箱，存檔前也要做機密檢查；這台還沒設定好（沒執行過 版本管理.bat）就不存檔。"""
    if not out(*GIT, "config", "--local", "user.email") or out(*GIT, "config", "core.hooksPath") != "tools/githooks":
        print("這台電腦還沒設定好版本管理（GitHub 隱藏信箱、存檔前的機密檢查），請先執行一次 版本管理.bat。")
        return False
    return True


def save_cli(message, name):
    """存檔：在主線上有程式改動時先開 work/<name>（主線只放審核過的版本）。"""
    if not ready_to_commit() or private_conflicts():
        return 1
    if merging() and unresolved(GIT, ROOT):
        conflict(interactive=False)
        return 1
    if not changes():
        return 0 if save_documents(message) else 1
    if current_branch() == MAIN and not create_work_branch(name):
        return 1
    return 0 if commit_all(message) else 1


def submit_cli(title, with_tests):
    """送審：要先存檔；測試沒有全部通過就不送（硬要送請成員用 版本管理.bat）。"""
    if current_branch() == MAIN:
        print("主線沒有要送審的工作：先用 --save 存檔（會開工作分支）。")
        return 1
    if changes():
        print("還有沒存檔的改動，先用 --save 存檔。")
        return 1
    commits = work_commits()
    if not commits:
        return 1
    if not handover_updated():
        print("提醒：這項工作開始後還沒更新 docs/交接.md。")
    tests = NO_TESTS
    if with_tests:
        tests = run_tests()
        if not tests.startswith("通過"):
            print("測試沒有全部通過，沒有送審。先修好；確定要送的話請成員用 版本管理.bat 的「送審」。")
            return 1
    return 0 if open_review(commits, tests, lambda: title or commits.splitlines()[0][2:], "Claude Code") else 1


# ---- 存檔前的機密檢查 ----

def secret_values():
    """不能進公開儲存庫的值：這台電腦的 SECRET_KEY 與遠端 Ollama 伺服器的主機名稱。"""
    values = [local.read(local.LOCAL_FILE).get("SECRET_KEY")]
    for path in (local.PRIVATE_FILE, local.LOCAL_FILE):
        host = urllib.parse.urlsplit(local.read(path).get("OLLAMA_URL") or "").hostname
        if host not in LOCAL_HOSTS:
            values.append(host)
    return [v for v in values if v]


def files_with_secrets(diff, values):
    """git diff 裡新增的行（包括新的檔名）含有機密的檔案。刪掉機密的行不算，那正是要做的事。"""
    found, path = [], None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else line[4:]
        if line.startswith("+") and any(v in line for v in values) and path not in found:
            found.append(path)
    return found


def private_files(names):
    """不能進公開儲存庫的檔名：.md（只給成員看）與 private/（私人儲存庫本身）。平常由 .git/info/exclude 排除，
    這裡再擋還沒寫好排除清單的電腦，或用 git add -f 硬加進來的。"""
    return [n for n in names if n.lower().endswith(".md") or n == "private" or n.startswith("private/")]


def check_secrets():
    found = files_with_secrets(out(*GIT, "diff", "--cached", "-U0", "--no-color", "--no-ext-diff"), secret_values())
    docs = private_files(out(*GIT, "diff", "--cached", "--name-only", "--diff-filter=AR").splitlines())
    if found:
        print("沒有存檔：這些檔案含有不能公開的內容（研究用伺服器的位址或 SECRET_KEY），公開的儲存庫不能放：", file=sys.stderr)
        print("\n".join(f"  {f}" for f in found), file=sys.stderr)
        print("請改寫成「研究用伺服器」，位址只放在 private/settings.json（docs/交接.md 4.8）。", file=sys.stderr)
    if docs:
        print("沒有存檔：文件（.md）與 private/ 只給成員看，要放私人儲存庫，不能進公開的儲存庫：", file=sys.stderr)
        print("\n".join(f"  {f}" for f in docs), file=sys.stderr)
        print("請用 git rm --cached 取消，文件由 版本管理.bat 存進私人儲存庫（docs/交接.md 4.8）。", file=sys.stderr)
    return 1 if found or docs else 0


# ---- 選單 ----

def main():
    parser = argparse.ArgumentParser(description="版本管理選單。加參數時不問問題，給 Claude Code 用（.claude/skills/save-work）。")
    parser.add_argument("--check-secrets", action="store_true", help="存檔前的檢查（tools/githooks/pre-commit 用）")
    parser.add_argument("--status", action="store_true", help="目前的分支、改動、存檔與送審")
    parser.add_argument("--update", action="store_true", help="取得最新版本")
    parser.add_argument("--save", metavar="訊息", help="存檔；在主線上有程式改動時先開 work/<--name>")
    parser.add_argument("--name", help="--save 在主線上時，新工作分支的名稱（英文、數字、-）")
    parser.add_argument("--submit", action="store_true", help="送審（先跑完整測試，沒有全部通過就不送）")
    parser.add_argument("--title", help="--submit 的標題（預設是第一筆存檔的說明）")
    parser.add_argument("--no-tests", action="store_true", help="--submit 時不跑測試")
    args = parser.parse_args()
    if args.check_secrets:
        return check_secrets()
    if args.status or args.update or args.save or args.submit:
        fetch_private()
        if args.status:
            return status_cli()
        if args.update:
            return 0 if update(interactive=False) else 1
        if args.save:
            return save_cli(args.save, args.name)
        return submit_cli(args.title, not args.no_tests)
    if not setup():
        return 1
    admin = out("gh", "api", f"repos/{repo_of(ROOT)}", "--jq", ".permissions.admin") == "true"
    actions = {"1": update, "2": start_work, "3": commit, "4": submit, "5": review}
    if admin:
        actions["6"] = invite
        actions["7"] = members
    while True:
        branch = current_branch()
        print(f"\n== 專案版本管理（{repo_of(ROOT)}）==")
        print(f"目前分支：{branch}" + ("（主線）" if branch == MAIN else "") + f"，還沒存檔的改動：{len(changes())} 個檔案"
              + f"，文件（私人儲存庫）：{len(private_changes())} 個")
        print("1. 取得最新版本\n2. 開始新工作\n3. 存檔（commit）\n4. 送審\n5. 審核送審")
        if admin:
            print("6. 邀請成員\n7. 成員列表")
        print("0. 離開")
        choice = ask("請選擇：")
        if choice == "0":
            return 0
        if choice in actions:
            actions[choice]()
        else:
            print("請輸入選單上的數字。")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (EOFError, KeyboardInterrupt):
        sys.exit(0)
