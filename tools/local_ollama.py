"""用這台電腦的 Ollama 當 AI（啟動對戰平台-本機AI.bat → tools/launch.py --ai local）。

2026-10-15 的本機測試模式（使用者決定）在 2026-11-07 併進一鍵啟動：啟動檔只剩「本機 AI」與「學校 VPN」兩個，
都開真人對戰的通道。為了不拖慢電腦：預設用小模型 gemma4:e2b、同時只載入一個模型、閒置 2 分鐘就卸載、
本機 Ollama 用低於標準的優先權。

這次啟動的本機 Ollama 和網站都放進同一個 Windows 工作物件（job object，KillOnExit）：啟動程式一結束（按 Enter、
關閉視窗或被強制結束），系統就把它們一起結束，不會留在背景佔用顯示卡記憶體或對外公開。只靠「關閉主控台視窗
會結束裡面的程式」不可靠：2026-10-15 實測直接結束主控台時，背景的 Ollama 和網站都還在跑。
"""
import ctypes
import json
import os
import subprocess
import time
import urllib.request
from ctypes import wintypes
from pathlib import Path

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


def env(port=11434):
    """網站改用本機 Ollama 的環境變數。tools/launch.py 一開始就設進自己的環境：之後載入的設定與啟動的網站都會沿用，
    也不會再問研究用伺服器的位址。用 127.0.0.1：localhost 會先試 IPv6，本機 Ollama 只聽 IPv4。"""
    return {"OLLAMA_URL": f"http://127.0.0.1:{port}", "OLLAMA_MODEL": MODEL, "OLLAMA_KEEP_ALIVE": KEEP_ALIVE}


def start(group, port=11434):
    """本機 Ollama 沒在執行就啟動它（放進 group）；成功回傳 True。"""
    url = f"http://127.0.0.1:{port}"
    if responds(f"{url}/api/tags"):
        print("本機 Ollama 已經在執行，直接沿用：「同時只載入一個模型」要等它重新啟動才會生效，關閉這個視窗也不會停止它。")
    else:
        if not OLLAMA_EXE.exists():
            print(f"找不到本機 Ollama：{OLLAMA_EXE}\n請先到 https://ollama.com 下載安裝，或改用 啟動對戰平台-VPN.bat。")
            return False
        print("正在啟動本機 Ollama...")
        group.start([str(OLLAMA_EXE), "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=BELOW_NORMAL_PRIORITY_CLASS,
                    env=dict(os.environ, OLLAMA_HOST=f"127.0.0.1:{port}",
                             OLLAMA_MAX_LOADED_MODELS="1", OLLAMA_NUM_PARALLEL="1"))
        if not wait_until(f"{url}/api/tags", 20):
            print("本機 Ollama 沒有在 20 秒內啟動。")
            return False
    with urllib.request.urlopen(f"{url}/api/tags", timeout=10) as resp:
        installed = {m["name"] for m in json.load(resp)["models"]}
    if MODEL not in installed:
        print(f"本機沒有 {MODEL} 這個模型，AI 會改由電腦代下。要下載的話執行：ollama pull {MODEL}")
    return True
