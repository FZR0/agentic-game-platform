"""Pikafish（UCI）的包裝：第一次使用時啟動，之後一直開著；同時只送一個查詢。

只用 1 個執行緒、每次查詢前清掉置換表，再用固定深度搜尋，所以同一個局面每次結果都一樣。
復盤要和 AI 下棋用同一個標準（引擎自己對下的棋不能被挑出問題），靠的就是這一點。
分數一律是「輪到的一方」的觀點：score 是百分之一個兵（cp），mate 是幾回合內將死（負數代表會被將死）。
"""
import atexit
import subprocess
import threading
from pathlib import Path

from django.conf import settings


class PikafishUnavailable(Exception):
    """Pikafish 沒有安裝、無法啟動或回傳錯誤。"""


class Pikafish:
    def __init__(self):
        self.proc = None
        self.lock = threading.Lock()

    def _send(self, *commands):
        for c in commands:
            self.proc.stdin.write(c + "\n")
        self.proc.stdin.flush()

    def _until(self, prefix):
        lines = []
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise PikafishUnavailable("Pikafish 意外結束")
            line = line.strip()
            lines.append(line)
            if line.startswith(prefix):
                return lines

    def _start(self):
        exe = Path(settings.PIKAFISH_EXE)
        if not exe.exists():
            raise PikafishUnavailable(f"找不到 {exe}")
        try:
            # 權重檔 pikafish.nnue 放在執行檔旁邊，Pikafish 會自己從那裡載入
            self.proc = subprocess.Popen([str(exe)], cwd=exe.parent, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1)
        except OSError as exc:
            raise PikafishUnavailable(f"無法啟動 Pikafish：{exc}")
        atexit.register(self.close)
        self._send("uci")
        self._until("uciok")
        self._send("setoption name Threads value 1", "setoption name Hash value 32", "isready")
        self._until("readyok")

    def close(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def analyze(self, actions, depth, multipv=1, searchmoves=None):
        """分析從開局走完 actions 之後的局面。回傳依好壞排序的 [{"move", "score", "mate", "pv"}]；沒有合法步時回傳空清單。"""
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                self._start()
            try:
                self._send("ucinewgame", f"setoption name MultiPV value {multipv}", "isready")
                self._until("readyok")
                moves = f" moves {' '.join(actions)}" if actions else ""
                go = f"go depth {depth}" + (f" searchmoves {' '.join(searchmoves)}" if searchmoves else "")
                self._send(f"position startpos{moves}", go)
                lines = self._until("bestmove")
            except OSError as exc:
                self.close()
                raise PikafishUnavailable(f"和 Pikafish 溝通失敗：{exc}")
        best = {}
        for line in lines:
            words = line.split()
            if not words or words[0] != "info" or "pv" not in words or "score" not in words \
                    or "lowerbound" in words or "upperbound" in words:
                continue
            k = int(words[words.index("multipv") + 1]) if "multipv" in words else 1
            i = words.index("score")
            kind, value = words[i + 1], int(words[i + 2])
            pv = words[words.index("pv") + 1:]
            best[k] = {"move": pv[0], "score": value if kind == "cp" else None,
                       "mate": value if kind == "mate" else None, "pv": pv}
        return [best[k] for k in sorted(best)]


def value(entry):
    """可以比大小的分數：將死換成很大的數，越快將死越大。"""
    if entry["mate"] is not None:
        return (30000 - abs(entry["mate"])) * (1 if entry["mate"] > 0 else -1)
    return entry["score"]


ENGINE = Pikafish()
