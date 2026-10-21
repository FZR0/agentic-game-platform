"""檢查遊戲引擎（圍棋 KataGo、象棋 Pikafish、國際象棋 Stockfish），缺少時問一次再從官方發行版下載
（啟動遊戲平台.bat 呼叫；2026-10-15 使用者決定）。

只補缺少的檔案，已經有的（包括調過的設定檔）不會覆蓋。每個下載的檔案都先比對大小和 SHA-256（GitHub 發行版公布的值；
KataGo 網路是原開發機那份的值），不符就刪掉、不使用。檔案位置照 config/settings.py（可用 KATAGO_EXE、KATAGO_MODEL、
KATAGO_CONFIG、PIKAFISH_EXE、STOCKFISH_EXE 環境變數改）。
Stockfish 用 universal 版，執行時自動選這台 CPU 支援的指令集；壓縮檔裡有 GPLv3 授權檔與原始碼，一起放著。
KataGo：系統裡有 OpenCL（顯示卡驅動會裝 C:\\Windows\\System32\\OpenCL.dll）就用 OpenCL 版，否則用 CPU 版
（CPU 支援 AVX2 用 eigenavx2，不支援用 eigen）。Pikafish 用壓縮檔裡的通用版執行檔，任何 x86-64 電腦都能跑；
壓縮檔是 7z，用 Windows 內建的 tar 解（Git Bash 的 GNU tar 不能解 7z，所以指定 System32 的路徑）。
"""
import ctypes
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

KATAGO_RELEASE = "https://github.com/lightvector/KataGo/releases/download/v1.18.1/"
KATAGO_BUILDS = {  # 名稱: (檔名, 大小, SHA-256)
    "opencl": ("katago-v1.18.1-opencl-windows-x64.zip", 6004137,
               "1710db1903ab921aa6837a9599c8474f8a59f057650217c5d9bc125ee393a9ff"),
    "eigenavx2": ("katago-v1.18.1-eigenavx2-windows-x64.zip", 5899607,
                  "0d62ffa41ee04dd89dd1b80fe45e306c837231cb1e2dccb4f5780d0ca7c313db"),
    "eigen": ("katago-v1.18.1-eigen-windows-x64.zip", 5903072,
              "074485cf150c38aa3bb14ac9f54f2952ffefbceb44673709bbb8a83650bf95d6"),
}
KATAGO_BUILD_NAMES = {"opencl": "顯示卡（OpenCL）版", "eigenavx2": "CPU（AVX2）版", "eigen": "CPU 版"}
KATAGO_MODEL = ("https://media.katagotraining.org/uploaded/networks/models/kata1/kata1-tf3-b11c768-s12252M-d6398M.bin.gz",
                262014270, "ad279eb0e6aa4ddf52cf24b27241d220043b31f38064b3fc3a43d5a524e6b03d")
PIKAFISH = ("https://github.com/official-pikafish/Pikafish/releases/download/Pikafish-2026-09-06/Pikafish.2026-09-06.7z",
            53603424, "41952bbfe2520faceb5902c69e6ab4845cc999841d2b49a95cc1be7867a25e5b")
PIKAFISH_WINDOWS_EXE = "Pikafish-Windows-x86-64-universal.exe"
PIKAFISH_EXTRAS = ("pikafish.nnue", "Copying.txt", "NNUE-License.md")  # 權重與授權檔，和執行檔放在同一個資料夾
# 大小與 SHA-256：2026-10-14 下載時和 GitHub 公布的值比對相符（docs/交接.md 第 6 節）
STOCKFISH = ("https://github.com/official-stockfish/Stockfish/releases/download/sf_19/stockfish-windows-x86-64-universal.zip",
             81431614, "3c8bf1f9ea66a09350a40df4f632288285ac206d99f33ab5842c408fc30b48a7")
SYSTEM_TAR = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "tar.exe"
PF_AVX2_INSTRUCTIONS_AVAILABLE = 40


def katago_build():
    """這台電腦適合的 KataGo 版本。"""
    if (Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "OpenCL.dll").exists():
        return "opencl"
    if ctypes.windll.kernel32.IsProcessorFeaturePresent(PF_AVX2_INSTRUCTIONS_AVAILABLE):
        return "eigenavx2"
    return "eigen"


def download(url, size, sha256, dest):
    """下載到 dest；大小或 SHA-256 不符時刪掉並丟出 ValueError。"""
    digest = hashlib.sha256()
    done, shown = 0, -1
    req = urllib.request.Request(url, headers={"User-Agent": "game-platform-setup"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as out:
        while chunk := resp.read(1 << 20):
            out.write(chunk)
            digest.update(chunk)
            done += len(chunk)
            percent = done * 100 // size
            if percent // 10 != shown // 10:
                shown = percent
                print(f"  {percent}%（{done / 1e6:.1f} / {size / 1e6:.1f} MB）", flush=True)
    if done != size or digest.hexdigest() != sha256:
        dest.unlink()
        raise ValueError(f"{url} 下載的檔案和官方公布的不一樣（大小或 SHA-256 不符），已刪除")


def copy_missing(src_dir, dest_dir):
    """把 src_dir 裡的檔案複製到 dest_dir，已經存在的不覆蓋。"""
    for src in src_dir.rglob("*"):
        if src.is_file():
            dest = dest_dir / src.relative_to(src_dir)
            if not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)


def unzip(archive, dest):
    """解開 zip 到 dest，回傳內容所在的資料夾（壓縮檔裡多包一層資料夾時拿掉那一層）。"""
    with zipfile.ZipFile(archive) as z:
        z.extractall(dest)
    roots = list(dest.iterdir())
    return roots[0] if len(roots) == 1 and roots[0].is_dir() else dest


def install_katago(tmp, exe):
    build = katago_build()
    name, size, sha256 = KATAGO_BUILDS[build]
    print(f"下載 KataGo（{KATAGO_BUILD_NAMES[build]}）...")
    archive = tmp / name
    download(KATAGO_RELEASE + name, size, sha256, archive)
    copy_missing(unzip(archive, tmp / "katago"), exe.parent)


def install_stockfish(tmp, exe):
    print("下載 Stockfish...")
    archive = tmp / "stockfish.zip"
    download(*STOCKFISH, archive)
    copy_missing(unzip(archive, tmp / "stockfish"), exe.parent)
    if not exe.exists():  # 用 STOCKFISH_EXE 改了檔名時，壓縮檔裡的執行檔名稱對不上
        raise ValueError(f"Stockfish 壓縮檔裡沒有 {exe.name}")


def install_pikafish(tmp, exe):
    if not SYSTEM_TAR.exists():
        raise ValueError(f"找不到 Windows 內建的 tar（{SYSTEM_TAR}），無法解開 Pikafish 的 7z 壓縮檔")
    print("下載 Pikafish...")
    url, size, sha256 = PIKAFISH
    archive = tmp / "pikafish.7z"
    download(url, size, sha256, archive)
    unpacked = tmp / "pikafish"
    unpacked.mkdir()
    subprocess.run([str(SYSTEM_TAR), "-xf", str(archive), "-C", str(unpacked)], check=True)
    exe.parent.mkdir(parents=True, exist_ok=True)
    if not exe.exists():
        shutil.copy2(next(unpacked.rglob(PIKAFISH_WINDOWS_EXE)), exe)
    for extra in PIKAFISH_EXTRAS:
        dest = exe.with_name(extra)
        found = next(unpacked.rglob(extra), None)
        if found and not dest.exists():
            shutil.copy2(found, dest)


def check_engines(katago_exe, pikafish_exe, stockfish_exe):
    """確認下載的引擎在這台電腦上跑得起來。"""
    if katago_exe.exists():
        out = subprocess.run([str(katago_exe), "version"], capture_output=True, text=True, timeout=60)
        print("KataGo：" + (out.stdout.splitlines()[0] if out.returncode == 0 and out.stdout else "無法啟動，請看上面的訊息"))
    for name, exe in (("Pikafish", pikafish_exe), ("Stockfish", stockfish_exe)):
        if exe.exists():
            out = subprocess.run([str(exe)], input="uci\nquit\n", capture_output=True, text=True, timeout=60, cwd=exe.parent)
            print(f"{name}：" + ("可以使用" if "uciok" in out.stdout else "無法啟動，請看上面的訊息"))


def main():
    from django.conf import settings

    katago_exe = Path(settings.KATAGO["exe"])
    model = Path(settings.KATAGO["model"])
    config = Path(settings.KATAGO["config"])
    pikafish_exe = Path(settings.PIKAFISH_EXE)
    stockfish_exe = Path(settings.STOCKFISH["exe"])
    need_katago = not katago_exe.exists() or not config.exists()
    need_model = not model.exists()
    need_pikafish = not pikafish_exe.exists() or not pikafish_exe.with_name("pikafish.nnue").exists()
    need_stockfish = not stockfish_exe.exists()
    if not (need_katago or need_model or need_pikafish or need_stockfish):
        return 0

    plan = []
    if need_katago:
        name, size, _ = KATAGO_BUILDS[katago_build()]
        plan.append(f"圍棋引擎 KataGo v1.18.1（{KATAGO_BUILD_NAMES[katago_build()]}，{name}，{size / 1e6:.1f} MB）")
    if need_model:
        if model.name != KATAGO_MODEL[0].rsplit("/", 1)[1]:
            print(f"找不到 KataGo 網路 {model}，而且不是這裡會下載的那個檔名，請自行放好。")
            need_model = False
        else:
            plan.append(f"KataGo 網路（{model.name}，{KATAGO_MODEL[1] / 1e6:.0f} MB）")
    if need_pikafish:
        plan.append(f"象棋引擎 Pikafish 2026-09-06（Pikafish.2026-09-06.7z，{PIKAFISH[1] / 1e6:.1f} MB）")
    if need_stockfish:
        plan.append(f"國際象棋引擎 Stockfish 19（stockfish-windows-x86-64-universal.zip，{STOCKFISH[1] / 1e6:.1f} MB）")
    if not plan:
        return 1
    print("缺少下列遊戲引擎，可以從官方網站（GitHub、katagotraining.org）下載：")
    for item in plan:
        print(f"  - {item}")
    try:
        answer = input("要現在下載嗎？(Y/N) ")
    except EOFError:  # 沒有人可以回答（例如輸入被導向空檔案）時當成不下載
        answer = ""
    if answer.strip().lower() not in ("y", "yes"):
        print("沒有下載：圍棋、象棋或國際象棋要等引擎放好才能下，其他遊戲照常可用。之後再執行一次啟動檔就會再問。")
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        try:
            if need_katago:
                install_katago(tmp, katago_exe)
            if need_model:
                print("下載 KataGo 網路...")
                download(*KATAGO_MODEL, tmp / model.name)
                model.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(tmp / model.name), model)
            if need_pikafish:
                install_pikafish(tmp, pikafish_exe)
            if need_stockfish:
                install_stockfish(tmp, stockfish_exe)
        except (OSError, ValueError, subprocess.CalledProcessError, StopIteration, zipfile.BadZipFile) as exc:
            print(f"下載或安裝失敗：{exc}")
            return 1
    check_engines(katago_exe, pikafish_exe, stockfish_exe)
    if need_katago and katago_build() == "opencl":
        print("第一次下圍棋時，KataGo 會先花幾分鐘調校這台電腦的顯示卡，之後就不用再等。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
