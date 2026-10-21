"""本機測試模式（由 啟動本機測試.bat 呼叫；2026-10-15 使用者決定）。

只有這次執行改用本機 Ollama，平常的 啟動遊戲平台.bat 仍只用研究用伺服器。為了不拖慢電腦：預設用小模型
gemma4:e2b、同時只載入一個模型、閒置 2 分鐘就卸載、本機 Ollama 用低於標準的優先權。網站開在 8001 埠，
和平常的 8000 分開。

這次啟動的本機 Ollama 和網站都放進同一個 Windows 工作物件（job object）：這個程式一結束（按 Enter、關閉視窗
或被強制結束），系統就把它們一起結束，不會留在背景佔用顯示卡記憶體。只靠「關閉主控台視窗會結束裡面的程式」
不可靠：2026-10-15 實測直接結束主控台時，背景的 Ollama 和網站都還在跑。
"""
import argparse
import ctypes
import json
import os
import subprocess
import sys
import time
import urllib.request
import webbrowser
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OLLAMA_EXE = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
MODEL = os.environ.get("OLLAMA_MODEL", "gemma4:e2b")  # 選單裡最小的模型；原開發機本機實測每手 7–9 秒
KEEP_ALIVE = "2m"
BELOW_NORMAL_PRIORITY_CLASS = 0x4000
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9


class _BasicLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", ctypes.c_ulonglong * 6),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


class KillOnExit:
    """放進來的程序（和它們之後開的子程序）會在這個程式結束時被系統一起結束。"""

    def __init__(self):
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.job = self.kernel32.CreateJobObjectW(None, None)
        if not self.job:
            raise ctypes.WinError(ctypes.get_last_error())
        info = _ExtendedLimits()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel32.SetInformationJobObject(self.job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                                                     ctypes.byref(info), ctypes.sizeof(info)):
            raise ctypes.WinError(ctypes.get_last_error())

    def start(self, args, **kwargs):
        proc = subprocess.Popen(args, **kwargs)
        if not self.kernel32.AssignProcessToJobObject(self.job, int(proc._handle)):
            proc.kill()
            raise ctypes.WinError(ctypes.get_last_error())
        return proc


def responds(url, timeout=2):
    try:
        with urllib.request.urlopen(url, timeout=timeout):
            return True
    except OSError:
        return False


def wait_until(url, seconds):
    for _ in range(seconds):
        if responds(url):
            return True
        time.sleep(1)
    return False


def main():
    parser = argparse.ArgumentParser(description="本機測試模式：用本機 Ollama 開一個測試用的網站")
    parser.add_argument("--site-port", type=int, default=8001)
    parser.add_argument("--ollama-port", type=int, default=11434)
    parser.add_argument("--no-browser", action="store_true", help="不自動打開瀏覽器")
    args = parser.parse_args()
    ctypes.windll.kernel32.SetConsoleTitleW("益智遊戲 AI 教練（本機測試）")
    # 用 127.0.0.1 而不是 localhost：localhost 會先試 IPv6，本機 Ollama 和開發伺服器都只聽 IPv4
    ollama_url = f"http://127.0.0.1:{args.ollama_port}"
    site_check = f"http://127.0.0.1:{args.site_port}/"
    site_url = f"http://localhost:{args.site_port}/"

    if responds(site_check):
        print(f"本機測試的網站已經在執行：{site_url}")
        if not args.no_browser:
            webbrowser.open(site_url)
        return 0

    group = KillOnExit()
    env = dict(os.environ, OLLAMA_URL=ollama_url, OLLAMA_MODEL=MODEL, OLLAMA_KEEP_ALIVE=KEEP_ALIVE)
    if responds(f"{ollama_url}/api/tags"):
        print("本機 Ollama 已經在執行，直接沿用：「同時只載入一個模型」要等它重新啟動才會生效，關閉這個視窗也不會停止它。")
    else:
        if not OLLAMA_EXE.exists():
            print(f"找不到本機 Ollama：{OLLAMA_EXE}\n請先到 https://ollama.com 下載安裝。")
            return 1
        print("正在啟動本機 Ollama...")
        group.start([str(OLLAMA_EXE), "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=BELOW_NORMAL_PRIORITY_CLASS,
                    env=dict(env, OLLAMA_HOST=f"127.0.0.1:{args.ollama_port}",
                             OLLAMA_MAX_LOADED_MODELS="1", OLLAMA_NUM_PARALLEL="1"))
        if not wait_until(f"{ollama_url}/api/tags", 20):
            print("本機 Ollama 沒有在 20 秒內啟動。")
            return 1

    with urllib.request.urlopen(f"{ollama_url}/api/tags", timeout=10) as resp:
        installed = {m["name"] for m in json.load(resp)["models"]}
    if MODEL not in installed:
        print(f"本機沒有 {MODEL} 這個模型，AI 會改由引擎代下。要下載的話執行：ollama pull {MODEL}")

    print("正在啟動網站...")
    group.start([sys.executable, "manage.py", "runserver", str(args.site_port)], cwd=ROOT, env=env)
    if not wait_until(site_check, 30):
        print("網站沒有在 30 秒內啟動，請看上面的錯誤訊息。")
        return 1
    if not args.no_browser:
        webbrowser.open(site_url)
    print(f"\n本機測試中：網站 {site_url}，AI 用本機 Ollama 的 {MODEL}。")
    print("測完按 Enter，或直接關閉這個視窗，網站和這次啟動的本機 Ollama 會一起停止。")
    try:
        input()
    except (EOFError, KeyboardInterrupt):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
