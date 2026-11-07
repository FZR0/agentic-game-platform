"""真人對戰：用 Cloudflare 臨時通道讓別台電腦連進來（tools/launch.py 呼叫；2026-11-05，docs/交接.md 4.10）。
2026-11-07 起兩個啟動檔（本機 AI、學校 VPN）都走這裡，不再有只在本機玩的啟動檔（使用者決定）。

- ensure_cloudflared：沒有 tools/cloudflared/cloudflared.exe 時問一次，從 Cloudflare 在 GitHub 的官方發行版下載，
  比對大小與 SHA-256（不符就刪掉）。
- wait_for_vpn（VPN 版）：連不上研究用伺服器時打開 FortiClient，等使用者連好學校的 VPN（按 S 不等、按 Q 結束）。
- run：以公開模式（GAME_PUBLIC=1：DEBUG 關閉、只接受通道網址，見 config/settings.py）啟動網站，只聽 127.0.0.1，
  別台電腦只能經過通道連進來；再開臨時通道（`cloudflared tunnel --url`，不需要 Cloudflare 帳號），印出並打開
  https://<隨機>.trycloudflare.com。

網站和 cloudflared 放進同一個 Windows 工作物件（tools/local_ollama.KillOnExit）：按 Enter、關閉視窗
或被強制結束時一起停止，不會留下一個對外公開的網站。每次啟動網址都不一樣；臨時通道沒有穩定性保證，只適合測試。
"""
import msvcrt
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.setup_engines import download  # noqa: E402  比對大小與 SHA-256 的下載，和引擎共用
from tools.local_ollama import wait_until  # noqa: E402
from config import local  # noqa: E402  AI 伺服器位址的來源和網站相同（環境變數 → private/settings.json → config/local.json）

# 2026.10.3（2026-10-21 發布）：刻意不用最新版，選發布超過兩週的版本。大小與 SHA-256 是 GitHub 發行版公布的值（2026-11-05 查）
CLOUDFLARED = ("https://github.com/cloudflare/cloudflared/releases/download/2026.10.3/cloudflared-windows-amd64.exe",
               55329336, "76b2805253d0d6ffacb092a7acfbee99e7af6917835c0c5410e23f3ef5052da2")
CLOUDFLARED_EXE = ROOT / "tools" / "cloudflared" / "cloudflared.exe"
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
FORTICLIENT = Path(r"C:\Program Files\Fortinet\FortiClient\FortiClient.exe")  # 學校的 VPN 軟體（使用者 2026-11-07）


def ai_server_reachable():
    """連得上 AI 伺服器的埠嗎（只連 TCP，3 秒）。位址不印出來：研究用伺服器的位址不能公開。"""
    url = urllib.parse.urlsplit(local.get("OLLAMA_URL", "http://127.0.0.1:11434"))
    try:
        with socket.create_connection((url.hostname, url.port or 11434), timeout=3):
            return True
    except OSError:
        return False


def wait_for_vpn():
    """VPN 版（啟動對戰平台-VPN.bat）：連不上研究用伺服器時打開 FortiClient，等使用者連好 VPN 再啟動網站。
    帳號密碼由使用者自己在 FortiClient 輸入，這裡不經手。回傳 False 表示使用者選擇結束。"""
    if ai_server_reachable():
        print("研究用伺服器連得上。")
        return True
    print("連不上研究用伺服器：在校外要先連學校的 VPN。")
    if FORTICLIENT.exists():
        subprocess.Popen([str(FORTICLIENT)])
        print("已打開 FortiClient，請在裡面連上學校的 VPN。")
    else:
        print(f"找不到 FortiClient（{FORTICLIENT}），請自己打開學校的 VPN 軟體連線。")
    print("連上後會自動繼續。按 S 不等了直接啟動（AI 會改用原本的程式），按 Q 結束。")
    while True:
        for _ in range(30):  # 每 3 秒檢查一次連線，中間每 0.1 秒看一次按鍵
            while msvcrt.kbhit():
                key = msvcrt.getwch().lower()
                if key == "s":
                    print("不等 VPN，直接啟動。")
                    return True
                if key == "q":
                    return False
            time.sleep(0.1)
        if ai_server_reachable():
            print("研究用伺服器連得上了。")
            return True


def ensure_cloudflared():
    if CLOUDFLARED_EXE.exists():
        return True
    url, size, sha256 = CLOUDFLARED
    print(f"需要 Cloudflare 的 cloudflared（官方發行版 2026.10.3，{size / 1e6:.1f} MB），用來開臨時通道。")
    if input("要現在下載嗎？(Y/N) ").strip().lower() not in ("y", "yes"):
        print("沒有下載，無法開通道。")
        return False
    CLOUDFLARED_EXE.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        part = Path(tmp) / "cloudflared.exe"
        try:
            download(url, size, sha256, part)
        except (OSError, ValueError) as exc:
            print(f"下載失敗：{exc}")
            return False
        part.replace(CLOUDFLARED_EXE)
    return True


def run(group, port, open_browser=True):
    """以公開模式啟動網站並開通道（都放進 group），等使用者按 Enter；回傳結束碼。"""
    site_check = f"http://127.0.0.1:{port}/static/arena/style.css"  # 不經過 AI 助理（每個頁面都會叫模型）
    print("正在以公開模式啟動網站...")
    # --insecure：DEBUG 關閉時 runserver 預設不提供靜態檔（CSS、JS），測試用的網站由它直接提供
    group.start([sys.executable, "manage.py", "runserver", "--insecure", f"127.0.0.1:{port}"], cwd=ROOT,
                env=dict(os.environ, GAME_PUBLIC="1"))
    if not wait_until(site_check, 30):
        print("網站沒有在 30 秒內啟動，請看上面的錯誤訊息。")
        return 1

    print("正在開 Cloudflare 臨時通道...")
    tunnel = group.start([str(CLOUDFLARED_EXE), "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    found = threading.Event()

    def read_tunnel_output():
        # cloudflared 把網址印在 stderr 的一個框框裡；其餘輸出（連線狀態）不顯示，免得蓋掉網址
        for line in tunnel.stderr:
            m = URL_RE.search(line)
            if m and not found.is_set():
                found.set()
                print("\n" + "=" * 60)
                print(f"  對外網址：{m.group(0)}")
                print("  把這個網址傳給朋友：註冊、登入或以遊客身分進入後，用房間密碼或邀請連結加入對局。")
                print(f"  管理員後台（登入紀錄、線上訪客）：{m.group(0)}/admin/")
                print("=" * 60)
                print("關掉網站：按 Enter，或直接關閉這個視窗，網站和通道會一起停止。")
                # 自己也用通道網址玩：邀請連結是用網址列的網址組成的，用 localhost 開的話朋友點不進來
                if open_browser:
                    webbrowser.open(m.group(0))

    threading.Thread(target=read_tunnel_output, daemon=True).start()
    if not found.wait(60):
        print(f"60 秒內沒有拿到通道網址，可能連不上 Cloudflare。網站仍在本機執行（http://localhost:{port}/）；按 Enter 結束。")
    try:
        input()
    except (EOFError, KeyboardInterrupt):
        pass
    return 0
