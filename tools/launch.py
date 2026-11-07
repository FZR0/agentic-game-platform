"""一鍵部署與啟動（啟動對戰平台-本機AI.bat 用 --ai local、啟動對戰平台-VPN.bat 用 --ai vpn；一鍵部署 2026-10-15）。

2026-11-07 使用者決定只留這兩個啟動檔，都開真人對戰的通道，不再有只在本機玩的版本：
- 本機 AI：用這台電腦的 Ollama（tools/local_ollama.py，原本的本機測試模式）。
- VPN：用研究用伺服器，連不上就打開 FortiClient 等使用者連好學校的 VPN（tools/start_tunnel.wait_for_vpn）。
兩個都在任何一台新電腦上直接部署：找 Python、建 .venv（.bat）、補裝套件、SQL Server、資料庫、遊戲引擎、cloudflared。

啟動檔只負責找 Python 和第一次建立 .venv，而且只放 ASCII：.bat 裡有中文時，新開的主控台會讀錯位置，
把半行中文當成指令執行（docs/交接.md 第 7 節）。其餘步驟都在這裡：補裝套件、下載私人資料與設定 AI 伺服器位址（2026-10-21）、設定這台電腦的 SQL Server、更新資料庫、
搬 db.sqlite3 的舊對局、檢查並下載遊戲引擎、
檢查 AI 伺服器，最後以公開模式啟動網站並開 Cloudflare 臨時通道（tools/start_tunnel.run），打開通道網址。
網站、通道和這次啟動的本機 Ollama 都在這個視窗結束時一起停止。
"""
import argparse
import ctypes
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def responds(url, timeout=2):
    try:
        with urllib.request.urlopen(url, timeout=timeout):
            return True
    except OSError:
        return False


def python(*args, **kwargs):
    return subprocess.run([sys.executable, *args], cwd=ROOT, **kwargs).returncode == 0


def main():
    parser = argparse.ArgumentParser(description="部署並啟動遊戲平台（真人對戰，對外公開）")
    parser.add_argument("--ai", choices=["local", "vpn"], required=True, help="local：這台電腦的 Ollama；vpn：研究用伺服器")
    parser.add_argument("--port", type=int, default=8010, help="網站的埠（只聽 127.0.0.1，別台電腦經過通道連進來）")
    parser.add_argument("--no-browser", action="store_true", help="不自動打開瀏覽器")
    args = parser.parse_args()
    where = "本機 AI" if args.ai == "local" else "研究用伺服器"
    ctypes.windll.kernel32.SetConsoleTitleW(f"益智遊戲 AI 教練（{where}，對外公開中；關閉這個視窗就會停止）")
    # 用 127.0.0.1 檢查：localhost 會先試 IPv6，開發伺服器只聽 IPv4，每次要多等約 2 秒
    # 檢查用 CSS 檔：每個頁面都交給 AI 助理（2026-10-28），首頁第一次要等模型十幾秒，
    # 檢查會逾時、誤以為伺服器沒開，而且每試一次就多叫一次模型
    if responds(f"http://127.0.0.1:{args.port}/static/arena/style.css"):
        print(f"網站已經在另一個視窗執行（埠 {args.port}），對外網址在那個視窗裡。要重開的話先關掉那個視窗。")
        input("按 Enter 結束。")
        return 0
    if args.ai == "local":
        # 先設好：之後載入的 Django 設定、啟動的網站都改用本機 Ollama，也不會問研究用伺服器的位址
        from tools import local_ollama
        os.environ.update(local_ollama.env())

    # 上次安裝到一半失敗時，環境在但套件不完整，所以檢查套件能不能載入，不能就重新安裝。
    # requirements.txt 加了套件時這裡也要加：舊電腦的環境裡有 Django、沒有新套件（例如國際象棋的 chess），
    # 只檢查 Django 的話不會補裝，整個網站會啟動失敗
    if not python("-c", "import django, chess, mssql, pyodbc, channels, daphne", capture_output=True):
        print("正在安裝套件...")
        if not python("-m", "pip", "install", "-r", "requirements.txt"):
            print("安裝套件失敗，請查看上面的錯誤訊息（需要能連上網路）。")
            return 1
    sys.path.insert(0, str(ROOT))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    # 研究用伺服器的位址不能公開（2026-10-21），放在私人儲存庫：受邀的成員在這裡自動下載，位址就不用輸入。
    # 沒有權限的電腦問一次，存在 config/local.json。要在載入 Django 設定之前做，設定才讀得到
    from tools import git_menu
    git_menu.fetch_private()
    from config import local
    if local.get("OLLAMA_URL") is None:
        print("還沒有設定 AI 伺服器（Ollama）的位址。研究用伺服器的位址請向專案負責人索取，")
        print("或請負責人邀請你加入私人儲存庫，之後啟動時會自動設定。")
        url = input("請輸入位址（例如 http://伺服器:11434；直接按 Enter 用這台電腦的 Ollama）：").strip().rstrip("/")
        if url and "://" not in url:
            url = "http://" + url
        local.save("OLLAMA_URL", url or "http://127.0.0.1:11434")
    # 資料庫用這台電腦的 SQL Server（2026-10-19）：第一次請管理者選自動或手動；找不到或連不上就停下來說明。
    # 和下載引擎一樣在這個程序裡呼叫，才拿得到輸入
    from tools import setup_sqlserver
    if setup_sqlserver.main():
        return 1
    if not python("manage.py", "migrate", "--noinput", stdout=subprocess.DEVNULL):
        print("更新資料庫失敗，請查看上面的錯誤訊息。")
        return 1
    # 改用 SQL Server 之前的對局在 db.sqlite3：SQL Server 還沒有任何資料時搬一次。搬失敗就停下來，
    # 否則開始下新的對局後就不會再搬，舊對局會留在 db.sqlite3
    if (ROOT / "db.sqlite3").exists() and not python("manage.py", "import_sqlite_games"):
        print("搬移 db.sqlite3 的舊對局失敗，請查看上面的錯誤訊息。")
        return 1
    # 缺少圍棋、象棋引擎時問一次再從官方網站下載；不下載也能玩其他遊戲。
    # 在這個程序裡直接呼叫，不另開子程序：子程序拿不到導向的輸入，測試時回答不到下載的問題（2026-10-15）
    from tools import setup_engines
    setup_engines.main()

    # 開通道要用的 cloudflared：沒有時問一次再下載。放在啟動任何程式之前，不下載就直接結束
    from tools import start_tunnel
    if not start_tunnel.ensure_cloudflared():
        return 1
    from tools.local_ollama import KillOnExit
    group = KillOnExit()  # 這次啟動的本機 Ollama、網站、通道：這個視窗結束時一起停止
    if args.ai == "local":
        if not local_ollama.start(group):
            return 1
    elif not start_tunnel.wait_for_vpn():
        return 0

    # 檢查 AI 伺服器：有問題時印出原因與排除步驟（查不出原因時印可能原因），並存一份故障報告（arena/health.py）
    import django
    django.setup()
    from django.conf import settings
    from django.contrib.auth.models import User
    from arena import health
    problem = health.check()
    if problem:
        print(health.as_text(problem))
    if not User.objects.filter(is_superuser=True).exists():
        print("提醒：還沒有管理員帳號，後台（/admin/）看不到登入紀錄與線上訪客。")
        print("要建立的話，另開一個視窗在專案資料夾執行：.venv\\Scripts\\python.exe manage.py createsuperuser\n")
    print(f"運行紀錄（AI 伺服器、AI 助理、錯誤）會顯示在這個視窗，也存在 {settings.LOG_FILE}")
    return start_tunnel.run(group, args.port, open_browser=not args.no_browser)

if __name__ == "__main__":
    sys.exit(main())
