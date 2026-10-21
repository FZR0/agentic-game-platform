"""平台上的遊戲清單：key → 遊戲模組。新增遊戲時在這裡登記。"""
from chessgame.module import MODULE as CHESS
from gomoku.module import MODULE as GOMOKU
from holdem.module import MODULE as HOLDEM
from weiqi.module import MODULE as WEIQI
from xiangqi.module import MODULE as XIANGQI

GAMES = {m.key: m for m in (GOMOKU, WEIQI, CHESS, XIANGQI, HOLDEM)}


def get(key):
    """回傳遊戲模組；不存在時丟出 KeyError。"""
    return GAMES[key]
