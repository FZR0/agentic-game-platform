"""國際象棋規則：包 python-chess 的 Board，加上本專案需要的終局判定與窮舉式戰術查詢（一步將死、懸空的子）。

行動記法：UCI（起點格加終點格，例如 "e2e4"；升變多一個小寫字母 "e7e8q"；王車易位寫王的走法 "e1g1"）與 "resign"。
座位 1＝白棋（先手）、2＝黑棋。這裡的事實都屬於規則確認：python-chess 負責走法與終局，這個檔案只做窮舉。
"""
import re

import chess

WHITE, BLACK = 1, 2
RESIGN = "resign"
COLOR_NAMES = {WHITE: "白棋", BLACK: "黑棋"}
PIECE_NAMES = {chess.PAWN: "兵", chess.KNIGHT: "馬", chess.BISHOP: "象", chess.ROOK: "車", chess.QUEEN: "后", chess.KING: "王"}
PIECE_VALUES = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 100}
UCI_RE = re.compile(r"^[a-h][1-8][a-h][1-8][qrbn]?$")

TERMINATIONS = {
    chess.Termination.CHECKMATE: "將死",
    chess.Termination.STALEMATE: "逼和（輪到的一方沒有合法走法，但沒有被將軍）",
    chess.Termination.INSUFFICIENT_MATERIAL: "雙方子力都不足以將死對方",
    chess.Termination.SEVENTYFIVE_MOVES: "七十五步內沒有吃子或走兵",
    chess.Termination.FIVEFOLD_REPETITION: "同一局面出現五次",
    chess.Termination.FIFTY_MOVES: "五十步內沒有吃子或走兵",
    chess.Termination.THREEFOLD_REPETITION: "同一局面出現三次",
}


def seat_of(color):
    return WHITE if color == chess.WHITE else BLACK


def other(seat):
    return BLACK if seat == WHITE else WHITE


class ChessState:
    """board 帶著整串走法（才判得出重複局面）；actions 是存檔用的行動字串；resigned 是認輸的座位。"""

    def __init__(self, board=None, actions=(), resigned=None):
        self.board = board or chess.Board()
        self.actions = list(actions)
        self.resigned = resigned

    @property
    def to_move(self):
        return seat_of(self.board.turn)


def normalize(text):
    """把文字轉成標準記法（小寫 UCI 或 resign）；格式不對時回傳 None。接受 "e2-e4"、"e2→e4"、"e2xd3"、"E2E4"。"""
    t = str(text or "").strip().lower()
    if t in (RESIGN, "認輸"):
        return RESIGN
    t = re.sub(r"[\s\-→x:]", "", t)
    return t if UCI_RE.match(t) else None


def play(state, action):
    """執行行動，回傳新的狀態（不修改原狀態）；不合法時丟出 ValueError。"""
    if result(state):
        raise ValueError("對局已經結束")
    if action == RESIGN:
        return ChessState(state.board, state.actions + [action], resigned=state.to_move)
    if not UCI_RE.match(action or ""):
        raise ValueError(f"走法格式不對：{action}")
    move = chess.Move.from_uci(action)
    if move not in state.board.legal_moves:
        promotable = len(action) == 4 and chess.Move.from_uci(action + "q") in state.board.legal_moves
        hint = "；兵走到最後一排要指定升變成什麼（例如 e7e8q）" if promotable else ""
        raise ValueError(f"{action} 不是合法的走法{hint}")
    board = state.board.copy()
    board.push(move)
    return ChessState(board, state.actions + [action])


def result(state):
    """尚未結束時回傳 None；結束時回傳 (獲勝座位或 None, 說明文字)。
    三次重複與五十步規則在成立的當下直接判和（不做「宣告和棋」的按鈕）。
    不用 python-chess 的 outcome(claim_draw=True)：它在「再走一步就會重複三次」時就先判和了，比規則早一手。"""
    if state.resigned:
        return other(state.resigned), f"{COLOR_NAMES[state.resigned]}認輸"
    board = state.board
    outcome = board.outcome()  # 將死、逼和、子力不足、七十五步、五次重複
    if outcome is None and board.is_repetition(3):
        outcome = chess.Outcome(chess.Termination.THREEFOLD_REPETITION, None)
    elif outcome is None and board.halfmove_clock >= 100:
        outcome = chess.Outcome(chess.Termination.FIFTY_MOVES, None)
    if outcome is None:
        return None
    winner = None if outcome.winner is None else seat_of(outcome.winner)
    reason = TERMINATIONS.get(outcome.termination, outcome.termination.name)
    return winner, (f"{COLOR_NAMES[winner]}將死對方" if winner else f"和局：{reason}")


# ---- 窮舉式查詢（規則確認） ----

def piece_text(board, square):
    """例如「白棋的馬 c3」。"""
    piece = board.piece_at(square)
    return f"{COLOR_NAMES[seat_of(piece.color)]}的{PIECE_NAMES[piece.piece_type]} {chess.square_name(square)}"


def move_text(board, move):
    """例如「白棋的馬 g1→f3（Nf3）」；用箭頭寫格子，說明檢查才抓得到座標。"""
    piece = board.piece_at(move.from_square)
    promo = f"=升變成{PIECE_NAMES[move.promotion]}" if move.promotion else ""
    return (f"{COLOR_NAMES[seat_of(piece.color)]}的{PIECE_NAMES[piece.piece_type]} "
            f"{chess.square_name(move.from_square)}→{chess.square_name(move.to_square)}{promo}（{board.san(move)}）")


def mates_in_one(board):
    """輪到的一方現在能一步將死的所有走法。"""
    found = []
    for move in list(board.legal_moves):
        board.push(move)
        if board.is_checkmate():
            found.append(move)
        board.pop()
    return found


def allowed_mates(board, move):
    """下了 move 之後，對手能一步將死的所有走法（空清單＝不會被一步將死）。"""
    board.push(move)
    try:
        return mates_in_one(board)
    finally:
        board.pop()


def threatened_mates(board):
    """輪到的一方如果什麼都不做（假設對手再走一步），對手能一步將死的走法；被將軍時沒有這個概念，回傳空清單。"""
    if board.is_check():
        return []
    board.push(chess.Move.null())
    try:
        return mates_in_one(board)
    finally:
        board.pop()


def weak_pieces(board, color):
    """color 一方被攻擊、而且沒有保護或被價值較低的子攻擊的子（王不算）。回傳 [(格子, 攻擊者格子清單, 有沒有保護)]，價值高的在前。"""
    opp = not color
    weak = []
    for square, piece in board.piece_map().items():
        if piece.color != color or piece.piece_type == chess.KING:
            continue
        attackers = list(board.attackers(opp, square))
        if not attackers:
            continue
        defended = bool(board.attackers(color, square))
        cheapest = min(PIECE_VALUES[board.piece_type_at(a)] for a in attackers)
        if not defended or cheapest < PIECE_VALUES[piece.piece_type]:
            weak.append((square, attackers, defended))
    weak.sort(key=lambda w: -PIECE_VALUES[board.piece_type_at(w[0])])
    return weak
