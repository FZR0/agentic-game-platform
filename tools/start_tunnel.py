"""真人對戰測試：用 Cloudflare 臨時通道讓別台電腦連進來（由 啟動對戰測試.bat 呼叫；2026-11-05，docs/交接.md 4.10）。

做的事：
1. 沒有 tools/cloudflared/cloudflared.exe 時問一次，從 Cloudflare 在 GitHub 的官方發行版下載，比對大小與 SHA-256（不符就刪掉）。
2. 以公開模式（GAME_PUBLIC=1：DEBUG 關閉、只接受通道網址，見 config/settings.py）啟動網站，只聽 127.0.0.1，
   別台電腦只能經過通道連進來。
3. 開臨時通道（`cloudflared tunnel --url`，不需要 Cloudflare 帳號），印出 https://<隨機>.trycloudflare.com 網址。

網站和 cloudflared 放進同一個 Windows 工作物件（和 tools/start_local_test.py 同一個做法）：按 Enter、關閉視窗
或被強制結束時一起停止，不會留下一個對外公開的網站。每次啟動網址都不一樣；臨時通道沒有穩定性保證，只適合測試。
"""
import argparse
import ctypes
import os
import re
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.setup_engines import download  # noqa: E402  比對大小與 SHA-256 的下載，和引擎共用
from tools.start_local_test import KillOnExit, wait_until  # noqa: E402

# 2026.10.3（2026-10-21 發布）：刻意不用最新版，選發布超過兩週的版本。大小與 SHA-256 是 GitHub 發行版公布的值（2026-11-05 查）
CLOUDFLARED = ("https://github.com/cloudflare/cloudflared/releases/download/2026.10.3/cloudflared-windows-amd64.exe",
               55329336, "76b2805253d0d6ffacb092a7acfbee99e7af6917835c0c5410e23f3ef5052da2")
CLOUDFLARED_EXE = ROOT / "tools" / "cloudflared" / "cloudflared.exe"
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


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


def has_admin():
    """有沒有管理員帳號（後台看登入紀錄與線上訪客要用）。"""
    code = ("import django, os; os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings'); django.setup(); "
            "from django.contrib.auth.models import User; print(User.objects.filter(is_superuser=True).exists())")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
    return out.stdout.strip() == "True"


def main():
    parser = argparse.ArgumentParser(description="真人對戰測試：開 Cloudflare 臨時通道")
    parser.add_argument("--port", type=int, default=8010, help="網站的埠（只聽 127.0.0.1；預設 8010，和平常的 8000 分開）")
    args = parser.parse_args()
    ctypes.windll.kernel32.SetConsoleTitleW("益智遊戲 AI 教練（真人對戰測試：對外公開中）")
    site_check = f"http://127.0.0.1:{args.port}/static/arena/style.css"  # 不經過 AI 助理（同 tools/launch.py）

    if not ensure_cloudflared():
        return 1
    if not has_admin():
        print("提醒：還沒有管理員帳號，後台（/admin/）看不到登入紀錄與線上訪客。")
        print("要建立的話，另開一個視窗在專案資料夾執行：.venv\\Scripts\\python.exe manage.py createsuperuser\n")

    group = KillOnExit()
    print("正在以公開模式啟動網站...")
    # --insecure：DEBUG 關閉時 runserver 預設不提供靜態檔（CSS、JS），測試用的網站由它直接提供
    group.start([sys.executable, "manage.py", "runserver", "--insecure", f"127.0.0.1:{args.port}"], cwd=ROOT,
                env=dict(os.environ, GAME_PUBLIC="1"))
    if not wait_until(site_check, 30):
        print("網站沒有在 30 秒內啟動，請看上面的錯誤訊息。")
        return 1

    print("正在開 Cloudflare 臨時通道...")
    tunnel = group.start([str(CLOUDFLARED_EXE), "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{args.port}"],
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
                print("  把這個網址傳給朋友：註冊或登入後，用房間密碼或邀請連結加入對局。")
                print(f"  管理員後台（登入紀錄、線上訪客）：{m.group(0)}/admin/")
                print("=" * 60)
                print("測完按 Enter，或直接關閉這個視窗，網站和通道會一起停止。")

    threading.Thread(target=read_tunnel_output, daemon=True).start()
    if not found.wait(60):
        print("60 秒內沒有拿到通道網址，可能連不上 Cloudflare。網站仍在本機執行；按 Enter 結束。")
    try:
        input()
    except (EOFError, KeyboardInterrupt):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
