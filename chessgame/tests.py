import json
import re
from pathlib import Path
from unittest import skipUnless
from unittest.mock import patch

import chess
from django.conf import settings
from django.test import SimpleTestCase, TestCase, override_settings

from arena import agent, narrator
from arena.base import ESTIMATE, RULE, SOLVED
from arena.models import Game

from . import module as chess_module
from . import rules
from .module import MODULE, ChessModule
from .rules import BLACK, WHITE
from .stockfish import StockfishUnavailable

SCHOLAR = ["e2e4", "e7e5", "d1h5", "b8c6", "f1c4", "g8f6"]  # 白棋下一步 Qxf7# 就將死
STALEMATE = ["e2e3", "a7a5", "d1h5", "a8a6", "h5a5", "h7h5", "h2h4", "a6h6", "a5c7", "f7f6", "c7d7", "e8f7",
             "d7b7", "d8d3", "b7b8", "d3h7", "b8c8", "f7g6", "c8e6"]  # 薩姆·洛伊德的 10 步逼和
BOTH_QUEENS = "5k2/8/3q4/8/8/3q4/8/K7 w - - 0 1"  # 白王每走一步都會被一步將死，但有好幾步可走


class FakeStockfish:
    """假的 Stockfish：先挑一步將死，再避開讓對手一步將死的步，然後吃價值高的子，最後照 UCI 排序；
    第 i 名的勝率是 base − step×i（最低 0.02，將死的那一步是 1.0）。step=0、base=0.5 是「平坦」：每一步都一樣好，
    用來測整盤棋的流程，不會因為假引擎的排名而被挑出問題。"""

    def __init__(self, base=0.6, step=0.08):
        self.base, self.step = base, step

    def analyse(self, board, depth, multipv):
        if board.is_game_over():
            return []
        mates = rules.mates_in_one(board)

        def gain(move):
            if not board.is_capture(move):
                return 0
            return rules.PIECE_VALUES[chess.PAWN if board.is_en_passant(move) else board.piece_type_at(move.to_square)]

        ranked = sorted(board.legal_moves, key=lambda m: (m not in mates, bool(rules.allowed_mates(board, m)), -gain(m), m.uci()))
        lines = []
        for i, move in enumerate(ranked[:multipv]):
            is_mate = move in mates
            lines.append({"move": move.uci(), "mate": 1 if is_mate else None, "cp": 0,
                          "winrate": 1.0 if is_mate else max(0.02, self.base - self.step * i), "pv": [move.uci()]})
        return lines


class BrokenStockfish:
    def analyse(self, *args, **kwargs):
        raise StockfishUnavailable("找不到 stockfish.exe")


def module_with(analyzer):
    m = ChessModule()
    m.analyzer = analyzer
    return m


def replay(actions):
    return ChessModule().replay({}, actions)


def state_from(fen):
    return rules.ChessState(chess.Board(fen))


def fake_chat(*replies):
    """假的模型：依序回傳 [(工具名, 參數), ...]。"""
    queue = list(replies)

    def chat(messages, config, tools):
        return {"content": "", "tool_calls": [{"function": {"name": n, "arguments": a}} for n, a in queue.pop(0)]}
    return chat


class RulesTests(SimpleTestCase):
    def test_fools_mate_ends_the_game_and_black_wins(self):
        s = replay(["f2f3", "e7e5", "g2g4", "d8h4"])
        self.assertEqual(rules.result(s)[0], BLACK)
        self.assertIn("將死", rules.result(s)[1])
        with self.assertRaises(ValueError):
            rules.play(s, "a2a3")  # 結束後不能再走

    def test_stalemate_is_a_draw_not_a_win(self):
        # 沒被將軍卻沒有合法走法：和局。最常見的實作錯誤是把它當成輸。
        s = replay(STALEMATE)
        winner, text = rules.result(s)
        self.assertIsNone(winner)
        self.assertIn("逼和", text)
        self.assertEqual(ChessModule().outcome(s), (None, True))

    def test_illegal_moves_are_rejected(self):
        s = replay([])
        for bad in ("e2e5", "e1e2", "e7e5", "a1a3", "e2e4q", "zz"):
            with self.assertRaises(ValueError, msg=bad):
                rules.play(s, bad)

    def test_promotion_must_name_the_piece(self):
        s = state_from("8/P6k/8/8/8/8/8/K7 w - - 0 1")
        with self.assertRaises(ValueError) as caught:
            rules.play(s, "a7a8")
        self.assertIn("升變", str(caught.exception))
        self.assertEqual(rules.play(s, "a7a8q").board.piece_at(chess.A8).piece_type, chess.QUEEN)

    def test_castling_and_en_passant_are_playable(self):
        castled = replay(["e2e4", "a7a6", "g1f3", "a6a5", "f1c4", "a5a4", "e1g1"])
        self.assertEqual(castled.board.piece_at(chess.G1).piece_type, chess.KING)
        ep = replay(["e2e4", "a7a6", "e4e5", "d7d5", "e5d6"])
        self.assertIsNone(ep.board.piece_at(chess.D5))  # 吃過路兵：被吃的兵不在落點上

    def test_threefold_repetition_is_declared_a_draw_at_once(self):
        shuffle = ["g1f3", "g8f6", "f3g1", "f6g8"]
        self.assertIsNone(rules.result(replay(shuffle + shuffle[:3])))  # 第 7 手：起始局面還只出現 2 次
        winner, text = rules.result(replay(shuffle * 2))  # 第 8 手：第 3 次
        self.assertIsNone(winner)
        self.assertIn("三次", text)

    def test_fifty_moves_without_capture_or_pawn_move_is_a_draw(self):
        # 規則面板說五十步內沒有吃子也沒有走兵就和局：第 100 個半步成立時直接判和；走兵會重新計算，不能誤判
        quiet = state_from("4k3/8/8/8/8/8/P7/R3K3 w - - 99 80")
        self.assertIsNone(rules.result(quiet))
        winner, text = rules.result(rules.play(quiet, "a1b1"))
        self.assertIsNone(winner)
        self.assertIn("五十步", text)
        self.assertIsNone(rules.result(rules.play(quiet, "a2a3")))

    def test_resigning_loses_for_the_resigning_side(self):
        s = replay(["e2e4", "resign"])
        self.assertEqual(rules.result(s)[0], WHITE)
        self.assertEqual(ChessModule().outcome(s), (WHITE, False))

    def test_normalize_accepts_common_spellings_but_not_san(self):
        m = ChessModule()
        for text in ("e2e4", "E2E4", "e2-e4", "e2→e4", "g1 f3", "e2xd3", "e7e8Q"):
            self.assertIsNotNone(m.normalize(text), text)
        self.assertEqual(m.normalize("E2-E4"), "e2e4")
        for text in ("Nf3", "O-O", "", None, "e9e4"):
            self.assertIsNone(m.normalize(text), text)
        self.assertEqual(m.normalize("認輸"), "resign")

    def test_saved_games_replay_and_illegal_sequences_do_not(self):
        m = ChessModule()
        self.assertEqual(m.replay({}, SCHOLAR).board.fen().split()[0], "r1bqkb1r/pppp1ppp/2n2n2/4p2Q/2B1P3/8/PPPP1PPP/RNB1K1NR")
        with self.assertRaises(ValueError):
            m.replay({}, ["e2e4", "e2e4"])


class FrameTests(SimpleTestCase):
    def test_board_string_starts_at_a1_and_lists_every_legal_target(self):
        f = ChessModule().frame(replay([]), WHITE)
        self.assertEqual((f["board"][0], f["board"][4], f["board"][60]), ("R", "K", "k"))  # a1、e1、e8
        self.assertEqual(sorted(f["legal"]["e2"]), ["e3", "e4"])
        self.assertEqual(sorted(f["legal"]["g1"]), ["f3", "h3"])
        self.assertEqual((f["to_move"], f["check"], f["result"]), (WHITE, None, None))

    def test_promotion_squares_and_check_are_reported(self):
        f = ChessModule().frame(state_from("8/P6k/8/8/8/8/8/K7 w - - 0 1"), WHITE)
        self.assertEqual(f["promotions"], ["a7a8"])
        f = ChessModule().frame(state_from("4k3/8/8/8/8/8/4r3/4K3 w - - 0 1"), WHITE)
        self.assertEqual(f["check"], "e1")

    def test_finished_game_has_no_moves_and_says_why(self):
        f = ChessModule().frame(replay(["f2f3", "e7e5", "g2g4", "d8h4"]), WHITE)
        self.assertEqual(f["legal"], {})
        self.assertIn("將死", f["result"])
        self.assertEqual(f["check"], "e1")


class HardRuleTests(SimpleTestCase):
    def setUp(self):
        self.m = module_with(FakeStockfish())

    def test_must_take_a_mate_in_one(self):
        s = replay(SCHOLAR)
        self.assertIn("必須走將死", self.m.rejection(s, "g1f3"))
        self.assertIsNone(self.m.rejection(s, "h5f7"))

    def test_must_not_allow_a_mate_in_one_when_there_is_an_alternative(self):
        s = replay(["f2f3", "e7e5"])  # 白棋下 g2g4 會被 Qh4# 將死
        reason = self.m.rejection(s, "g2g4")
        self.assertIn("一步將死", reason)
        # 要給 Agent 一個不會被將死的選擇，而且是引擎排名最前的那一步：隨便一個合法步可能是送子的壞棋
        top = self.m._analysis(s.board)[0]["move"]
        self.assertIn(f"例如 {rules.move_text(s.board, chess.Move.from_uci(top))}", reason)
        self.assertNotEqual(top, next(iter(s.board.legal_moves)).uci())  # 否則這個測試分不出兩種做法
        self.assertIsNone(self.m.rejection(s, "e2e4"))

    def test_allowed_when_every_move_allows_mate(self):
        s = state_from(BOTH_QUEENS)
        legal = [m.uci() for m in s.board.legal_moves]
        self.assertGreater(len(legal), 1)
        for uci in legal:
            self.assertIsNone(self.m.rejection(s, uci), uci)

    def test_illegal_resign_and_bad_format_are_rejected(self):
        s = replay([])
        self.assertIn("UCI", self.m.rejection(s, None))
        self.assertIn("不能認輸", self.m.rejection(s, "resign"))
        self.assertIn("不是合法", self.m.rejection(s, "e2e5"))

    def test_only_one_legal_move_needs_no_model(self):
        s = state_from("k7/8/8/8/8/8/1r6/K7 w - - 0 1")
        forced = self.m.forced(s)
        self.assertEqual(forced["action"], "a1b2")
        self.assertEqual(forced["points"][0][0], RULE)
        self.assertIsNone(self.m.forced(replay([])))


class FactsTests(SimpleTestCase):
    def setUp(self):
        self.m = module_with(FakeStockfish())
        self.s = replay(SCHOLAR)

    def test_threats_report_the_mate_and_the_hanging_pieces_as_rule_facts(self):
        label, points = self.m.run_tool(self.s, "find_threats", {})
        text = "".join(t for _, t in points)
        self.assertIn("一步將死對方：白棋的后 h5→f7（Qxf7#）", text)
        self.assertIn("白棋的后 h5 正被攻擊", text)  # 后被黑馬攻擊且沒有保護
        self.assertTrue(all(ev == RULE for ev, _ in points))

    def test_mate_is_solved_and_estimates_stay_estimates(self):
        _, points = self.m.run_tool(self.s, "list_candidates", {})
        evidence = {ev for ev, _ in points}
        self.assertEqual(evidence, {ESTIMATE, SOLVED})
        solved = [t for ev, t in points if ev == SOLVED][0]
        self.assertIn("在搜尋範圍內已確認", solved)  # 求解確認的用語

    def test_check_move_gives_rule_then_engine_facts(self):
        _, points = self.m.run_tool(self.s, "check_move", {"move": "g1f3"})
        self.assertEqual(points[0][0], RULE)
        self.assertEqual(points[-1][0], ESTIMATE)
        self.assertTrue(any("勝率約" in t for _, t in points))
        _, points = self.m.run_tool(self.s, "check_move", {"move": "h5f7"})
        self.assertTrue(any("將死對方" in t for _, t in points))

    def test_check_move_explains_an_illegal_move_instead_of_failing(self):
        _, points = self.m.run_tool(self.s, "check_move", {"move": "e1e3"})
        self.assertEqual([ev for ev, _ in points], [RULE])
        self.assertIn("不是合法", points[0][1])
        self.assertIsNone(self.m.run_tool(self.s, "check_move", {"move": "Nf3"}))  # 格式不對：當成沒有這個工具
        self.assertIsNone(self.m.run_tool(self.s, "nonsense", {}))

    def test_squares_in_facts_are_found_by_the_coordinate_check(self):
        # 模型提到事實裡沒有的格子要被擋下來；事實裡的格子（用箭頭寫）則可以提
        _, points = self.m.run_tool(self.s, "check_move", {"move": "h5f7"})
        facts = {"points": [{"evidence": ev, "text": t} for ev, t in points]}
        self.assertIsNone(narrator.check_text("后從 h5 吃 f7 就是將死。", facts, self.m.coord_re))
        self.assertIn("a3", narrator.check_text("也可以考慮 a3。", facts, self.m.coord_re))
        self.assertEqual(self.m.coord_re.findall("Nf3 與 exd5 還有 e2→e4"), ["f3", "d5", "e2", "e4"])
        # Agent 提示要模型寫 UCI，理由裡常出現 h5a5 這種寫法：第二格也要檢查，不能只看起點
        self.assertEqual(self.m.coord_re.findall("走 g1f3 或 e7e8q"), ["g1", "f3", "e7", "e8"])
        self.assertIn("a5", narrator.check_text("走 h5a5 比較好。", facts, self.m.coord_re))

    def test_board_text_lists_pieces_and_recent_moves_in_san(self):
        text = self.m.board_text(self.s, SCHOLAR)
        self.assertIn("白棋：王 e1、后 h5", text)
        self.assertIn("最近幾手：1. e4 e5 2. Qh5 Nc6 3. Bc4 Nf6", text)
        self.assertIn("現在輪到你（白棋）下第 7 手", text)

    def test_questions_about_squares_get_facts_about_those_squares(self):
        self.assertEqual(self.m.question_focus("為什麼 F7 很危險？e4 呢？E4 呢"), ["f7", "e4"])
        self.assertEqual(self.m.question_focus("為什麼不走 g1f3？"), ["g1", "f3"])  # UCI 寫法的兩格都要查
        points = self.m.explain_position(self.s, ["f7"], "g1f3")
        text = "".join(p["text"] for p in points)
        self.assertIn("f7 上是黑棋的兵", text)
        self.assertIn("紀錄中實際的下一手", text)

    def test_stockfish_failure_is_reported_not_raised(self):
        m = module_with(BrokenStockfish())
        label, points = m.run_tool(self.s, "list_candidates", {})
        self.assertIn("無法使用", points[0][1])
        points = m.explain_position(self.s, [], None)
        self.assertTrue(any("無法使用" in p["text"] for p in points))


class EngineMoveTests(SimpleTestCase):
    def test_engine_plays_the_top_candidate(self):
        m = module_with(FakeStockfish())
        move = m.engine_move(replay(SCHOLAR))
        self.assertEqual(move["action"], "h5f7")
        self.assertEqual({ev for ev, _ in move["points"]}, {ESTIMATE, SOLVED})

    def test_fallback_without_stockfish_still_takes_a_mate_and_says_it_is_weak(self):
        m = module_with(BrokenStockfish())
        move = m.engine_move(replay(SCHOLAR))
        self.assertEqual(move["action"], "h5f7")
        self.assertIn("品質很低", move["points"][0][1])

    def test_fallback_without_stockfish_is_legal_and_avoids_mate(self):
        m = module_with(BrokenStockfish())
        s = replay(["f2f3", "e7e5"])
        action = m.engine_move(s)["action"]
        self.assertIsNone(m.rejection(s, action))  # 不違反硬性規定（g2g4 會被將死）
        rules.play(s, action)


class AgentTests(SimpleTestCase):
    def test_agent_cannot_skip_a_mate_in_one(self):
        m = module_with(FakeStockfish())
        chat = fake_chat([("play_move", {"move": "g1f3", "reason": "發展馬"})],
                         [("play_move", {"move": "h5→f7", "reason": "將死"})])
        with patch.object(agent, "_chat", chat):
            result = agent.agent_move(m, replay(SCHOLAR), SCHOLAR, steps=4)
        self.assertEqual(result["action"], "h5f7")
        self.assertFalse(result["fallback"])
        self.assertIn("被退回", result["narration"]["text"])  # 思路裡要看得到第一次被退回
        self.assertEqual(result["narration"]["key"], "將死")

    def test_engine_takes_over_when_the_model_is_unreachable(self):
        m = module_with(FakeStockfish())
        with patch.object(agent, "_chat", side_effect=OSError("連不上")):
            result = agent.agent_move(m, replay(SCHOLAR), SCHOLAR, steps=4)
        self.assertTrue(result["fallback"])
        self.assertEqual(result["action"], "h5f7")

    def test_agent_uses_the_tools_and_explains_with_checked_text(self):
        m = module_with(FakeStockfish())
        chat = fake_chat([("find_threats", {})], [("play_move", {"move": "h5f7", "reason": "h5 的后吃 f7 將死，這是一定的"})])
        with patch.object(agent, "_chat", chat):
            result = agent.agent_move(m, replay(SCHOLAR), SCHOLAR, steps=4)
        self.assertEqual(result["facts"]["points"][0]["evidence"], RULE)
        self.assertIn("一步將死", result["facts"]["points"][0]["text"])
        # 理由裡的 h5、f7 都在事實裡，而且事實有規則確認，所以理由原樣保留
        self.assertEqual(result["narration"]["key"], "h5 的后吃 f7 將死，這是一定的")


class ReviewTests(SimpleTestCase):
    def test_missed_mate_in_one_is_a_key_moment_with_rule_evidence(self):
        m = module_with(FakeStockfish(base=0.5, step=0))
        moments = m.review({}, SCHOLAR + ["g1f3"], WHITE)
        self.assertEqual([(x["index"], x["level"], x["pattern"]) for x in moments], [(6, "key", "錯過一步將死")])
        self.assertIn("Qxf7#", moments[0]["title"])
        self.assertEqual(moments[0]["points"][0]["evidence"], RULE)
        self.assertEqual(moments[0]["best"], "h5f7")

    def test_allowing_a_mate_in_one_is_a_key_moment_unless_unavoidable(self):
        m = module_with(FakeStockfish(base=0.5, step=0))
        moments = m.review({}, ["f2f3", "e7e5", "g2g4"], WHITE)
        self.assertEqual([(x["index"], x["level"], x["pattern"]) for x in moments], [(2, "key", "讓對手一步將死")])
        self.assertEqual(moments[0]["points"][0]["evidence"], RULE)
        # 舉的例子就是引擎建議的那一步，玩家不會看到「例如」和「建議」是兩步不同的棋
        board = replay(["f2f3", "e7e5"]).board
        self.assertIn(f"例如 {rules.move_text(board, chess.Move.from_uci(moments[0]['best']))}", moments[0]["points"][0]["text"])
        # 每一步都會被將死的局面，不能因為「讓對手一步將死」怪玩家
        s = state_from(BOTH_QUEENS)
        for move in s.board.legal_moves:
            moment = m._judge(s, move.uci())
            self.assertNotEqual(moment and moment["pattern"], "讓對手一步將死", move.uci())

    def test_win_rate_loss_levels(self):
        # 起始局面：假引擎的前 5 名勝率依序 60/52/44/36/28%
        m = module_with(FakeStockfish())
        levels = {uci: [x["level"] for x in m.review({}, [uci], WHITE)] for uci in ("a2a3", "a2a4", "b1a3", "b1c3", "b2b3")}
        self.assertEqual(levels, {"a2a3": [], "a2a4": ["suggest"], "b1a3": ["minor"], "b1c3": ["key"], "b2b3": ["key"]})

    def test_a_position_that_was_already_lost_is_not_blamed(self):
        # 損失不會超過下之前的勝率：最好的下法也只有 4% 時，再差的一步也不到 5 個百分點，不怪玩家
        self.assertIsNone(module_with(FakeStockfish(base=0.04))._judge(replay([]), "b1c3"))
        self.assertIsNotNone(module_with(FakeStockfish(base=0.30))._judge(replay([]), "b1c3"))

    def test_evidence_types_never_overstate(self):
        # 沒有規則或求解依據、只靠引擎勝率的問題，所有事實都只能是引擎估計
        m = module_with(FakeStockfish())
        moment = m._judge(replay([]), "b1c3")
        self.assertEqual({p["evidence"] for p in moment["points"]}, {ESTIMATE})

    def test_only_the_players_moves_are_judged(self):
        m = module_with(FakeStockfish())
        self.assertEqual(m.review({}, ["a2a3", "b8c6"], WHITE), [])  # a2a3 是第一名；黑棋的 b8c6 不是玩家下的
        self.assertEqual([x["index"] for x in m.review({}, ["a2a3", "b8c6"], BLACK)], [1])  # b8c6 在前五名之外，損失很大

    def test_review_without_stockfish_is_empty_not_an_error(self):
        self.assertEqual(module_with(BrokenStockfish()).review({}, SCHOLAR, WHITE), [])

    def test_engine_playing_both_sides_is_never_flagged(self):
        # 復盤和 AI 用同一個標準：引擎自己對下的棋，復盤不應挑出任何問題
        m = module_with(FakeStockfish())
        state, actions = m.new_state({}), []
        while len(actions) < 40 and not m.outcome(state):
            action = m.engine_move(state)["action"]
            actions.append(action)
            state = m.apply(state, action)
        self.assertEqual(m.review({}, actions, WHITE), [])
        self.assertEqual(m.review({}, actions, BLACK), [])

    def test_overall_points_are_plain_counts(self):
        m = module_with(FakeStockfish())
        points = m.overall_points({}, ["e2e4", "d7d5", "e4d5", "d8d5", "b1c3", "d5a5"], WHITE)
        self.assertEqual(points[0][1], "你一共走了 3 步，其中吃子 1 次、將軍 0 次。")
        self.assertEqual(points[1][1], "你這盤沒有王車易位。")
        self.assertEqual(m.overall_points({}, [], WHITE), [])


class ModuleContractTests(SimpleTestCase):
    def test_benchmark_samples_are_legal_and_leave_a_move_to_make(self):
        m = ChessModule()
        for actions in m.benchmark_samples:
            self.assertFalse(m.outcome(m.replay({}, actions)))

    def test_thresholds_are_marked_provisional_in_the_source(self):
        # 復盤門檻還沒用真實資料校準（使用者要求 2026-10-14 標明）；校準後再改這個測試
        source = Path(chess_module.__file__).read_text(encoding="utf-8")
        self.assertIn("暫定值、尚未校準", source)
        self.assertGreater(chess_module.KEY_LOSS, chess_module.MINOR_LOSS)
        self.assertGreater(chess_module.MINOR_LOSS, chess_module.SUGGEST_LOSS)

    def test_package_does_not_shadow_python_chess(self):
        self.assertEqual(chess.__name__, "chess")  # 專案的 app 叫 chessgame，不能蓋掉套件
        self.assertNotIn("chessgame", Path(chess.__file__).parts)


class BoardScriptTests(SimpleTestCase):
    def test_start_frame_in_board_js_matches_the_server(self):
        # 玩家執白先下時，對局等第一步才建立，第一步要靠 board.js 裡的開局資料標出能走的格子；它必須和伺服器算的一樣
        js = (Path(chess_module.__file__).parent / "static" / "chessgame" / "board.js").read_text(encoding="utf-8")
        start = json.loads(re.search(r"/\*start-frame\*/(.*?)/\*end-start-frame\*/", js).group(1))
        server = MODULE.frame(MODULE.new_state({}), WHITE)
        self.assertEqual({k: v for k, v in start.items() if k != "legal"}, {k: v for k, v in server.items() if k != "legal"})
        self.assertEqual({k: sorted(v) for k, v in start["legal"].items()}, {k: sorted(v) for k, v in server["legal"].items()})


@override_settings(OLLAMA_URL="http://127.0.0.1:9", OLLAMA_TIMEOUT=2)
class GameApiTests(TestCase):
    def setUp(self):
        patchers = [patch.object(MODULE, "analyzer", FakeStockfish()),
                    # 不連 Ollama：直接讓呼叫模型失敗，由（假的）引擎代下，測試才不會每一手都等連線被拒
                    patch.object(agent, "_chat", side_effect=OSError("測試不連 Ollama"))]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        MODULE._cache.clear()
        self.addCleanup(MODULE._cache.clear)

    def post(self, url, body):
        return self.client.post(url, json.dumps(body), content_type="application/json")

    def test_moves_are_validated_and_saved_in_uci(self):
        game_id = self.post("/api/chess/games/", {"seat": WHITE}).json()["game"]["id"]
        r = self.post(f"/api/chess/games/{game_id}/move/", {"action": "E2-E4"})
        self.assertEqual(r.status_code, 200)
        saved = Game.objects.get(pk=game_id)
        self.assertEqual(saved.actions[0], "e2e4")
        self.assertEqual(len(saved.actions), 2)  # 引擎代下了黑棋的一步
        MODULE.replay(saved.settings, saved.actions)
        self.assertEqual(r.json()["ai_moves"][0]["frame"]["to_move"], WHITE)

    def test_illegal_move_is_rejected_and_not_saved(self):
        game = Game.objects.create(game_type="chess", human_seat=WHITE, settings={}, actions=["e2e4", "e7e5"])
        r = self.post(f"/api/chess/games/{game.id}/move/", {"action": "e4e5"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("不是合法", r.json()["error"])
        game.refresh_from_db()
        self.assertEqual(game.actions, ["e2e4", "e7e5"])

    def test_playing_black_lets_the_ai_open(self):
        r = self.post("/api/chess/games/", {"seat": BLACK}).json()
        self.assertEqual(len(r["ai_moves"]), 1)
        self.assertEqual(r["ai_moves"][0]["frame"]["to_move"], BLACK)

    def test_checkmate_ends_the_game(self):
        game = Game.objects.create(game_type="chess", human_seat=WHITE, settings={}, actions=SCHOLAR)
        r = self.post(f"/api/chess/games/{game.id}/move/", {"action": "h5f7"}).json()
        self.assertEqual((r["winner"], r["ai_moves"]), (WHITE, []))
        self.assertIn("將死", r["frame"]["result"])
        self.assertTrue(Game.objects.get(pk=game.id).finished)

    def test_review_lists_the_missed_mate(self):
        game = Game.objects.create(game_type="chess", human_seat=WHITE, settings={}, actions=SCHOLAR + ["g1f3"])
        r = self.client.get(f"/api/chess/games/{game.id}/review/").json()
        self.assertIn("錯過一步將死", [m["pattern"] for m in r["moments"]])
        self.assertEqual(r["overall"]["kind"], "overall")

    def test_play_page_loads_the_chess_board(self):
        html = self.client.get("/chess/").content.decode()
        self.assertIn("chessgame/board.js", html)


@skipUnless(Path(settings.STOCKFISH["exe"]).exists(), "沒有安裝 Stockfish（tools/stockfish/）")
class StockfishIntegrationTests(SimpleTestCase):
    def test_real_engine_finds_the_mate_and_reads_the_side_to_move_correctly(self):
        m = ChessModule()
        s = replay(SCHOLAR)
        (est, _), (solved, text) = m.run_tool(s, "list_candidates", {})[1]
        self.assertEqual((est, solved), (ESTIMATE, SOLVED))
        self.assertIn("Qxf7#", text)
        self.assertEqual(m.engine_move(s)["action"], "h5f7")
        # 黑棋少一個后：輪到黑棋時，勝率要以黑棋的觀點算，所以很低
        down_a_queen = state_from("rnb1kbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR b KQkq - 0 1")
        self.assertLess(m._analysis(down_a_queen.board)[0]["winrate"], 0.1)

    def test_same_position_gives_the_same_answer_every_time(self):
        # 單執行緒＋每次清空雜湊表：復盤才會和 AI 當時的選擇一致
        m = ChessModule()
        board = replay(["e2e4", "e7e5", "g1f3", "b8c6"]).board
        first = m._analysis(board)
        m._cache.clear()
        self.assertEqual(first, m._analysis(board))

    def test_engine_playing_both_sides_is_never_flagged_by_the_real_review(self):
        m = ChessModule()
        state, actions = m.new_state({}), []
        for _ in range(16):
            action = m.engine_move(state)["action"]
            actions.append(action)
            state = m.apply(state, action)
        m._cache.clear()  # 復盤要自己重新分析，不能靠對局時留下的快取
        self.assertEqual(m.review({}, actions, WHITE), [])
        self.assertEqual(m.review({}, actions, BLACK), [])
