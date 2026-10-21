"""KataGo 分析引擎（JSON 介面）的包裝：第一次使用時啟動，之後一直開著；同時只送一個查詢。

勝率與目差一律換算成「黑棋的觀點」回傳，由呼叫端依需要轉成行棋方的觀點。
"""
import atexit
import itertools
import json
import subprocess
import threading
from pathlib import Path

from django.conf import settings

# 和 weiqi/rules.py 相同的規則：局面超級劫、數子、禁止自殺、不允許「友善虛手」（盤上的子一律算活）
RULES = {"ko": "POSITIONAL", "scoring": "AREA", "tax": "NONE", "suicide": False,
         "hasButton": False, "whiteHandicapBonus": "0", "friendlyPassOk": False}
OVERRIDES = ("numAnalysisThreads=2,numSearchThreadsPerAnalysisThread=4,nnMaxBatchSize=8,"
             "reportAnalysisWinratesAs=BLACK,logToStderr=false,logDir=analysis_logs")


class KataGoUnavailable(Exception):
    """KataGo 沒有安裝、無法啟動或回傳錯誤。"""


class KataGo:
    def __init__(self):
        self.proc = None
        self.lock = threading.Lock()
        self.ids = itertools.count()

    def _start(self):
        cfg = settings.KATAGO
        exe, model, config = Path(cfg["exe"]), Path(cfg["model"]), Path(cfg["config"])
        for path in (exe, model, config):
            if not path.exists():
                raise KataGoUnavailable(f"找不到 {path}")
        try:
            self.proc = subprocess.Popen(
                [str(exe), "analysis", "-model", str(model), "-config", str(config), "-override-config", OVERRIDES],
                cwd=exe.parent, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", bufsize=1)
        except OSError as exc:
            raise KataGoUnavailable(f"無法啟動 KataGo：{exc}")
        atexit.register(self.close)

    def close(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def analyze(self, size, komi, actions, turns, visits, ownership=False):
        """分析 actions 序列中的第 turns 步（0 = 開局）。回傳 {turn: {"root": {...}, "moves": [...], "ownership"}}。

        moves 依 KataGo 的排序（最好的在前），每個含 move、winrate、scoreLead（黑棋觀點）、visits、pv。
        """
        moves = [["B" if i % 2 == 0 else "W", a] for i, a in enumerate(actions) if a != "resign"]
        query = {"id": str(next(self.ids)), "moves": moves, "rules": RULES, "komi": komi,
                 "boardXSize": size, "boardYSize": size, "analyzeTurns": list(turns),
                 "maxVisits": visits, "includeOwnership": ownership}
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                self._start()
            try:
                self.proc.stdin.write(json.dumps(query) + "\n")
                self.proc.stdin.flush()
                results = {}
                while len(results) < len(query["analyzeTurns"]):
                    line = self.proc.stdout.readline()
                    if not line:
                        raise KataGoUnavailable("KataGo 意外結束")
                    data = json.loads(line)
                    if "error" in data:
                        raise KataGoUnavailable(f"KataGo 回報錯誤：{data['error']}")
                    if data.get("id") != query["id"] or "turnNumber" not in data:
                        continue  # 警告訊息或其他查詢的殘留
                    results[data["turnNumber"]] = {"root": data["rootInfo"], "moves": data["moveInfos"],
                                                   "ownership": data.get("ownership")}
                return results
            except (OSError, ValueError) as exc:
                self.close()
                raise KataGoUnavailable(f"和 KataGo 溝通失敗：{exc}")


ENGINE = KataGo()
