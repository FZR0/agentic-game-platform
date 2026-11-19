import json
import re
from pathlib import Path
from unittest import skipUnless
from unittest.mock import patch

from django.conf import settings
from django.test import SimpleTestCase, TestCase, override_settings

from arena.tests import AdminClient
from arena import agent
from arena import tests as arena_tests
from arena.base import ESTIMATE, RULE, SOLVED
from arena.models import Game

from . import rules
from .module import MODULE, XiangqiModule
from .pikafish import PikafishUnavailable
from .rules import BLACK, RED


def replay(actions):
    return MODULE.replay({}, actions)


def position(pieces, to_move=RED):
    """自訂局面：{"e0": "K", ...}。只給規則與假的分析器用（真的 Pikafish 一律從開局重播）。"""
    s = rules.XiangqiState()
    s.board = ["."] * 90
    for name, p in pieces.items():
        f, r = rules.parse_square(name)
        s.board[r * 9 + f] = p
    s.to_move = to_move
    s.keys = [s.key()]
    return s


class FakePikafish:
    """假的 Pikafish：best {第幾步: 首選}（預設第一個合法步）、scores {第幾步: 首選的分數}、
    played {第幾步: 指定那一步（searchmoves）的分數}。分數是行棋方觀點的 cp，或 ("mate", n)。"""

    def __init__(self, best=None, scores=None, played=None):
        self.best, self.scores, self.played = best or {}, scores or {}, played or {}

    @staticmethod
    def entry(move, v):
        mate = v[1] if isinstance(v, tuple) else None
        return {"move": move, "score": None if mate is not None else v, "mate": mate, "pv": [move]}

    def analyze(self, actions, depth, multipv=1, searchmoves=None):
        t = len(actions)
        legal = sorted(rules.legal_moves(replay(actions)))
        if searchmoves:
            return [self.entry(searchmoves[0], self.played.get(t, self.scores.get(t, 0)))]
        best = self.best.get(t, legal[0])
        return [self.entry(best, self.scores.get(t, 0))] + [self.entry(m, 0) for m in legal if m != best][:multipv - 1]


class MultiPvPikafish(FakePikafish):
    """真的 Pikafish 在同一深度下，MultiPV 1 和 MultiPV 5 的第一名可能不同（2026-11-16 原開發機 43 個局面有 12 個不同）。
    這個假的分析器重現這件事：只要一條線時首選是 one，要五條時第一名是 five。"""

    def __init__(self, one, five, played):
        super().__init__(best={0: five}, scores={0: 40}, played={0: played})
        self.one = one

    def analyze(self, actions, depth, multipv=1, searchmoves=None):
        if multipv == 1 and not searchmoves:
            return [self.entry(self.one, 60)]
        return super().analyze(actions, depth, multipv, searchmoves)


class BrokenPikafish:
    def analyze(self, *args, **kwargs):
        raise PikafishUnavailable("找不到 pikafish.exe")


def module_with(analyzer):
    m = XiangqiModule()
    m.analyzer = analyzer
    return m


def fake_chat(*replies):
    """假的模型：依序回傳 [(工具名, 參數), ...]。"""
    queue = list(replies)

    def chat(messages, config, tools):
        return {"content": "", "tool_calls": [{"function": {"name": n, "arguments": a}} for n, a in queue.pop(0)]}
    return chat


# 紅方一步殺：俥 b0b9 沿第 9 列將軍，第 8 列被另一隻俥封住，d9 會和帥照面
MATE_IN_ONE = {"d0": "K", "a8": "R", "b0": "R", "e9": "k"}
# 黑方威脅一步殺（車 b9b0）；紅方吃掉 a1 的車就能解（帥可以上 e1）。f6 的卒擋住紅俥從 f 線將軍
BLACK_THREAT = {"e0": "K", "a4": "R", "i3": "P", "f9": "k", "f6": "p", "a1": "r", "b9": "r"}


class RuleTests(SimpleTestCase):
    def test_perft_matches_the_known_counts(self):
        # 初始局面 1–3 層的走法數是公認的數字；走法產生器任何一個錯（蹩馬腿、塞象眼、炮架、照面）都會讓數字不對
        s = rules.XiangqiState()
        self.assertEqual([rules.perft(s, d) for d in (1, 2, 3)], [44, 1920, 79666])

    def test_piece_movement_limits(self):
        legal = rules.legal_moves(rules.XiangqiState())
        self.assertNotIn("b0d1", legal)  # 蹩馬腿：c0 有相
        self.assertIn("h2h9", legal)     # 炮隔著 h7 的砲吃 h9 的馬
        self.assertNotIn("h2h7", legal)  # 炮吃子一定要有炮架
        self.assertNotIn("d0c1", legal)  # 仕不能出九宮
        s = position({"e0": "K", "c4": "B", "d9": "k"})
        self.assertNotIn("c4e6", rules.legal_moves(s))  # 相不能過河
        s = position({"e0": "K", "c4": "B", "d3": "P", "d9": "k"})
        self.assertNotIn("c4e2", rules.legal_moves(s))  # 塞象眼

    def test_generals_may_not_face_each_other(self):
        s = position({"e0": "K", "e5": "R", "e9": "k"})
        with self.assertRaisesRegex(ValueError, "照面"):
            rules.play(s, "e5a5")

    def test_checkmate_and_stalemate_both_lose(self):
        s = rules.play(position(MATE_IN_ONE), "b0b9")
        self.assertEqual(s.result, (RED, "黑方被將死，紅方獲勝"))
        # 困斃：黑將沒有被將軍，但沒有任何一步可以走，照樣判負
        s = rules.play(position({"d0": "K", "f0": "R", "a7": "R", "e9": "k"}), "a7a8")
        self.assertEqual(s.result, (RED, "黑方被困斃（沒有合法步可走），紅方獲勝"))

    def test_perpetual_check_loses_but_plain_repetition_is_a_draw(self):
        # 長將判負：紅俥一直將軍、黑將一直躲，同一局面第三次出現時判一直將軍的紅方輸
        s = position({"d0": "K", "a8": "R", "e9": "k"})
        for move in ["a8a9", "e9e8", "a9a8", "e8e9"] * 2:
            s = rules.play(s, move)
        self.assertEqual(s.result[0], BLACK)
        self.assertIn("紅方長將", s.result[1])
        # 雙方都沒有將軍的重複：判和（長捉還沒有判定）
        s = position({"d0": "K", "e9": "k"})
        for move in ["d0d1", "e9e8", "d1d0", "e8e9"] * 2:
            s = rules.play(s, move)
        self.assertEqual(s.result, (None, "同一局面重複三次，判和"))
        self.assertEqual(MODULE.outcome(s), (None, True))

    def test_chinese_notation(self):
        s = rules.XiangqiState()
        self.assertEqual([rules.chinese(s, m) for m in ("h2e2", "b0c2", "g3g4", "c0e2", "d0e1")],
                         ["炮二平五", "傌八進七", "兵三進一", "相七進五", "仕六進五"])
        s = rules.play(s, "h2e2")  # 黑方由黑方的右邊數起，用１到９
        self.assertEqual([rules.chinese(s, m) for m in ("h7e7", "b9c7")], ["砲８平５", "馬２進３"])
        s = position({"d0": "K", "e3": "R", "e5": "R", "f9": "k"})  # 同一直線兩隻俥：用前後
        self.assertEqual([rules.chinese(s, m) for m in ("e5e7", "e3e4")], ["前俥進二", "後俥進一"])

    def test_chinese_notation_is_understood_in_any_common_writing(self):
        s = rules.play(rules.XiangqiState(), "h2e2")
        for text in ("砲８平５", "炮8平5", "包８平５"):
            self.assertEqual(rules.from_chinese(s, text), "h7e7")
        self.assertIsNone(rules.from_chinese(s, "車九平八"))  # 輪到黑方，沒有這一步


class ModuleTests(SimpleTestCase):
    def test_normalize(self):
        self.assertEqual([MODULE.normalize(t) for t in ("H2E2", " h2e2 ", "認輸", "j2e2", "h2")],
                         ["h2e2", "h2e2", "resign", None, None])

    def test_frame_gives_the_legal_moves_so_the_browser_never_computes_rules(self):
        f = MODULE.frame(replay(["h2e2"]), RED)
        self.assertEqual((len(f["board"]), f["last"], f["to_move"], f["check"]), (90, "h2e2", BLACK, False))
        self.assertEqual(set(f["legal"]), set(rules.legal_moves(replay(["h2e2"]))))

    def test_board_script_starts_from_the_same_position_as_the_server(self):
        # 玩家執紅時第一步在對局建立前就要能點，所以 board.js 內建開局局面與合法步；不能和伺服器不一樣
        js = (Path(__file__).parent / "static" / "xiangqi" / "board.js").read_text(encoding="utf-8")
        start = re.search(r'const START = "([^"]+)"', js).group(1)
        expr = re.search(r"const START_LEGAL = \((.*?)\)\.split", js, re.S).group(1)
        legal = "".join(re.findall(r'"([^"]*)"', expr))
        f = MODULE.frame(rules.XiangqiState(), RED)
        self.assertEqual(start, f["board"])
        self.assertEqual(sorted(legal.split()), sorted(f["legal"]))

    def test_agent_must_take_a_win_in_one(self):
        m = module_with(FakePikafish())
        s = position(MATE_IN_ONE)
        self.assertIn("一步就能獲勝", m.rejection(s, "a8a7"))
        self.assertIsNone(m.rejection(s, "b0b9"))

    def test_agent_may_not_allow_a_win_in_one_when_it_can_be_avoided(self):
        m = module_with(FakePikafish())
        s = position(BLACK_THREAT)
        self.assertIn("對方一步就能獲勝", m.rejection(s, "i3i4"))
        self.assertIsNone(m.rejection(s, "a4a1"))
        self.assertIn("不能認輸", m.rejection(s, "resign"))
        self.assertIn("不能這樣走", m.rejection(s, "i3i5"))

    def test_threats_name_the_side_for_every_fact(self):
        # 同一個威脅對兩方意義不同；事實要寫清楚是哪一方，否則語言模型會把雙方搞混
        texts = [t for _, t in MODULE.run_tool(position(BLACK_THREAT), "find_threats", {})[1]]
        self.assertTrue(any("如果紅方不處理，黑方下一步走" in t and "b9b0" in t for t in texts), texts)
        self.assertTrue(any("黑方車（a1）受到紅方俥（a4）攻擊，沒有保護（紅方現在可以吃掉）" in t for t in texts), texts)
        texts = [t for _, t in MODULE.run_tool(position(MATE_IN_ONE), "find_threats", {})[1]]
        self.assertTrue(any(t.startswith("紅方一步就能獲勝") and "b0b9" in t for t in texts), texts)

    def test_engine_mate_is_solver_evidence_and_scores_are_estimates(self):
        m = module_with(FakePikafish(best={0: "h2e2"}, scores={0: ("mate", 3)}))
        (evidence, text), = m.run_tool(rules.XiangqiState(), "list_candidates", {})[1]
        self.assertEqual(evidence, SOLVED)
        self.assertIn("在搜尋範圍內已確認", text)
        m = module_with(FakePikafish(best={0: "h2e2"}, scores={0: 30}))
        (evidence, text), = m.run_tool(rules.XiangqiState(), "list_candidates", {})[1]
        self.assertEqual(evidence, ESTIMATE)
        self.assertIn("1. 炮二平五（h2e2）（約領先 0.3 個兵）", text)

    def test_candidates_first_choice_engine_move_and_review_name_the_same_move(self):
        # 復盤與下棋用同一個標準，給模型和玩家的事實也不能互相矛盾。同一深度下 MultiPV 1 和 5 的第一名可能不同；
        # 以前候選清單用 MultiPV 5、其他用 MultiPV 1，2026-11-16 情境測試裡問題檢查建議兵三進一，
        # 「問 AI」的候選清單第一名卻是別的步，模型照清單回答。所以四者都要是同一次分析的第一名
        m = module_with(MultiPvPikafish(one="c3c4", five="g3g4", played=-100))
        s = rules.XiangqiState()
        (_, candidates), = m.run_tool(s, "list_candidates", {})[1]
        self.assertIn("1. 兵三進一（g3g4）", candidates)
        checked = [t for _, t in m.run_tool(s, "check_move", {"move": "h2e2"})[1]]
        self.assertIn("Pikafish 的首選是 兵三進一（g3g4）。", checked)
        self.assertEqual(m.engine_move(s)["action"], "g3g4")
        self.assertIsNone(m._review_move(s, 0, "g3g4"))  # 照候選清單第一名走，復盤不會說是失誤
        self.assertEqual(m._review_move(s, 0, "c3c4")["best"], "g3g4")  # 復盤建議的也是那一步

    def test_only_legal_move_needs_no_model(self):
        s = position({"d0": "K", "a8": "R", "e9": "k"})
        s = rules.play(s, "a8a9")  # 黑將被將軍，只能上 e8
        self.assertEqual(MODULE.forced(s)["action"], "e9e8")
        self.assertIsNone(MODULE.forced(rules.XiangqiState()))

    def test_engine_without_pikafish_still_wins_or_avoids_losing(self):
        m = module_with(BrokenPikafish())
        self.assertEqual(m.engine_move(position(MATE_IN_ONE))["action"], "b0b9")
        result = m.engine_move(position(BLACK_THREAT))
        self.assertEqual(result["action"], "a4a1")  # 吃車解殺，也是能吃到最大的子
        self.assertIn("品質很低", result["points"][0][1])


class AgentTests(SimpleTestCase):
    def test_agent_move_carries_engine_facts_and_reason(self):
        m = module_with(FakePikafish(best={0: "h2e2"}, scores={0: 30}))
        with patch("arena.agent._chat", side_effect=fake_chat(
                [("list_candidates", {})], [("play_move", {"move": "H2E2", "reason": "Pikafish 推薦 h2e2"})])):
            r = agent.agent_move(m, rules.XiangqiState(), [], "qwen3:8b", 4)
        self.assertEqual((r["action"], r["fallback"]), ("h2e2", False))
        self.assertTrue(any("Pikafish" in p["text"] for p in r["facts"]["points"]))

    def test_missing_a_win_in_one_is_sent_back(self):
        m = module_with(FakePikafish())
        with patch("arena.agent._chat", side_effect=fake_chat(
                [("play_move", {"move": "a8a7", "reason": "先等一下"})],
                [("play_move", {"move": "b0b9", "reason": "將死"})])):
            r = agent.agent_move(m, position(MATE_IN_ONE), [], "qwen3:8b", 4)
        self.assertEqual(r["action"], "b0b9")
        self.assertIn("被退回", r["narration"]["text"])


class ReviewTests(SimpleTestCase):
    GAME = ["h2e2", "h7e7", "b0c2", "b9c7"]  # 玩家執紅，第 3 步（index 2）傌八進七

    def review(self, played, best="h0g2", scores=None):
        m = module_with(FakePikafish(best={2: best}, scores=scores or {2: 50}, played={2: played}))
        return m.review({}, self.GAME, RED)

    def test_a_big_loss_is_flagged_with_the_engines_suggestion(self):
        moments = self.review(-300)
        self.assertEqual([(x["index"], x["played"], x["best"], x["level"], x["title"]) for x in moments],
                         [(2, "b0c2", "h0g2", "key", "損失約 3.5 個兵")])
        self.assertIn("紅方走傌八進七（b0c2）後約落後 3.0 個兵；走傌二進三（h0g2）的話約領先 0.5 個兵",
                      moments[0]["points"][0]["text"])

    def test_losses_are_graded_and_noise_or_the_engines_own_choice_is_not_listed(self):
        # 3 個兵以上是關鍵時刻、1.5–3 是小失誤、0.5–1.5 只是建議；再小在搜尋誤差內，沒有證據就不列
        level = lambda played, best="h0g2": [x["level"] for x in self.review(played, best)]
        self.assertEqual(level(-300), ["key"])
        self.assertEqual(level(-150), ["minor"])
        self.assertEqual(level(-50), ["suggest"])
        self.assertEqual(level(20), [])
        self.assertEqual(level(-900, best="b0c2"), [])  # 引擎自己的首選不列

    def test_missing_a_forced_mate_is_solver_evidence(self):
        moments = self.review(40, scores={2: ("mate", 4)})
        self.assertEqual((moments[0]["level"], moments[0]["pattern"]), ("key", "錯過殺棋"))
        self.assertEqual(moments[0]["points"][0]["evidence"], SOLVED)

    def test_already_being_mated_is_not_blamed_on_this_move(self):
        self.assertEqual(self.review(("mate", -2), scores={2: ("mate", -3)}), [])

    def test_rule_confirmed_mistakes_need_no_engine(self):
        # 錯過一步殺、讓對方一步殺都由規則確認，Pikafish 不能用時照樣找得到
        m = module_with(BrokenPikafish())
        found = m._review_move(position(MATE_IN_ONE), 0, "a8a7")
        self.assertEqual((found["level"], found["pattern"], found["best"]), ("key", "錯過一步殺", "b0b9"))
        found = m._review_move(position(BLACK_THREAT), 0, "i3i4")
        self.assertEqual((found["level"], found["pattern"]), ("key", "讓對方一步殺"))
        self.assertTrue(all(p["evidence"] == RULE for p in found["points"]))
        self.assertIsNone(m._review_move(position(BLACK_THREAT), 0, "a4a1"))

    def test_review_without_pikafish_is_empty_not_an_error(self):
        self.assertEqual(module_with(BrokenPikafish()).review({}, self.GAME, RED), [])

    def test_the_agents_candidates_the_first_choice_and_the_review_agree(self):
        # 2026-11-19 情境測試：候選清單用 MultiPV 5、首選用 MultiPV 1，兩次搜尋的第一名不同。
        # 同一步的分析原文寫著「候選第 1 是車９進１」又寫「Pikafish 的首選是車２進４」，
        # Agent 照候選第一名走，教練又說它沒照引擎首選。Agent、引擎代下、復盤要用同一份第一名
        class SplitPikafish(FakePikafish):
            def analyze(self, actions, depth, multipv=1, searchmoves=None):
                if multipv == 1 and not searchmoves:  # 另一次搜尋的第一名不一樣
                    return [self.entry(sorted(rules.legal_moves(replay(actions)))[-1], 0)]
                return super().analyze(actions, depth, multipv, searchmoves)
        m = module_with(SplitPikafish())
        s = replay([])
        move = m.engine_move(s)["action"]
        self.assertIn(f"1. {m.move_text(s, move)}", m.run_tool(s, "list_candidates", {})[1][0][1])
        self.assertNotIn("Pikafish 的首選是", " ".join(t for _, t in m.run_tool(s, "check_move", {"move": move})[1]))
        self.assertEqual(m.review({}, [move], RED), [])

    def test_facts_carry_the_continuation_so_the_answer_can_say_why(self):
        # 沒有後續變化時，模型只能說「引擎比較推薦另一步」（2026-11-19 情境測試：「資訊不足以分析具體的後續變化」）
        class LinePikafish(FakePikafish):
            def analyze(self, actions, depth, multipv=1, searchmoves=None):
                lines = super().analyze(actions, depth, multipv, searchmoves)
                for e in lines:  # 主要變化：這一步之後，雙方各走第一個合法步
                    state = rules.play(replay(actions), e["move"])
                    reply = sorted(rules.legal_moves(state))[0]
                    e["pv"] = [e["move"], reply, sorted(rules.legal_moves(rules.play(state, reply)))[0]]
                return lines
        m = module_with(LinePikafish(best={2: "h0g2"}, scores={2: 50}, played={2: -300}))
        moment, = m.review({}, self.GAME, RED)
        texts = [p["text"] for p in moment["points"]]
        self.assertTrue(any(t.startswith("依引擎估計，紅方走傌八進七（b0c2）之後的後續：黑方") for t in texts), texts)
        self.assertTrue(any(t.startswith("依引擎估計，紅方走傌二進三（h0g2）之後的後續：黑方") for t in texts), texts)
        self.assertTrue(all(p["evidence"] == ESTIMATE for p in moment["points"] if "後續" in p["text"]))


@override_settings(OLLAMA_URL="http://127.0.0.1:9", OLLAMA_TIMEOUT=2)
class GameApiTests(TestCase):
    client_class = AdminClient

    def setUp(self):
        patcher = patch.object(MODULE, "analyzer", FakePikafish())
        patcher.start()
        self.addCleanup(patcher.stop)
        MODULE._cache.clear()
        self.addCleanup(MODULE._cache.clear)
        # 不等 Ollama 逾時：直接讓模型呼叫失敗，由（假的）Pikafish 代下
        chat = patch("arena.agent._chat", side_effect=OSError("測試不連 Ollama"))
        chat.start()
        self.addCleanup(chat.stop)

    def post(self, url, body):
        return self.client.post(url, json.dumps(body), content_type="application/json")

    def test_moves_are_validated_and_saved(self):
        game_id = self.post("/api/xiangqi/games/", {"seat": RED, "settings": {}}).json()["game"]["id"]
        r = self.post(f"/api/xiangqi/games/{game_id}/move/", {"action": "H2E2"})
        self.assertEqual(r.status_code, 200)
        saved = Game.objects.get(pk=game_id)
        self.assertEqual(len(saved.actions), 2)
        self.assertEqual(saved.actions[0], "h2e2")
        MODULE.replay(saved.settings, saved.actions)

    def test_illegal_move_is_rejected_and_not_saved(self):
        game = Game.objects.create(game_type="xiangqi", human_seat=RED, settings={}, actions=[])
        r = self.post(f"/api/xiangqi/games/{game.id}/move/", {"action": "h2h7"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("不能這樣走", r.json()["error"])
        game.refresh_from_db()
        self.assertEqual(game.actions, [])

    def test_the_game_is_listed_with_the_others(self):
        self.assertContains(self.client.get("/"), "象棋")
        self.assertEqual(self.client.get("/xiangqi/").status_code, 200)

    def test_agent_gives_exactly_what_the_old_code_gave(self):
        # 象棋的請求改交給 AI 助理（2026-11-02）：結果要和原本的程式一樣
        arena_tests.assert_agent_gives_what_the_old_code_gave(
            self, MODULE, lambda: Game.objects.create(game_type="xiangqi", human_seat=RED, settings={}, actions=[]), "h2e2")

    def test_question_focus_reaches_the_position(self):
        # 中文記法要看局面才換算得出是哪一步，Agent 照玩家的說法填也要查得到
        game = Game.objects.create(game_type="xiangqi", human_seat=RED, settings={}, actions=[])
        for focus in ("炮二平五", "h2e2"):
            arena_tests.assert_question_focus_reaches_the_position(self, MODULE, game, 0, focus)


@skipUnless(Path(settings.PIKAFISH_EXE).exists(), "沒有安裝 Pikafish（tools/pikafish/pikafish.exe）")
class PikafishIntegrationTests(SimpleTestCase):
    def test_real_engine_gives_legal_candidates(self):
        m = XiangqiModule()
        s = replay(["h2e2"])
        (evidence, text), (_, line) = m.run_tool(s, "list_candidates", {})[1]
        self.assertEqual(evidence, ESTIMATE)
        self.assertIn("黑方的候選依好壞排序", text)
        self.assertIn("的後續：黑方", line)  # 主要變化，模型才講得出「為什麼」（2026-11-19）
        move = m.engine_move(s)["action"]
        self.assertTrue(rules.is_legal(s, move))
        # Agent 看到的候選第一名，就是引擎代下與復盤認定的首選（2026-11-19：兩邊用不同的搜尋，同一步的事實前後矛盾）
        self.assertIn(f"1. {m.move_text(s, move)}", text)
        self.assertNotIn("Pikafish 的首選是", " ".join(t for _, t in m.run_tool(s, "check_move", {"move": move})[1]))

    def test_real_engine_candidates_first_is_what_it_plays_and_review_suggests(self):
        # 中炮對屏風馬（benchmark_samples[1]）：深度 12 時 MultiPV 1 的首選是兵七進一（c3c4）、MultiPV 5 是兵三進一（g3g4），
        # 以前候選清單和引擎代下各用一種，7 個模型照清單走都被算成和引擎不同（docs/交接.md 4.5）
        m = XiangqiModule()
        actions = MODULE.benchmark_samples[1]
        s = replay(actions)
        top = m.engine_move(s)["action"]
        (_, candidates), *_ = m.run_tool(s, "list_candidates", {})[1]  # 後面是首選的後續變化
        self.assertIn(f"1. {m.move_text(s, top)}", candidates)
        self.assertIsNone(m._review_move(s, len(actions), top))
        second = m._lines(s)[1]["move"]
        self.assertIn(f"Pikafish 的首選是 {m.move_text(s, top)}。", [t for _, t in m._check(s, second)])
        found = m._review_move(s, len(actions), second)
        self.assertTrue(found is None or found["best"] == top, found)

    def test_engine_playing_both_sides_is_never_flagged(self):
        # 復盤和 AI 下棋用同一個標準：引擎自己選的步，復盤時不能被說成失誤
        m = XiangqiModule()
        s, actions = rules.XiangqiState(), []
        for _ in range(16):
            action = m.engine_move(s)["action"]
            actions.append(action)
            s = rules.play(s, action)
        self.assertEqual(m.review({}, actions, RED) + m.review({}, actions, BLACK), [])
