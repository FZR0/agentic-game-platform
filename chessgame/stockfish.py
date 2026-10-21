"""Stockfish（UCI）的包裝：第一次使用時啟動，之後一直開著；同時只送一個查詢。

分析一律回傳「輪到的一方」的觀點。每一條候選線是
{"move": UCI, "mate": 正數＝輪到的一方 N 步內將死、負數＝被將死、沒有就是 None, "cp": 兵值百分之一, "winrate": 0–1, "pv": [UCI, ...]}。
"""
import threading
from pathlib import Path

import chess
import chess.engine
from django.conf import settings

SAFETY_SECONDS = 30  # 一次查詢的時間上限；固定深度正常只要零點幾秒，這只是怕引擎卡住


class StockfishUnavailable(Exception):
    """Stockfish 沒有安裝、無法啟動或回傳錯誤。"""


def winrate_from_cp(cp):
    """引擎沒有回報勝負和機率時的後備換算（常見的 Elo 式曲線）。"""
    return 1 / (1 + 10 ** (-cp / 400))


class Stockfish:
    def __init__(self):
        self.engine = None
        self.lock = threading.Lock()

    def _start(self):
        exe = Path(settings.STOCKFISH["exe"])
        if not exe.exists():
            raise StockfishUnavailable(f"找不到 {exe}")
        try:
            self.engine = chess.engine.SimpleEngine.popen_uci(str(exe), cwd=exe.parent)
            # 單執行緒＋每次查詢前清空雜湊表（見 analyse 的 game 參數）：同一個局面每次算出同樣的結果，
            # 復盤才會和 AI 當時的選擇一致
            self.engine.configure({"Threads": 1, "Hash": settings.STOCKFISH["hash_mb"], "UCI_ShowWDL": True})
        except (OSError, chess.engine.EngineError) as exc:
            self.engine = None
            raise StockfishUnavailable(f"無法啟動 Stockfish：{exc}")
        # python-chess 的背景執行緒不是 daemon，Python 結束時會一直等它，而 atexit 要等執行緒都結束後才執行，
        # 所以不能用 atexit：改用 daemon 執行緒等主執行緒結束，再關掉引擎（不關的話整個程序，包括伺服器，都結束不了）
        threading.Thread(target=self._close_when_main_ends, daemon=True).start()

    def _close_when_main_ends(self):
        threading.main_thread().join()
        self.close()

    def close(self):
        if self.engine:
            try:
                self.engine.quit()
            except Exception:  # 結束時引擎可能已經不在了
                pass
            self.engine = None

    def analyse(self, board, depth, multipv):
        """分析 board 的局面，回傳最多 multipv 條候選線（好的在前）。已經分出勝負的局面回傳空清單。"""
        if board.is_game_over():
            return []
        with self.lock:
            if self.engine is None:
                self._start()
            try:
                # game 每次不同，python-chess 就會送 ucinewgame，引擎清空雜湊表
                infos = self.engine.analyse(board, chess.engine.Limit(depth=depth, time=SAFETY_SECONDS),
                                            multipv=multipv, game=object())
            except (OSError, TimeoutError, chess.engine.EngineError, chess.engine.EngineTerminatedError) as exc:
                self.close()
                raise StockfishUnavailable(f"和 Stockfish 溝通失敗：{exc}")
        lines = []
        for info in infos:
            if not info.get("pv"):
                continue
            score = info["score"].pov(board.turn)
            wdl = info.get("wdl")
            cp = score.score(mate_score=100000)
            lines.append({"move": info["pv"][0].uci(), "mate": score.mate(), "cp": cp,
                          "winrate": wdl.pov(board.turn).expectation() if wdl else winrate_from_cp(cp),
                          "pv": [m.uci() for m in info["pv"]]})
        return lines


ENGINE = Stockfish()
