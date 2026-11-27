import json
from unittest.mock import patch

from django.conf import settings
from django.test import SimpleTestCase, TestCase, override_settings

from arena.tests import AdminClient
from arena import agent
from arena.models import AiNote, Game

from . import engine
from .engine import BLACK, WHITE, RULE, SOLVED, ESTIMATE
from .module import MODULE, find_threats, list_candidates


def at(name):
    """'H8' -> (7, 7)"""
    return engine.COLS.index(name[0]), int(name[1:]) - 1


def board_with(black=(), white=()):
    b = engine.new_board()
    for names, color in ((black, BLACK), (white, WHITE)):
        for n in names:
            x, y = at(n)
            b[y][x] = color
    return b


def evidence_of(points):
    return {p["evidence"] for p in points}


# 黑棋：橫向 H8-J8 左邊被白 G8 擋住，直向 K9、K10。
# 黑 K8 衝四逼白擋 L8，同時在直向形成三，下一手 K7 或 K11 成活四。
VCF_POSITION = dict(black=["H8", "I8", "J8", "K9", "K10"], white=["G8", "A15", "O15", "O1"])


class RuleTests(SimpleTestCase):
    def test_overline_wins_under_freestyle_rule(self):
        # 採自由規則：六子以上也算勝。若改用連珠規則，這個測試必須跟著改。
        b = board_with(black=["B8", "C8", "D8", "F8", "G8"])
        self.assertTrue(engine.makes_five(b, *at("E8"), BLACK))

    def test_moves_after_a_five_are_rejected(self):
        # 伺服器不能接受分出勝負後還繼續下的序列，否則勝負與復盤都會錯。
        moves = []
        for i in range(5):
            moves += [[i, 0], [i, 5]]  # 黑在第 1 列連五，白在第 6 列
        with self.assertRaises(ValueError):
            engine.build_board(moves[:9] + [[10, 10]])
        _, winner = engine.build_board(moves[:9])
        self.assertEqual(winner, BLACK)

    def test_occupied_point_is_rejected(self):
        with self.assertRaises(ValueError):
            engine.build_board([[7, 7], [7, 7]])


class ChooseMoveTests(SimpleTestCase):
    def test_takes_own_five_before_blocking(self):
        # 雙方都有衝四時，先連五就贏了；先擋反而給對手機會。
        b = board_with(black=["H8", "I8", "J8", "K8"], white=["A1", "A2", "A3", "A4"])
        d = engine.choose_move(b, BLACK)
        self.assertIn(tuple(d["move"]), {at("G8"), at("L8")})
        self.assertEqual(d["reason"], "win")
        self.assertEqual(evidence_of(d["points"]), {RULE})

    def test_blocks_opponent_four(self):
        # 不擋衝四，對手下一手就連五，這是規則就能確認的事。
        b = board_with(black=["H8", "J10", "M3"], white=["A1", "A2", "A3", "A4"])
        d = engine.choose_move(b, BLACK)
        self.assertEqual(tuple(d["move"]), at("A5"))
        self.assertEqual(d["reason"], "block_five")
        self.assertEqual(evidence_of(d["points"]), {RULE})

    def test_blocks_open_three_at_a_point_that_actually_works(self):
        # 擋活三要擋在緊鄰的 G8 或 K8；擋在 F8 或 L8，對手仍能成活四。
        b = board_with(black=["H8", "I8", "J8"], white=["A15", "O15"])
        d = engine.choose_move(b, WHITE)
        self.assertIn(tuple(d["move"]), {at("G8"), at("K8")})
        self.assertEqual(d["reason"], "defend_vcf")

    def test_vcf_line_is_really_forced(self):
        # 連續衝四會以「求解確認」呈現給玩家，所以每一步都必須真的是強制的。
        b = board_with(**VCF_POSITION)
        line = engine.find_vcf(b, BLACK)
        self.assertIsNotNone(line)
        for i, (x, y) in enumerate(line):
            color = BLACK if i % 2 == 0 else WHITE
            if color == WHITE:
                # 白棋的應手必須是唯一能擋住黑棋連五的點
                self.assertEqual(engine.five_points(b, BLACK), [(x, y)])
            b[y][x] = color
        last = tuple(line[-1])
        self.assertGreaterEqual(len(engine.five_points(b, BLACK)), 2, f"最後一手 {engine.coord(*last)} 後白棋應擋不完")

    def test_vcf_is_played_and_labelled_as_solved(self):
        d = engine.choose_move(board_with(**VCF_POSITION), BLACK)
        self.assertEqual(d["reason"], "vcf")
        self.assertEqual(evidence_of(d["points"]), {SOLVED})


def fake_chat(*replies):
    """假的模型：依序回傳訊息，每則是 [(工具名, 參數), ...]；None 代表模型出錯。"""
    queue = list(replies)

    def chat(messages, config, tools):
        calls = queue.pop(0)
        if calls is None:
            raise OSError("模型沒有回應")
        return {"content": "", "tool_calls": [{"function": {"name": n, "arguments": a}} for n, a in calls]}
    return chat


# 黑 H8 I8 J8 活三，白 A15 O15；輪到白（Agent）
OPEN_THREE = ["H8", "A15", "I8", "O15", "J8"]
# 黑 H8-K8 衝四（G8 被白擋住），輪到白，必須擋 L8
BLACK_FOUR = ["H8", "A15", "I8", "O15", "J8", "G8", "K8"]


class AgentTests(SimpleTestCase):
    def play(self, actions, *replies, steps=None):
        with patch("arena.agent._chat", side_effect=fake_chat(*replies)):
            return agent.agent_move(MODULE, MODULE.replay({}, actions), actions, "qwen3:8b", steps)

    def test_thoughts_are_the_agents_own_verification(self):
        # 思路要呈現 Agent 實際查了什麼、看到什麼，而不是事後另外編一段說明。
        r = self.play(OPEN_THREE, [("find_threats", {})], [("check_move", {"coord": "g8"})],
                      [("play_move", {"coord": "G8", "reason": "擋住黑棋在 G8 的活四。"})])
        self.assertEqual(r["action"], "G8")
        self.assertFalse(r["fallback"])
        self.assertEqual(r["narration"]["key"], "擋住黑棋在 G8 的活四。")
        self.assertIn("查雙方威脅→檢查 G8→下 G8", r["narration"]["text"])
        self.assertIn(SOLVED, evidence_of(r["facts"]["points"]))  # 工具結果保留證據種類

    def test_agent_cannot_skip_blocking_a_four(self):
        # 不擋衝四就輸了，這是規則能確認的事；Agent 選錯時要被退回重選。
        r = self.play(BLACK_FOUR, [("play_move", {"coord": "A1", "reason": "佈局"})],
                      [("play_move", {"coord": "L8", "reason": "擋住衝四"})])
        self.assertEqual(r["action"], "L8")
        self.assertIn("被退回", r["narration"]["text"])

    def test_winning_move_is_allowed_even_when_opponent_has_a_four(self):
        # 實戰中曾發生：Agent 要連五卻被要求先擋，改去擋又被要求連五，怎麼下都被退回。
        # 自己連五就贏了，必須優先於擋對手。黑 H8-K8 衝四、白 A1-A4 衝四，輪到白。
        state = MODULE.replay({}, ["H8", "A1", "I8", "A2", "J8", "A3", "O15", "A4", "K8"])
        self.assertIsNone(MODULE.rejection(state, "A5"))
        self.assertIsNotNone(MODULE.rejection(state, "L8"))  # 擋了但沒連五，仍要退回

    def test_illegal_coordinate_is_rejected(self):
        r = self.play(OPEN_THREE, [("play_move", {"coord": "Z99", "reason": "x"})],
                      [("play_move", {"coord": "H8", "reason": "x"})],
                      [("play_move", {"coord": "K8", "reason": "擋住"})])
        self.assertEqual(r["action"], "K8")

    def test_reason_mentioning_unverified_points_is_dropped(self):
        # Agent 的理由也要通過說明檢查：提到沒查證過的座標就不採用。
        r = self.play(OPEN_THREE, [("find_threats", {})],
                      [("play_move", {"coord": "G8", "reason": "下 G8 之後可以接 N2。"})])
        self.assertEqual(r["action"], "G8")
        self.assertIsNone(r["narration"]["key"])

    def test_engine_takes_over_when_the_model_fails(self):
        # 模型沒回應時對局不能卡住，而且要老實標明這一手是引擎代下的。
        r = self.play(OPEN_THREE, None)
        self.assertTrue(r["fallback"])
        board = MODULE.replay({}, OPEN_THREE).board
        self.assertEqual(r["action"], engine.coord(*engine.choose_move(board, WHITE)["move"]))
        self.assertIn("引擎代下", r["facts"]["points"][0]["text"])

    def test_engine_takes_over_after_the_step_limit(self):
        r = self.play(OPEN_THREE, *([[("list_candidates", {})]] * MODULE.default_steps))
        self.assertTrue(r["fallback"])
        self.assertIn("已做的步驟：看引擎候選", r["facts"]["points"][0]["text"])  # 代下時要查得出原因

    def test_last_step_only_allows_playing(self):
        # 實戰中 Agent 曾一直查證到次數用完而被引擎代下；最後一次只給 play_move，逼它做決定。
        # 查證次數是玩家的設定，用非預設的 3 次確認設定真的生效。
        offered = []
        chat = fake_chat([("list_candidates", {})], [("list_candidates", {})],
                         [("play_move", {"coord": "G8", "reason": "擋住黑棋的活三"})])

        def recording(messages, config, tools):
            offered.append([t["function"]["name"] for t in tools])
            return chat(messages, config, tools)

        with patch("arena.agent._chat", side_effect=recording):
            r = agent.agent_move(MODULE, MODULE.replay({}, OPEN_THREE), OPEN_THREE, "qwen3:8b", 3)
        self.assertFalse(r["fallback"])
        self.assertEqual(len(offered), 3)
        self.assertEqual(offered[-1], ["play_move"])
        self.assertIn("check_move", offered[0])


class ReviewTests(SimpleTestCase):
    def test_missed_block_is_flagged_with_rule_evidence(self):
        b = board_with(black=["H8", "J10", "M3"], white=["A1", "A2", "A3", "A4"])
        m = engine.judge_move(b, *at("H9"), BLACK)
        self.assertEqual((m["type"], m["level"]), ("missed_block", "key"))
        self.assertEqual(tuple(m["best"]), at("A5"))
        self.assertEqual(evidence_of(m["points"]), {RULE})

    def test_unstoppable_double_four_is_not_blamed_on_this_move(self):
        # 對手已有兩個連五點時，怎麼下都擋不住；沒有依據就不能把敗因歸給這一手。
        b = board_with(black=["H8", "J10", "M3", "C12"],
                       white=["A1", "A2", "A3", "A4", "K15", "L15", "M15", "N15"])
        self.assertIsNone(engine.judge_move(b, *at("A5"), BLACK))

    def test_missed_vcf_is_flagged(self):
        m = engine.judge_move(board_with(**VCF_POSITION), *at("C3"), BLACK)
        self.assertEqual(m["type"], "missed_vcf")
        self.assertEqual(tuple(m["best"]), at("K8"))
        self.assertIn(SOLVED, evidence_of(m["points"]))

    def test_a_better_choice_by_estimate_only_is_a_minor_mistake(self):
        # 只有引擎估計（沒有規則或求解確認）的「有更好的選擇」不能和漏擋衝四同級，列為小失誤
        m = engine.judge_move(board_with(black=["H8", "J8"], white=["H9", "O15"]), *at("A1"), BLACK)
        self.assertEqual((m["type"], m["level"]), ("gap", "minor"))
        self.assertEqual(evidence_of(m["points"]), {ESTIMATE})

    def test_a_somewhat_weaker_point_is_only_a_suggestion(self):
        # 使用者要求「建議」：不算失誤，但引擎更推薦另一點（實際下的點有引擎選擇的一半到八成價值）
        b = board_with(black=["I7", "J10", "J7"], white=["E9", "J8", "E5"])
        played, best = at("H7"), engine.choose_move(b, BLACK)["move"]
        self.assertTrue(0.5 <= engine.point_value(b, *played, BLACK) / engine.point_value(b, *best, BLACK) < 0.8)
        m = engine.judge_move(b, *played, BLACK)
        self.assertEqual((m["type"], m["level"], tuple(m["best"])), ("suggest", "suggest", tuple(best)))
        self.assertEqual(evidence_of(m["points"]), {ESTIMATE})

    def test_a_five_on_the_next_move_is_not_called_an_open_four(self):
        # 2026-11-16 情境測試：白棋已有 A1–A4，事實卻寫「白棋下 A5 就形成活四或雙四，對方擋不完」，
        # 模型照著跟玩家說。下一手就連五（一定要擋）和活四（已經擋不完）是兩回事，玩家會被說錯的威脅誤導。
        b = board_with(black=["H8", "I8", "J8", "K8"], white=["A1", "A2", "A3", "A4"])
        texts = [p["text"] for p in engine.explain_position(b)]
        self.assertIn("在搜尋範圍內已確認，如果輪到白棋下，白棋下 A5 就能直接連成五子。", texts)
        self.assertFalse(any("活四或雙四" in t for t in texts), texts)
        # 真的是活四時仍然寫活四：黑棋 H8–J8 活三，下 G8 或 K8 就成活四
        d = engine.choose_move(board_with(black=["H8", "I8", "J8"], white=["A1", "A2"]), WHITE)
        self.assertTrue(any("就形成活四或雙四，對方擋不完" in p["text"] for p in d["points"]), d["points"])

    def test_asking_why_a_four_explains_what_happens_after_it(self):
        # 2026-11-18 使用者：問「為什麼衝四反而不好」，AI 教練只回「依引擎估計 I7 比較好、H5 效果較弱」。
        # 事實裡只有那一手當下形成什麼，模型講不出原因。現在要附上對方被迫擋在哪、擋完這個四就用掉了，
        # 以及擋完之後和引擎建議的下法各自怎麼發展，模型才有因果可以講
        b = board_with(black=["H8", "H7", "H6", "G7"], white=["H9", "G9", "J10", "F8"])
        texts = [p["text"] for p in engine.explain_position(b, [at("H5")])]
        self.assertIn("黑棋下 H5 是衝四，白棋下一手只能擋在 H4，否則黑棋就連五。", texts)
        self.assertTrue(any(t.startswith("白棋擋住 H4 之後，黑棋在 H5 的這個四就用掉了") for t in texts), texts)
        best = engine.coord(*engine.choose_move(b, BLACK)["move"])
        self.assertTrue(any(t.startswith("依引擎估計，白棋擋住 H4 之後的後續：黑棋下 ") for t in texts), texts)
        self.assertTrue(any(t.startswith(f"依引擎估計，黑棋下 {best} 之後的後續：白棋下 ") for t in texts), texts)
        # 後續的每一手都是引擎的選擇，只能是估計；只有「被迫擋」和「四用掉了」是規則
        for p in engine.explain_position(b, [at("H5")]):
            if "後續" in p["text"]:
                self.assertEqual(p["evidence"], ESTIMATE, p["text"])

    def test_playing_the_vcf_is_not_flagged(self):
        self.assertIsNone(engine.judge_move(board_with(**VCF_POSITION), *at("K8"), BLACK))

    def test_review_never_blames_the_engines_own_choices(self):
        # 復盤與下棋必須用同一套標準：AI 自己對下的每一手都是引擎的選擇，復盤若還能挑出錯，
        # 代表它會建議 AI 自己都不會下的棋（曾建議一手被安全檢查排除、會讓對手連續衝四的棋）。
        # 只用一種開局抓不到這個錯，所以跑多種開局。
        openings = [[], [[7, 7], [8, 8]], [[7, 7], [6, 7]], [[7, 7], [8, 7], [6, 6]], [[6, 8], [7, 7]]]
        for opening in openings:
            board, winner = engine.build_board(opening)
            moves = list(opening)
            while not winner and len(moves) < 120:
                color = engine.color_to_move(moves)
                x, y = engine.choose_move(board, color)["move"]
                board[y][x] = color
                moves.append([x, y])
                winner = color if engine.makes_five(board, x, y, color) else None
            for color in (BLACK, WHITE):
                flagged = [(m["index"] + 1, m["type"]) for m in engine.review(moves, color)
                           if m["index"] >= len(opening)]
                self.assertEqual(flagged, [], f"開局 {opening}，{engine.COLOR_NAMES[color]}被誤判的手數")


class WholeGameFactsTests(SimpleTestCase):
    """2026-11-27 使用者回報第 295 盤（下滿 225 手的和局）：後半盤 AI 每手都寫「根據引擎評估，O2 是目前最好的候選點」，
    問「你認為我下棋的策略是什麼」只得到「引擎目前無法判斷你的策略」。"""

    @staticmethod
    def dead_board(*empty):
        # 每條線上同色最多連兩顆的下滿棋盤，再空出幾格：任何一段 5 格都有黑有白，雙方都不可能再連成五子
        b = [[BLACK if (x // 2 + y) % 2 == 0 else WHITE for x in range(engine.SIZE)] for y in range(engine.SIZE)]
        for name in empty:
            x, y = at(name)
            b[y][x] = engine.EMPTY
        return b

    def test_a_point_where_nobody_can_make_five_says_so(self):
        # 後半盤每個點都寫「延伸佈局」，AI 只能照抄第一名；這種點要照實寫出「已經連不成五子」
        board = self.dead_board("H8", "A1")
        self.assertEqual(engine.move_labels(board, *at("H8"), WHITE), ["這裡雙方都已經連不成五子"])
        self.assertNotIn("這裡雙方都已經連不成五子", engine.move_labels(board_with(["H8"]), *at("I8"), WHITE))

    def test_a_board_where_nobody_can_make_five_is_called_a_draw(self):
        # 第 295 盤第 190 手起雙方都連不成五：AI 查威脅、看候選、提問時都要知道這盤已經是和局，下哪裡都一樣
        fact = (RULE, "棋盤上已經沒有任何一段能讓黑棋或白棋連成五子：這盤會是和局，接下來下在哪裡都不會改變結果。")
        board = self.dead_board("H8", "A1")
        self.assertEqual(engine.dead_board_fact(board), fact)
        self.assertIn(fact, find_threats(board, WHITE))
        self.assertIn(fact, list_candidates(board, WHITE))
        self.assertIn({"evidence": RULE, "text": fact[1]}, engine.explain_position(board))
        # 還有任何一段能連五就不能這樣說
        self.assertIsNone(engine.dead_board_fact(self.dead_board("A1", "B1", "C1", "D1", "E1")))
        self.assertIsNone(engine.dead_board_fact(engine.new_board()))

    def test_style_counts_attacking_and_defending_moves(self):
        # 問策略、風格時，偏向進攻或防守由程式依次數判斷（模型不自己評斷數字）
        names = ["H2", "A15", "I2", "O15", "J2", "K2", "H8", "A11", "I8", "O11", "J8", "A5", "H14", "O5", "I14",
                 "O1", "J14"]
        moves = [at(n) for n in names]
        black = " ".join(t for _, t in engine.style_points(moves, BLACK))
        self.assertIn("你（黑棋）這盤下了 9 手：自己形成活三以上的進攻手 3 手，擋住對方眠三以上的防守手 0 手", black)
        self.assertIn("活三 3 次", black)
        self.assertIn("偏向進攻", black)
        white = " ".join(t for _, t in engine.style_points(moves, WHITE))
        self.assertIn("你（白棋）這盤下了 8 手：自己形成活三以上的進攻手 0 手，擋住對方眠三以上的防守手 1 手", white)
        self.assertIn("沒有明顯偏向哪一邊", white)  # 次數太少不下結論


@override_settings(OLLAMA_URL="http://127.0.0.1:9", OLLAMA_TIMEOUT=2)
class GameApiTests(TestCase):
    client_class = AdminClient
    # 黑：H8 H9 M3 C12 K11（散落）；白：A1-A4 衝四。第 9 手黑 K11 沒擋 A5。
    MISSED_BLOCK = ["H8", "A1", "H9", "A2", "M3", "A3", "C12", "A4", "K11"]

    def post(self, url, body):
        return self.client.post(url, json.dumps(body), content_type="application/json")

    def new_game(self, seat=BLACK, **extra):
        return self.post("/api/gomoku/games/", {"seat": seat, **extra}).json()

    def saved(self, actions, seat=BLACK, **extra):
        return Game.objects.create(game_type="gomoku", human_seat=seat, actions=actions, **extra)

    def test_moves_and_ai_reply_are_saved(self):
        # 存檔的目的是之後能復盤，所以玩家與 AI 的每一手都要寫進資料庫。
        game_id = self.new_game()["game"]["id"]
        r = self.post(f"/api/gomoku/games/{game_id}/move/", {"action": "h8"})
        self.assertEqual(r.status_code, 200)
        saved = Game.objects.get(pk=game_id).actions
        self.assertEqual(saved, ["H8", r.json()["ai_moves"][0]["action"]])  # 小寫輸入也存成標準記法
        MODULE.replay({}, saved)  # 存下來的棋譜必須合法

    def test_frames_follow_the_saved_actions(self):
        # 網頁只照伺服器送的局面畫圖、不自己算規則，所以每一步之後的局面都必須和棋譜一致。
        game = self.saved(["H8", "I9", "H9"])
        frames = self.client.get(f"/api/gomoku/games/{game.id}/").json()["frames"]
        self.assertEqual(len(frames), 4)
        last = frames[-1]
        self.assertEqual((last["board"][7 * 15 + 7], last["board"][8 * 15 + 8], last["board"][8 * 15 + 7]), ("B", "W", "B"))
        self.assertEqual((last["last"], last["to_move"]), ("H9", WHITE))
        self.assertEqual(frames[0]["board"], "." * 225)

    def test_steps_setting_reaches_the_agent_and_is_validated(self):
        # 「Agent 每手查證次數」是玩家的設定：要真的傳到 Agent，超出範圍要拒絕。
        game_id = self.new_game()["game"]["id"]
        bad = self.post(f"/api/gomoku/games/{game_id}/move/", {"action": "H8", "steps": 1})
        self.assertEqual(bad.status_code, 400)
        result = {"action": "H9", "facts": {"kind": "ai_move", "points": []}, "narration": None, "fallback": True,
                  "run": None}
        with patch("arena.views.agent.agent_move", return_value=result) as agent_move:
            self.post(f"/api/gomoku/games/{game_id}/move/", {"action": "H8", "steps": 3})
        self.assertEqual(agent_move.call_args.args[4], 3)

    def test_unknown_game_is_not_found(self):
        self.assertEqual(self.client.get("/api/nope/games/").status_code, 404)
        self.assertEqual(self.client.get("/nope/").status_code, 404)

    def test_unknown_or_unsuitable_model_is_rejected(self):
        # 標成不能當 Agent 的模型（例如每次回應約 70 秒的思考版）不能拿來下棋：Agent 一手要呼叫好幾次。
        slow = {"name": "slow-thinker:4b", "label": "思考版", "think": True, "agent": False}
        with override_settings(OLLAMA_MODELS=settings.OLLAMA_MODELS + [slow]):
            self.assertEqual(self.post("/api/gomoku/games/", {"seat": BLACK, "model": slow["name"]}).status_code, 400)
        self.assertEqual(self.post("/api/gomoku/games/", {"seat": BLACK, "model": "nope:1b"}).status_code, 400)
        facts = {"kind": "ai_move", "points": [{"evidence": ESTIMATE, "text": "x"}]}
        self.assertEqual(self.post("/api/gomoku/narrate/", {"facts": facts, "model": "nope:1b"}).status_code, 400)

    def test_agent_explanation_is_saved_with_the_move(self):
        # Agent 的思路在落子當下就存好，復盤時看到的和下棋時一樣。
        game_id = self.new_game()["game"]["id"]
        narration = {"key": "擋住", "text": "查證過程：下 H9。", "source": "agent"}
        result = {"action": "H9", "facts": {"kind": "ai_move", "points": []}, "narration": narration, "fallback": False,
                  "run": None}
        with patch("arena.views.agent.agent_move", return_value=result):
            self.post(f"/api/gomoku/games/{game_id}/move/", {"action": "H8"})
        note = AiNote.objects.get(game_id=game_id, index=1)
        self.assertEqual((note.narration, note.seat), (narration, WHITE))

    def test_ai_thoughts_are_saved_for_review(self):
        # 復盤與打開舊局時要能回看下棋當下的 AI 思路，所以每一手的事實都要存下來。
        game_id = self.new_game()["game"]["id"]
        ai = self.post(f"/api/gomoku/games/{game_id}/move/", {"action": "H8"}).json()["ai_moves"][0]
        notes = self.client.get(f"/api/gomoku/games/{game_id}/").json()["notes"]
        self.assertEqual([(n["index"], n["facts"]) for n in notes], [(1, ai["facts"])])

    def test_note_narration_is_saved_and_reused(self):
        # 復盤看到的說明要和下棋當下一樣，也不必再花時間重新產生。
        game_id = self.new_game()["game"]["id"]
        self.post(f"/api/gomoku/games/{game_id}/move/", {"action": "H8"})
        llm = {"key": "重點", "text": "說明", "source": "llm", "model": "qwen3:8b", "label": "Qwen3 8B"}
        with patch("arena.views.narrator.narrate", return_value=llm) as narrate:
            first = self.post(f"/api/gomoku/games/{game_id}/notes/1/narrate/", {}).json()
            second = self.post(f"/api/gomoku/games/{game_id}/notes/1/narrate/", {}).json()
        self.assertEqual(narrate.call_count, 1)
        # handled 是這次請求花的秒數等處理資訊，每次不同；要比的是說明本身
        first.pop("handled"), second.pop("handled")
        self.assertEqual(first, second)

    def test_fallback_narration_is_not_saved(self):
        # Ollama 沒開時的範本說明不能存，否則之後 Ollama 恢復也拿不到真正的說明。
        game_id = self.new_game()["game"]["id"]
        self.post(f"/api/gomoku/games/{game_id}/move/", {"action": "H8"})
        r = self.post(f"/api/gomoku/games/{game_id}/notes/1/narrate/", {}).json()
        self.assertEqual(r["source"], "template")
        self.assertIsNone(AiNote.objects.get(game_id=game_id, index=1).narration)

    def test_ai_moves_first_when_human_is_white(self):
        data = self.new_game(WHITE)
        self.assertEqual(Game.objects.get(pk=data["game"]["id"]).actions, [data["ai_moves"][0]["action"]])
        self.assertEqual(data["ai_moves"][0]["frame"]["to_move"], WHITE)

    def test_illegal_move_is_rejected_and_not_saved(self):
        # 存檔的棋譜一定要合法，否則復盤會建立在錯誤的局面上。
        game_id = self.new_game()["game"]["id"]
        self.post(f"/api/gomoku/games/{game_id}/move/", {"action": "H8"})
        before = Game.objects.get(pk=game_id).actions
        r = self.post(f"/api/gomoku/games/{game_id}/move/", {"action": "H8"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(Game.objects.get(pk=game_id).actions, before)

    def test_finished_game_rejects_moves(self):
        game = self.saved([], winner=WHITE)
        r = self.post(f"/api/gomoku/games/{game.id}/move/", {"action": "H8"})
        self.assertEqual(r.status_code, 400)

    def test_review_uses_saved_moves(self):
        game = self.saved(self.MISSED_BLOCK)
        moments = self.client.get(f"/api/gomoku/games/{game.id}/review/").json()["moments"]
        self.assertEqual([m["type"] for m in moments if m["index"] == 8], ["missed_block"])
        self.assertEqual((moments[-1]["played"], moments[-1]["best"]), ("K11", "A5"))

    def test_review_comes_with_an_overall_comment_built_from_its_findings(self):
        # 使用者要求整體評論；評論的事實必須來自這盤的檢查結果（結果、各級數量、最常見的問題），不能讓模型自己評價
        game = self.saved(self.MISSED_BLOCK)
        data = self.client.get(f"/api/gomoku/games/{game.id}/review/").json()
        texts = [p["text"] for p in data["overall"]["points"]]
        self.assertEqual(data["overall"]["kind"], "overall")
        self.assertIn("這盤還沒有下完。", texts)
        self.assertTrue(any(t.startswith("問題檢查的結果：關鍵時刻 1 個") for t in texts))
        self.assertIn("最常出現的問題：「沒有擋住對手的衝四」1 次（第 9 手）。", texts)
        narrated = self.post("/api/gomoku/narrate/", {"facts": data["overall"]})  # Ollama 不在：顯示事實原文
        self.assertEqual(narrated.status_code, 200)
        self.assertEqual(narrated.json()["source"], "template")

    def test_the_result_says_which_side_the_player_was_and_that_they_lost(self):
        # 2026-11-16 情境測試：總評只寫「白棋（AI）贏了」、終局提問的事實只寫「白棋獲勝」，
        # 模型被問「我是贏還是輸？」時只答得出「白棋獲勝」。輸贏要從玩家的角度寫明
        game = self.saved(self.MISSED_BLOCK + ["A5"], winner=WHITE)
        overall = self.client.get(f"/api/gomoku/games/{game.id}/review/").json()["overall"]
        self.assertIn("結果：你（黑棋）輸了，白棋（AI）贏了。", [p["text"] for p in overall["points"]])

    def test_a_question_at_the_end_gets_the_result_and_what_went_wrong(self):
        # 終局問「我為什麼輸？」：以前事實只有「這時對局已經結束（白棋獲勝）」，模型答不出原因，只能說白棋贏了。
        # 要附上和問題檢查同一份的結果，才指得出第幾手出了什麼問題
        game = self.saved(self.MISSED_BLOCK + ["A5"], winner=WHITE)
        r = self.post(f"/api/gomoku/games/{game.id}/ask/", {"question": "我為什麼輸？", "at": 10})
        texts = [p["text"] for p in r.json()["facts"]["points"]]
        self.assertIn("結果：你（黑棋）輸了，白棋（AI）贏了。", texts)
        self.assertIn("最常出現的問題：「沒有擋住對手的衝四」1 次（第 9 手）。", texts)

    def test_a_resigned_game_without_mistakes_is_not_described_as_full_of_mistakes(self):
        # 2026-11-17 使用者回報「不同對局 同樣結果 不同回答」：兩盤一樣的棋（黑棋 7 手後認輸、只有 1 個建議），
        # 整體評論一盤寫「建議檢討失誤」、一盤寫「表現不理想」。事實只寫「建議 1 個」，模型把建議讀成失誤；
        # 要寫明沒有失誤、建議不是失誤、這盤是認輸結束的
        from arena import operations
        game = self.saved(["H8", "I9", "resign"], winner=WHITE)
        suggestion = {"level": "suggest", "title": "建議：引擎推薦 H9", "index": 0}
        texts = [p["text"] for p in operations._overall(MODULE, game, [suggestion], BLACK)["points"]]
        self.assertIn("問題檢查的結果：沒有失誤；另有建議 1 個（建議不是失誤，只是引擎比較推薦的下法）。", texts)
        self.assertIn("問題檢查沒有找到你的失誤；這盤是你認輸結束的。", texts)
        # 真的有失誤時照樣寫出來，也不說這盤只是認輸結束的
        mistake = {"level": "key", "title": "沒有擋住對手的衝四", "index": 0, "points": [{"evidence": RULE, "text": "x"}]}
        texts = [p["text"] for p in operations._overall(MODULE, game, [mistake, suggestion], BLACK)["points"]]
        self.assertIn("問題檢查的結果：關鍵時刻 1 個；另有建議 1 個（建議不是失誤，只是引擎比較推薦的下法）。", texts)
        self.assertFalse(any("認輸結束" in t for t in texts))

    def test_a_question_at_the_end_of_an_unscored_game_says_it_has_no_result(self):
        # 2026-11-16 情境測試：「結束（不計勝負）」的最後局面問「這盤誰贏了？」，事實只有局面分析，沒說這盤不計勝負
        game = self.saved(self.MISSED_BLOCK, ended=True)
        r = self.post(f"/api/gomoku/games/{game.id}/ask/", {"question": "這盤誰贏了？", "at": len(self.MISSED_BLOCK)})
        texts = [p["text"] for p in r.json()["facts"]["points"]]
        self.assertIn("這盤沒有下完，是你選擇結束、不計勝負。", texts)
        self.assertIn("目前輪到白棋下。", texts)  # 棋停在一半：停下來時的局面照樣附上

    def test_a_question_at_the_end_of_a_resigned_game_gets_the_position_before_it(self):
        # 退回的查法（AI 助理沒完成時）也要拿得到認輸前的局面，否則問「我認輸時局面真的很差嗎？」沒有依據
        game = self.saved(["H8", "A1", "I8", "A2", "J8", "resign"], winner=BLACK)
        r = self.post(f"/api/gomoku/games/{game.id}/ask/", {"question": "認輸時局面很差嗎？", "at": 6})
        texts = [p["text"] for p in r.json()["facts"]["points"]]
        self.assertIn("結果：你（黑棋）贏了（白棋認輸，黑棋獲勝）。", texts)
        self.assertIn("第 6 手是白棋認輸；以下是認輸前的局面。", texts)

    def test_history_lists_only_this_games_records(self):
        game = self.saved(["H8"], seat=WHITE)
        Game.objects.create(game_type="other", human_seat=1, actions=[])
        listed = self.client.get("/api/gomoku/games/").json()["games"]
        self.assertEqual([(g["id"], g["action_count"]) for g in listed], [(game.id, 1)])

    def test_answer_is_grounded_in_engine_check_of_the_asked_point(self):
        # 玩家問到的點必須先經過引擎檢查，回答才有依據；問「j9」（小寫）也要認得。
        game = self.saved(self.MISSED_BLOCK)
        r = self.post(f"/api/gomoku/games/{game.id}/ask/", {"question": "這時候下 j9 可以嗎？", "at": 8})
        data = r.json()
        facts = "".join(p["text"] for p in data["facts"]["points"])
        self.assertIn("下 J9 沒有擋住 A5", facts)
        self.assertIn("實際的下一手是黑棋 K11", facts)
        self.assertEqual(data["source"], "template")  # Ollama 不可用時改用事實原文
        self.assertEqual(data["text"], facts)

    def test_question_by_move_number_analyses_that_move(self):
        # 玩家習慣說「第 9 手」而不是座標；引擎必須分析那一手，回答才有依據。
        game = self.saved(self.MISSED_BLOCK)
        r = self.post(f"/api/gomoku/games/{game.id}/ask/", {"question": "第 9 手為什麼不好？", "at": 0})
        facts = "".join(p["text"] for p in r.json()["facts"]["points"])
        self.assertIn("第 9 手是黑棋 K11", facts)
        self.assertIn("引擎把第 9 手列為關鍵時刻：沒有擋住對手的衝四", facts)

    def test_ask_rejects_position_outside_the_game(self):
        game = self.saved(["H8"])
        r = self.post(f"/api/gomoku/games/{game.id}/ask/", {"question": "現在誰比較好？", "at": 5})
        self.assertEqual(r.status_code, 400)


class ResignTests(SimpleTestCase):
    # 使用者要求五子棋也能結束對局（2026-10-21）：認輸＝這盤算對方贏，和圍棋、象棋、國際象棋的認輸一樣
    def test_resigning_ends_the_game_and_the_other_side_wins(self):
        state = MODULE.replay({}, ["H8", "I9", "resign"])
        self.assertEqual(MODULE.outcome(state), (WHITE, False))
        self.assertIn("黑棋認輸，白棋獲勝", MODULE.frame(state, BLACK)["result"])
        self.assertEqual(MODULE.normalize("認輸"), "resign")

    def test_agent_may_not_resign(self):
        # AI 對手放棄的話玩家就沒得下了；其他遊戲的 Agent 也不能認輸
        self.assertIsNotNone(MODULE.rejection(MODULE.replay({}, ["H8"]), "resign"))

    def test_review_and_questions_skip_the_resign(self):
        # 認輸不是棋子：復盤、問「第 3 手」、問認輸前的局面都不能因為它出錯
        actions = ["H8", "I9", "resign"]
        MODULE.review({}, actions, BLACK)
        self.assertIn("黑棋認輸", MODULE.explain_action({}, actions, 3)[0]["text"])
        MODULE.explain_position(MODULE.replay({}, actions[:2]), [], "resign")

    def test_asking_about_the_resign_gets_the_position_it_was_played_in(self):
        # 2026-11-16 情境測試：問「我認輸時局面真的很差嗎？」，事實只有「第 13 手是黑棋認輸」，
        # 模型就自己說「是的，非常不利」，其實引擎沒找到任何問題。要附上認輸前的局面，回答才有依據
        actions = ["H8", "A1", "I8", "A2", "J8", "resign"]  # 白棋面對黑棋的活三就認輸了，其實擋得住
        texts = [p["text"] for p in MODULE.explain_action({}, actions, 6)]
        self.assertEqual(texts[0], "第 6 手是白棋認輸；以下是認輸前的局面。")
        self.assertIn("目前輪到白棋下。", texts)
        self.assertTrue(any("黑棋下" in t and "就形成活四或雙四" in t for t in texts), texts)
        self.assertTrue(any(t.startswith("引擎會建議白棋下") for t in texts), texts)


# ---- 黑棋禁手（2026-11-20 使用者要求「黑子禁止雙活三雙活四」，三三、四四、長連各自可以設定） ----

ALL_BANS = engine.Rules(three_three=True, four_four=True, overline=True)
BAN_SETTINGS = MODULE.clean_settings({"three_three": "ban", "four_four": "ban", "overline": "ban"})
# 黑 F8 G8（橫）、H6 H7（直），白下在角落；輪到黑，H8 會同時做出兩個活三
THREE_THREE = ["F8", "A1", "G8", "A15", "H6", "O1", "H7", "O15"]
# 黑 H8 的橫向 F8 G8、斜向 F6 G7 都是活三，所以 H8 是黑棋的三三禁手；白 H9–H12 衝四（H13 被黑擋住），唯一的連五點就是 H8
FOUR_ON_BAN = ["F8", "H9", "G8", "H10", "F6", "H11", "G7", "A1", "H7", "A15", "H13", "H12"]


class ForbiddenShapeTests(SimpleTestCase):
    """禁手的定義。判錯的話，玩家會被拒絕下一手合法的棋，或黑棋下出規則不允許的棋。"""

    def ban(self, black, white=(), point="H8", rules=ALL_BANS, color=BLACK):
        return engine.forbidden(board_with(black, white), *at(point), color, rules)

    def test_double_three_is_forbidden_for_black_only_and_only_when_banned(self):
        self.assertEqual(self.ban(["F8", "G8", "H6", "H7"]), "三三")
        self.assertEqual(self.ban(["F8", "G8", "H6", "H7"], color=WHITE), None, "白棋沒有禁手")
        self.assertIsNone(self.ban(["F8", "G8", "H6", "H7"], rules=engine.FREESTYLE), "預設的自由規則沒有禁手")
        self.assertIsNone(self.ban(["F8", "G8", "H6", "H7"], rules=engine.Rules(four_four=True, overline=True)),
                          "三項各自開關：只禁四四和長連時，三三可以下")

    def test_a_split_three_is_still_a_live_three(self):
        # G8 _ J8 加上 H8 是「跳三」，再下 I8 就是兩頭都空的四，所以也算活三
        self.assertEqual(self.ban(["G8", "J8", "H6", "H7"]), "三三")

    def test_a_three_that_cannot_become_an_open_four_is_not_counted(self):
        # 假三三：橫向 F8 G8 H8 兩邊被白 E8、K8 擋住，怎麼補都變不成兩頭空的四，只剩直向一個活三
        self.assertIsNone(self.ban(["F8", "G8", "H6", "H7"], ["E8", "K8"]))

    def test_a_three_whose_four_point_is_itself_forbidden_is_not_counted(self):
        # 假三三的另一種：橫向 F8 G8 H8 左邊 D8 被擋，只能補 I8 成活四；但加了 J9 之後 I8 本身是三三禁手，
        # 黑棋不能下 I8，所以橫向不是真活三，H8 只有直向一個活三，可以下。沒有 J9 時 I8 能下，H8 就是三三
        base, white = ["F8", "G8", "H6", "H7", "I6", "I7"], ["D8"]
        self.assertEqual(self.ban(base, white), "三三")
        self.assertIsNone(self.ban(base + ["J9"], white))

    def test_double_open_four_is_forbidden_but_closed_fours_and_four_three_are_not(self):
        # 使用者決定四四只算兩個活四（兩頭都空），不是連珠規則把沖四也算進去的四四
        self.assertEqual(self.ban(["E8", "F8", "G8", "H5", "H6", "H7"]), "四四")
        self.assertIsNone(self.ban(["E8", "F8", "G8", "H5", "H6", "H7"], ["D8", "H4"]), "兩個沖四不算")
        self.assertIsNone(self.ban(["E8", "F8", "G8", "H6", "H7"]), "四三不是禁手")

    def test_overline_neither_wins_nor_may_be_played_when_banned(self):
        b = board_with(["C8", "D8", "E8", "F8", "H8"])
        self.assertEqual(engine.forbidden(b, *at("G8"), BLACK, ALL_BANS), "長連")
        self.assertFalse(engine.makes_five(b, *at("G8"), BLACK, ALL_BANS))
        self.assertTrue(engine.makes_five(b, *at("G8"), BLACK), "沒禁長連時照自由規則，六子也算贏")
        self.assertNotIn(at("G8"), engine.five_points(b, BLACK, ALL_BANS))

    def test_white_overline_still_wins(self):
        b = board_with(white=["C8", "D8", "E8", "F8", "H8"])
        self.assertTrue(engine.makes_five(b, *at("G8"), WHITE, ALL_BANS))

    def test_an_exact_five_wins_even_if_it_also_makes_a_forbidden_shape(self):
        # 連五優先（使用者決定）：G8 連成剛好五子，同時也做出兩個活三，算黑棋贏，不是禁手
        b = board_with(["C8", "D8", "E8", "F8", "G6", "G7", "H9", "I10"])
        self.assertIsNone(engine.forbidden(b, *at("G8"), BLACK, ALL_BANS))
        self.assertTrue(engine.makes_five(b, *at("G8"), BLACK, ALL_BANS))


class ForbiddenPlayTests(SimpleTestCase):
    """禁手要由伺服器擋下：真人、AI Agent、引擎代下都不能下出禁手；沒有禁手設定的舊棋局照原規則重播。"""

    def test_the_server_refuses_a_forbidden_move_but_old_games_still_replay(self):
        state = MODULE.replay(BAN_SETTINGS, THREE_THREE)
        with self.assertRaisesRegex(ValueError, "禁手（三三）"):
            MODULE.apply(state, "H8")
        MODULE.replay({}, THREE_THREE + ["H8"])  # 舊棋局沒有設定＝自由規則，這一手合法

    def test_settings_default_to_freestyle_and_reject_unknown_values(self):
        self.assertEqual(engine.Rules.of(MODULE.clean_settings({})), engine.FREESTYLE)
        with self.assertRaises(ValueError):
            MODULE.clean_settings({"three_three": "maybe"})

    def test_the_board_marks_forbidden_points_only_on_blacks_turn(self):
        # 網頁不算規則：禁止符號的位置一定要由伺服器給
        self.assertIn("H8", MODULE.frame(MODULE.replay(BAN_SETTINGS, THREE_THREE), BLACK)["forbidden"])
        self.assertNotIn("forbidden", MODULE.frame(MODULE.replay(BAN_SETTINGS, THREE_THREE[:-1]), WHITE))
        self.assertNotIn("forbidden", MODULE.frame(MODULE.replay({}, THREE_THREE), BLACK))

    def test_the_agent_may_not_play_a_forbidden_point(self):
        state = MODULE.replay(BAN_SETTINGS, THREE_THREE)
        self.assertIn("禁手", MODULE.rejection(state, "H8"))
        banned = list_candidates(state.board, BLACK, state.rules)[-1]  # 候選清單也告訴 Agent 哪些點不能下
        self.assertEqual(banned[0], RULE)
        self.assertIn("H8", banned[1])
        self.assertNotIn(at("H8"), engine.ranked_candidates(state.board, BLACK, state.rules))

    def test_black_is_not_forced_to_block_on_its_own_forbidden_point(self):
        # 白棋衝四、唯一的擋點是黑棋的禁手：黑棋擋不住。硬規則若還要求「必須擋住」，Agent 的每一手都會被退回
        state = MODULE.replay(BAN_SETTINGS, FOUR_ON_BAN)
        self.assertIsNone(MODULE.rejection(state, "A2"))
        self.assertIn("禁手", MODULE.rejection(state, "H8"))
        self.assertNotEqual(MODULE.engine_move(state)["action"], "H8")
        decision = engine.choose_move(state.board, BLACK, state.rules)
        self.assertEqual(decision["reason"], "no_defense")
        self.assertTrue(any(p["evidence"] == RULE and "禁手" in p["text"] for p in decision["points"]))

    def test_white_can_win_by_forcing_black_to_block_on_a_forbidden_point(self):
        # 白棋利用禁手取勝：衝四逼黑棋擋在禁手點。自由規則下黑棋擋得住，這不是連續衝四
        before = board_with(["F8", "G8", "F6", "G7", "H7", "H13"], ["H9", "H10", "H11", "A1", "A15"])
        self.assertIsNone(engine.find_vcf(before, WHITE))
        line = engine.find_vcf(before, WHITE, rules=ALL_BANS)
        self.assertEqual(line, [at("H12")])
        self.assertIn("禁手（三三）", engine.vcf_text("白棋", line, before, WHITE, ALL_BANS))
        decision = engine.choose_move(before, WHITE, ALL_BANS)
        self.assertEqual((tuple(decision["move"]), decision["reason"]), (at("H12"), "vcf"))

    def test_review_does_not_blame_black_for_not_blocking_on_a_forbidden_point(self):
        actions = FOUR_ON_BAN + ["A2"]
        moments = MODULE.review(BAN_SETTINGS, actions, BLACK)
        self.assertNotIn(len(actions) - 1, [m["index"] for m in moments])
        # 同一盤照自由規則看，黑棋沒擋 H8 就是關鍵時刻：復盤真的有照這盤的規則判斷
        self.assertIn("missed_block", [m["type"] for m in MODULE.review({}, actions, BLACK)])

    def test_questions_are_answered_under_this_games_rules(self):
        state = MODULE.replay(BAN_SETTINGS, THREE_THREE)
        texts = [p["text"] for p in MODULE.explain_position(state, ["H8"], None)]
        self.assertTrue(any("這盤黑棋有禁手" in t for t in texts), texts)
        self.assertIn("黑棋不能下 H8：這一點是黑棋的禁手（三三）。", texts)
        self.assertNotIn("禁手", "".join(p["text"] for p in MODULE.explain_position(MODULE.replay({}, THREE_THREE), ["H8"], None)))

    def test_engine_playing_both_sides_under_forbidden_rules_is_never_flagged_or_forbidden(self):
        # 和自由規則的同名測試一樣：復盤與下棋用同一套標準，引擎自己對下的每一手都不能被挑錯；
        # 黑棋的每一手也都不能是禁手（build_board 會檢查）。這個開局下滿 120 手，黑棋有 11 手面對禁手點
        # （2026-11-20 量的；空棋盤開局只有 2 手、要 37 秒，[[7, 7], [6, 7]] 一次都沒遇到，所以只留這一個）
        for opening in ([[7, 7], [8, 8]],):
            board, winner = engine.build_board(opening, ALL_BANS)
            moves = list(opening)
            while not winner and len(moves) < 120:
                color = engine.color_to_move(moves)
                x, y = engine.choose_move(board, color, ALL_BANS)["move"]
                self.assertIsNone(engine.forbidden(board, x, y, color, ALL_BANS), f"開局 {opening} 第 {len(moves) + 1} 手")
                board[y][x] = color
                moves.append([x, y])
                winner = color if engine.makes_five(board, x, y, color, ALL_BANS) else None
            engine.build_board(moves, ALL_BANS)
            for color in (BLACK, WHITE):
                flagged = [(m["index"] + 1, m["type"]) for m in engine.review(moves, color, ALL_BANS)
                           if m["index"] >= len(opening)]
                self.assertEqual(flagged, [], f"開局 {opening}，{engine.COLOR_NAMES[color]}被誤判的手數")


@override_settings(OLLAMA_URL="http://127.0.0.1:9", OLLAMA_TIMEOUT=2)
class ForbiddenApiTests(TestCase):
    client_class = AdminClient

    def post(self, url, body):
        return self.client.post(url, json.dumps(body), content_type="application/json")

    def test_the_setting_is_saved_with_the_game_and_validated(self):
        r = self.post("/api/gomoku/games/", {"seat": WHITE, "settings": {"three_three": "ban"}})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Game.objects.get(pk=r.json()["game"]["id"]).settings["three_three"], "ban")
        self.assertEqual(self.post("/api/gomoku/games/", {"seat": BLACK, "settings": {"overline": "x"}}).status_code, 400)

    def test_a_forbidden_move_is_refused_and_not_saved(self):
        game = Game.objects.create(game_type="gomoku", human_seat=BLACK, actions=THREE_THREE, settings=BAN_SETTINGS)
        r = self.post(f"/api/gomoku/games/{game.id}/move/", {"action": "H8"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(Game.objects.get(pk=game.id).actions, THREE_THREE)
