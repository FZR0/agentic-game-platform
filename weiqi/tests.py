import json
from pathlib import Path
from unittest import skipUnless
from unittest.mock import patch

from django.conf import settings
from django.test import SimpleTestCase, TestCase, override_settings

from arena import agent
from arena.base import ESTIMATE, RULE
from arena.models import Game

from . import rules
from .katago import KataGoUnavailable
from .module import MODULE, WeiqiModule
from .rules import BLACK, WHITE

SMALL = {"size": 9, "komi": 7.5}


def replay(actions, size=9):
    return MODULE.replay({"size": size, "komi": 7.5}, actions)


class FakeKataGo:
    """假的 KataGo，數值都是黑棋觀點：winrates {第幾步: 勝率}（預設 0.5）、leads {第幾步: 目差}（預設 0）、
    best {第幾步: 最佳候選}（預設 default）。"""

    def __init__(self, winrates=None, leads=None, best=None, default="C7"):
        self.winrates, self.leads, self.best, self.default = winrates or {}, leads or {}, best or {}, default

    def analyze(self, size, komi, actions, turns, visits, ownership=False):
        out = {}
        for t in turns:
            wr, lead = self.winrates.get(t, 0.5), self.leads.get(t, 0.0)
            out[t] = {"root": {"winrate": wr, "scoreLead": lead},
                      "moves": [{"move": self.best.get(t, self.default), "winrate": wr, "scoreLead": lead, "visits": 10}]}
        return out


class BrokenKataGo:
    def analyze(self, *args, **kwargs):
        raise KataGoUnavailable("找不到 katago.exe")


def module_with(analyzer):
    m = WeiqiModule()
    m.analyzer = analyzer
    return m


def fake_chat(*replies):
    """假的模型：依序回傳 [(工具名, 參數), ...]。"""
    queue = list(replies)

    def chat(messages, config, tools):
        return {"content": "", "tool_calls": [{"function": {"name": n, "arguments": a}} for n, a in queue.pop(0)]}
    return chat


# 劫：黑 D5 提掉 C5 之後，白不能馬上在 C5 提回來
KO = ["C6", "D6", "B5", "C5", "C4", "D4", "J1", "E5", "D5"]


class RuleTests(SimpleTestCase):
    def test_surrounded_stones_are_captured_and_counted(self):
        s = replay(["D5", "E5", "F5", "A9", "E4", "A8", "E6"])
        self.assertEqual(s.board[4][4], rules.EMPTY)
        self.assertEqual(s.captures[BLACK], 1)

    def test_suicide_is_illegal(self):
        with self.assertRaisesRegex(ValueError, "自殺"):
            replay(["A2", "J9", "B1", "A1"])

    def test_ko_cannot_be_retaken_immediately_but_can_after_a_move_elsewhere(self):
        # 局面超級劫：盤面不能重複，否則雙方可以無限互提
        s = replay(KO)
        self.assertEqual(s.captures[BLACK], 1)
        with self.assertRaisesRegex(ValueError, "劫"):
            rules.play(s, "C5")
        s = rules.play(rules.play(s, "J9"), "J2")  # 白找劫材、黑應劫材以外的地方
        self.assertEqual(rules.play(s, "C5").captures[WHITE], 1)

    def test_two_passes_end_the_game_and_count_area_with_komi(self):
        s = replay(["E5", "pass", "pass"])
        self.assertTrue(s.over)
        self.assertEqual(rules.result(s), (BLACK, "數子：黑棋 81，白棋 0＋貼目 7.5；黑棋勝 73.5 目"))

    def test_stones_left_on_the_board_count_as_alive(self):
        # 終局不判死活：白 A1 孤子還在盤上，就讓黑棋的空不算地。所以虛手前要先把死子提掉。
        s = replay(["E5", "A1", "pass", "pass"])
        winner, text = rules.result(s)
        self.assertEqual(winner, WHITE)
        self.assertIn("黑棋 1，白棋 1", text)

    def test_resign_ends_the_game_and_nothing_can_follow(self):
        self.assertEqual(rules.result(replay(["E5", "resign"])), (BLACK, "白棋認輸，黑棋獲勝"))
        with self.assertRaises(ValueError):
            replay(["E5", "resign", "C3"])

    def test_columns_skip_i(self):
        self.assertEqual(rules.parse("J9", 9), (8, 8))
        self.assertIsNone(rules.parse("I5", 9))
        self.assertIsNone(rules.parse("K5", 9))  # 9 路沒有 K 欄


class ModuleTests(SimpleTestCase):
    def test_normalize_accepts_chinese_words_and_lowercase(self):
        self.assertEqual([MODULE.normalize(t) for t in ("d4", "虛手", "PASS", "認輸", "I5", "Z1")],
                         ["D4", "pass", "pass", "resign", None, None])

    def test_agent_cannot_resign_or_play_illegal_points(self):
        m = module_with(FakeKataGo())
        s = replay(["A2", "J9", "B1"])
        self.assertIn("不能認輸", m.rejection(s, "resign"))
        self.assertIn("自殺", m.rejection(s, "A1"))
        self.assertIn("已經有棋子", m.rejection(s, "A2"))
        self.assertIsNone(m.rejection(s, "E5"))

    def test_agent_may_pass_only_when_katago_would(self):
        # 太早虛手等於把盤面讓給對手；只有 KataGo 也認為該虛手時才放行
        s = replay(["E5"])
        self.assertIn("太早", module_with(FakeKataGo(best={1: "C3"})).rejection(s, "pass"))
        self.assertIsNone(module_with(FakeKataGo(best={1: "pass"})).rejection(s, "pass"))

    def test_no_legal_point_means_a_forced_pass(self):
        s = rules.GoState(9, 7.5)
        eyes = {(1, 1), (4, 4), (7, 7)}  # 黑棋一大塊有三個眼，白下任何眼都是自殺
        s.board = [[rules.EMPTY if (x, y) in eyes else BLACK for x in range(9)] for y in range(9)]
        s.to_move = WHITE
        self.assertEqual(MODULE.forced(s)["action"], "pass")
        self.assertIsNone(MODULE.forced(replay([])))

    def test_frame_shows_the_board_and_final_result(self):
        f = MODULE.frame(replay(["E5", "pass", "pass"]), BLACK)
        self.assertEqual((f["size"], len(f["board"]), f["board"][4 * 9 + 4]), (9, 81, "B"))
        self.assertEqual((f["last"], f["passes"]), ("pass", 2))
        self.assertIn("黑棋勝 73.5 目", f["result"])

    def test_threats_report_stones_in_atari_from_both_sides(self):
        # 白 E5 被叫吃（只剩 E6）。同一件事對輪到的一方意義不同（逃或提），事實要寫清楚是哪一方
        atari = ["D5", "E5", "F5", "A9", "E4"]
        white_view = MODULE.run_tool(replay(atari), "find_threats", {})[1]
        self.assertIn((RULE, "白棋 E5 被叫吃（只剩 E6 1 氣），黑棋下一手下 E6 就能提掉 1 子。"), white_view)
        black_view = MODULE.run_tool(replay(atari + ["A8"]), "find_threats", {})[1]
        self.assertIn((RULE, "白棋 E5 只剩 1 氣，黑棋現在下 E6 可以提掉 1 子。"), black_view)

    def test_candidates_are_from_the_movers_view(self):
        # KataGo 回報黑棋觀點；輪到白時要換算，否則 Agent 會把對手的好棋當成自己的
        m = module_with(FakeKataGo(winrates={1: 0.8}, leads={1: 6.0}, best={1: "C3"}))
        (evidence, text), = m.run_tool(replay(["E5"]), "list_candidates", {})[1]
        self.assertEqual(evidence, ESTIMATE)
        self.assertIn("白棋目前勝率約 20%", text)
        self.assertIn("C3（勝率 20%，落後約 6.0 目）", text)

    def test_engine_falls_back_to_simple_rules_without_katago(self):
        s = replay(["D5", "E5", "F5", "A9", "E4"])  # 白 E5 只剩 1 氣
        result = module_with(BrokenKataGo()).engine_move(s)
        self.assertEqual(result["action"], "E6")
        self.assertIn("品質很低", result["points"][0][1])


class AgentTests(SimpleTestCase):
    def play(self, analyzer, actions, *replies):
        m = module_with(analyzer)
        with patch("arena.agent._chat", side_effect=fake_chat(*replies)):
            return agent.agent_move(m, replay(actions), actions, "qwen3:8b", 4)

    def test_agent_move_carries_katago_facts(self):
        r = self.play(FakeKataGo(best={1: "C3"}), ["E5"], [("list_candidates", {})],
                      [("play_move", {"coord": "c3", "reason": "KataGo 推薦 C3"})])
        self.assertEqual((r["action"], r["fallback"]), ("C3", False))
        self.assertTrue(any(p["evidence"] == ESTIMATE and "KataGo" in p["text"] for p in r["facts"]["points"]))

    def test_early_pass_is_sent_back(self):
        r = self.play(FakeKataGo(best={1: "C3"}), ["E5"], [("play_move", {"coord": "pass", "reason": "結束"})],
                      [("play_move", {"coord": "C3", "reason": "KataGo 推薦 C3"})])
        self.assertEqual(r["action"], "C3")
        self.assertIn("被退回", r["narration"]["text"])


class ReviewTests(SimpleTestCase):
    GAME = ["E5", "C7", "J9", "G7"]  # 玩家執黑，第 3 手（index 2）J9

    def test_a_big_point_loss_is_flagged_with_katagos_suggestion(self):
        m = module_with(FakeKataGo(leads={2: 2.0, 3: -5.0}, best={2: "C3"}))
        moments = m.review(SMALL, self.GAME, BLACK)
        self.assertEqual([(x["index"], x["played"], x["best"], x["title"]) for x in moments], [(2, "J9", "C3", "損失約 7 目")])
        self.assertIn("黑棋下 J9 前領先約 2.0 目，下完後落後約 5.0 目，約損失 7.0 目", moments[0]["points"][0]["text"])
        self.assertTrue(all(p["evidence"] in (RULE, ESTIMATE) for p in moments[0]["points"]))

    def test_losses_are_graded_and_tiny_ones_or_the_engines_own_choice_are_not_listed(self):
        # 問題全部列出但分級：4 目以上是關鍵時刻、2–4 目是小失誤、1–2 目不算失誤只列為建議；
        # 小於 1 目在 KataGo 低搜尋次數的誤差內，沒有證據就不列
        level = lambda after, best="C3": [x["level"] for x in module_with(
            FakeKataGo(leads={2: 2.0, 3: after}, best={2: best})).review(SMALL, self.GAME, BLACK)]
        self.assertEqual(level(-5.0), ["key"])
        self.assertEqual(level(-1.0), ["minor"])
        self.assertEqual(level(0.5), ["suggest"])
        self.assertEqual(level(1.5), [])
        self.assertEqual(level(-9.0, best="J9"), [])  # KataGo 自己的首選不列

    def test_win_rate_saturation_does_not_hide_mistakes(self):
        # 9 路貼 7.5 目時黑棋一開局勝率就很低；用勝率判斷會讓黑棋永遠找不到失誤，所以用目數。
        # 勝負已定時照樣列出，但要註明這一手不是輸棋的原因，不能誇大證據。
        decided = lambda m: any("勝負大致已定" in p["text"] for p in m["points"])
        close = module_with(FakeKataGo(winrates={2: 0.03, 3: 0.01}, leads={2: -1.0, 3: -9.0}, best={2: "C3"}))
        moments = close.review(SMALL, self.GAME, BLACK)
        self.assertEqual([x["played"] for x in moments], ["J9"])
        self.assertFalse(decided(moments[0]))  # 只落後 1 目，對學習者來說勝負未定
        far = module_with(FakeKataGo(winrates={2: 0.01, 3: 0.0}, leads={2: -15.0, 3: -25.0}, best={2: "C3"}))
        moments = far.review(SMALL, self.GAME, BLACK)
        self.assertTrue(decided(moments[0]))
        self.assertTrue(any("不是輸棋的原因" in p["text"] for p in moments[0]["points"]))

    def test_white_players_loss_is_measured_from_whites_view(self):
        # KataGo 回報黑棋觀點：黑棋目差上升＝白棋損失
        m = module_with(FakeKataGo(leads={1: -1.0, 2: 6.0}, best={1: "C3"}))
        moments = m.review(SMALL, ["E5", "J9", "C7"], WHITE)
        self.assertEqual([x["played"] for x in moments], ["J9"])
        self.assertIn("白棋下 J9 前領先約 1.0 目，下完後落後約 6.0 目", moments[0]["points"][0]["text"])

    def test_review_without_katago_is_empty_not_an_error(self):
        self.assertEqual(module_with(BrokenKataGo()).review(SMALL, ["E5", "C7"], BLACK), [])


@override_settings(OLLAMA_URL="http://127.0.0.1:9", OLLAMA_TIMEOUT=2)
class GameApiTests(TestCase):
    def setUp(self):
        self.kata = FakeKataGo()
        patcher = patch.object(MODULE, "analyzer", self.kata)
        patcher.start()
        self.addCleanup(patcher.stop)
        MODULE._cache.clear()
        self.addCleanup(MODULE._cache.clear)

    def post(self, url, body):
        return self.client.post(url, json.dumps(body), content_type="application/json")

    def new_game(self, **extra):
        return self.post("/api/weiqi/games/", {"seat": BLACK, "settings": {"size": 9}, **extra})

    def test_moves_are_validated_and_saved(self):
        game_id = self.new_game().json()["game"]["id"]
        r = self.post(f"/api/weiqi/games/{game_id}/move/", {"action": "e5"})
        self.assertEqual(r.status_code, 200)
        saved = Game.objects.get(pk=game_id)
        self.assertEqual(saved.settings, SMALL)
        self.assertEqual(saved.actions, ["E5", "C7"])  # Ollama 不在時由（假的）KataGo 代下
        MODULE.replay(saved.settings, saved.actions)

    def test_illegal_move_is_rejected_and_not_saved(self):
        game = Game.objects.create(game_type="weiqi", human_seat=WHITE, settings=SMALL, actions=["A2", "J9", "B1"])
        r = self.post(f"/api/weiqi/games/{game.id}/move/", {"action": "A1"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("自殺", r.json()["error"])
        game.refresh_from_db()
        self.assertEqual(game.actions, ["A2", "J9", "B1"])

    def test_board_size_is_validated(self):
        self.assertEqual(self.new_game(settings={"size": 15}).status_code, 400)

    def test_both_passing_ends_the_game_with_a_count(self):
        self.kata.default = "pass"
        game = Game.objects.create(game_type="weiqi", human_seat=BLACK, settings=SMALL, actions=["E5", "C7"])
        r = self.post(f"/api/weiqi/games/{game.id}/move/", {"action": "pass"}).json()
        self.assertEqual([m["action"] for m in r["ai_moves"]], ["pass"])
        self.assertEqual(r["winner"], WHITE)  # 各 1 子、沒有地，貼目讓白勝
        self.assertIn("白棋勝 7.5 目", r["ai_moves"][0]["frame"]["result"])
        self.assertTrue(Game.objects.get(pk=game.id).finished)


@skipUnless(Path(settings.KATAGO["exe"]).exists(), "沒有安裝 KataGo（tools/katago/katago.exe）")
class KataGoIntegrationTests(SimpleTestCase):
    def test_real_engine_gives_legal_candidates(self):
        m = WeiqiModule()
        s = replay(["E5"])
        (evidence, text), = m.run_tool(s, "list_candidates", {})[1]
        self.assertEqual(evidence, ESTIMATE)
        self.assertIn("白棋目前勝率約", text)
        move = m.engine_move(s)["action"]
        self.assertTrue(rules.is_legal(s, move), move)
