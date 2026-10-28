"""一鍵啟動（由 啟動遊戲平台.bat 呼叫；一鍵部署 2026-10-15）。

啟動檔只負責找 Python 和第一次建立 .venv，而且只放 ASCII：.bat 裡有中文時，新開的主控台會讀錯位置，
把半行中文當成指令執行（docs/交接.md 第 7 節）。其餘步驟都在這裡：補裝套件、下載私人資料與設定 AI 伺服器位址（2026-10-21）、設定這台電腦的 SQL Server、更新資料庫、
搬 db.sqlite3 的舊對局、檢查並下載遊戲引擎、
檢查研究用 Ollama 伺服器、開網站伺服器視窗、打開瀏覽器。
"""
import argparse
import ctypes
import os
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CREATE_NEW_CONSOLE = 0x10
SW_SHOWMINNOACTIVE = 7


def responds(url, timeout=2):
    try:
        with urllib.request.urlopen(url, timeout=timeout):
            return True
    except OSError:
        return False


def python(*args, **kwargs):
    return subprocess.run([sys.executable, *args], cwd=ROOT, **kwargs).returncode == 0


def main():
    parser = argparse.ArgumentParser(description="啟動遊戲平台")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser", action="store_true", help="不自動打開瀏覽器")
    args = parser.parse_args()
    ctypes.windll.kernel32.SetConsoleTitleW("益智遊戲 AI 教練")
    # 用 127.0.0.1 檢查：localhost 會先試 IPv6，開發伺服器只聽 IPv4，每次要多等約 2 秒
    site_check = f"http://127.0.0.1:{args.port}/"
    site_url = f"http://localhost:{args.port}/"

    if responds(site_check):  # 伺服器已經在執行時，直接打開網頁
        if not args.no_browser:
            webbrowser.open(site_url)
        return 0

    # 上次安裝到一半失敗時，環境在但套件不完整，所以檢查套件能不能載入，不能就重新安裝。
    # requirements.txt 加了套件時這裡也要加：舊電腦的環境裡有 Django、沒有新套件（例如國際象棋的 chess），
    # 只檢查 Django 的話不會補裝，整個網站會啟動失敗
    if not python("-c", "import django, chess, mssql, pyodbc", capture_output=True):
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

    from django.conf import settings
    if not responds(f"{settings.OLLAMA_URL}/api/tags", timeout=5):
        print(f"連不上 Ollama 伺服器 {settings.OLLAMA_URL}，AI 會改由引擎代下、說明改顯示引擎原文。")

    print(f"運行紀錄（AI 伺服器、AI 助理、錯誤）會顯示在「遊戲平台伺服器」視窗，也存在 {settings.LOG_FILE}")
    print("伺服器啟動中...")
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = SW_SHOWMINNOACTIVE
    # 網站伺服器開在自己的視窗：關閉那個視窗就會停止。整串交給 cmd，cmd 會拿掉最外面那對引號
    subprocess.Popen(f'cmd /k "title 遊戲平台伺服器（關閉這個視窗就會停止）& "{sys.executable}" manage.py runserver {args.port}"',
                     cwd=ROOT, creationflags=CREATE_NEW_CONSOLE, startupinfo=startup)
    for _ in range(30):
        if responds(site_check):
            break
        time.sleep(1)
    else:
        print("伺服器沒有在 30 秒內啟動，請查看「遊戲平台伺服器」視窗裡的錯誤訊息。")
        return 1
    if not args.no_browser:
        webbrowser.open(site_url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
