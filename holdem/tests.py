import json
import random
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings

from arena import agent
from arena.base import CONDITIONAL
from arena.models import AiNote, Game

from . import cards
from .module import EQUITY_FILE, MODULE, HoldemModule
from .rules import HUMAN, Table


def settings_for(players=2, stack=1000, seed=1, ai_mode="agent_bots"):
    return {"players": players, "ai_mode": ai_mode, "stack": stack, "sb": 10, "bb": 20, "secret": {"seed": seed}}


def find_seed(players, test, limit=3000):
    """找一個第一手牌符合條件的種子（牌序由種子決定，測試要的牌型用找的，不改內部狀態）。"""
    for seed in range(limit):
        t = MODULE.new_state(settings_for(players, seed=seed))
        if test(t):
            return seed, t
    raise AssertionError("找不到符合條件的種子")


def fake_chat(*replies):
    queue = list(replies)

    def chat(messages, config, tools):
        return {"content": "", "tool_calls": [{"function": {"name": n, "arguments": a}} for n, a in queue.pop(0)]}
    return chat


class CardTests(SimpleTestCase):
    def test_hand_categories_rank_in_order(self):
        hands = [
            "As Kd 9c 7h 4s 3d 2c",  # 高牌
            "As Ad 9c 7h 4s 3d 2c",  # 一對
            "As Ad 9c 9h 4s 3d 2c",  # 兩對
            "As Ad Ac 7h 4s 3d 2c",  # 三條
            "5s 4d 3c 2h As Kd 9c",  # 順子（A2345，最小的順子）
            "Ks Qs 9s 7s 2s 3d 4c",  # 同花
            "As Ad Ac 7h 7s 3d 2c",  # 葫蘆
            "As Ad Ac Ah 4s 3d 2c",  # 四條
            "5h 4h 3h 2h Ah Kd 9c",  # 同花順
        ]
        ranks = [cards.evaluate(h.split()) for h in hands]
        self.assertEqual([r[0] for r in ranks], list(range(9)))
        self.assertEqual(ranks, sorted(ranks))

    def test_short_boards_can_be_evaluated(self):
        # 推估對手範圍時會看公共牌本身（3–4 張）的牌型；兩對或四條的公共牌沒有踢腳，曾讓伺服器出錯（2026-10-11）
        ev = lambda h: cards.evaluate(h.split())
        self.assertEqual(ev("7c 3d 7s 3c"), (2, 7, 3, 0))
        self.assertEqual(ev("9c 9d 9s 9h"), (7, 9, 0))
        self.assertEqual(ev("Kc Kd Ks")[0], 3)
        from .module import fits
        self.assertTrue(fits("bet", ["Ah", "7d"], ["7c", "3d", "7s", "3c"]))  # 葫蘆，牌力來自底牌
        self.assertFalse(fits("bet", ["Qh", "Jd"], ["7c", "3d", "7s", "3c"]))  # 只有公共牌的兩對

    def test_kickers_and_wheel(self):
        ev = lambda h: cards.evaluate(h.split())
        self.assertLess(ev("5s 4d 3c 2h As Kd 9c"), ev("6s 5d 4c 3h 2s Kd 9c"))  # A2345 < 23456
        self.assertGreater(ev("As Ad Kc 7h 4s 3d 2c"), ev("Ah Ac Qc 7d 4h 3s 2d"))  # 同一對比踢腳
        self.assertEqual(ev("As Ad Kc 7h 4s 3d 2c"), ev("Ah Ac Kd 7c 4h 3s 2d"))  # 花色不影響大小
        self.assertEqual(cards.describe(ev("As Ad Ac 7h 7s 3d 2c")), "葫蘆（A 帶 7）")

    def test_aces_against_kings_is_about_82_percent(self):
        # 牌型判斷錯了，所有勝率與復盤都會錯；AA 對 KK 是公認的約 82%
        rng = random.Random(3)
        deck = [c for c in cards.DECK if c not in ("As", "Ah", "Kc", "Kd")]
        wins = 0.0
        for _ in range(4000):
            board = rng.sample(deck, 5)
            a, k = cards.evaluate(["As", "Ah"] + board), cards.evaluate(["Kc", "Kd"] + board)
            wins += 1 if a > k else 0.5 if a == k else 0
        self.assertAlmostEqual(wins / 4000, 0.82, delta=0.02)

    def test_equity_depends_on_the_assumed_range(self):
        # 勝率是「在假設下」的結果：對手範圍越緊，同一手牌的勝率越低，所以假設一定要列出來
        loose = cards.equity(["As", "Kd"], [], [1.0], 3000, random.Random(1))
        tight = cards.equity(["As", "Kd"], [], [0.05], 3000, random.Random(1))
        self.assertAlmostEqual(cards.equity(["As", "Ah"], [], [1.0], 4000, random.Random(2)), 0.85, delta=0.02)
        self.assertGreater(loose, tight + 0.1)


class RuleTests(SimpleTestCase):
    def test_heads_up_button_posts_small_blind_and_acts_first_only_preflop(self):
        t = Table(2, 1000, 10, 20, 4)
        other = 3 - t.button
        self.assertEqual((t.sb_seat, t.bb_seat, t.to_act), (t.button, other, t.button))
        t.apply("call")
        t.apply("check")
        self.assertEqual((len(t.board), t.to_act), (3, other))

    def test_minimum_raise_and_stack_cap(self):
        t = Table(3, 1000, 10, 20, 4)
        self.assertEqual(t.legal()["raise"], (40, 1000))
        for bad in ("raise 30", "raise 1001"):
            with self.assertRaisesRegex(ValueError, "40 到 1000"):
                t.copy().apply(bad)

    def test_short_all_in_does_not_reopen_raising(self):
        # 不足額的全下只讓已行動的人補齊，不能再加注（否則籌碼少的人可以替別人重開下注）
        t = Table(3, 1000, 10, 20, 4)
        first, second, third = t.to_act, t.next_seat(t.to_act, t.seats), None
        third = t.next_seat(second, t.seats)
        t.stacks[third] = 130  # 已下大盲 20，全下總共 150：只比 100 多 50，不到最小加注 80
        t.apply("raise 100")
        t.apply("call")
        t.apply("allin")
        self.assertEqual((t.to_act, t.current), (first, 150))
        legal = t.legal()
        self.assertEqual((legal["call"], legal["raise"], legal["allin"]), (True, None, False))
        with self.assertRaises(ValueError):
            t.copy().apply("raise 300")

    def test_side_pots_follow_each_players_contribution(self):
        t = Table(3, 1000, 10, 20, 4)
        t.total = {1: 100, 2: 300, 3: 300}
        t.folded = set()
        self.assertEqual(t.side_pots(), [(300, [1, 2, 3]), (400, [2, 3])])
        t.folded = {3}  # 棄牌的人投入的籌碼還在池裡，但不能贏
        self.assertEqual(t.side_pots(), [(300, [1, 2]), (400, [2])])

    def test_short_stack_can_only_win_the_main_pot(self):
        t = Table(3, 1000, 10, 20, 4)
        t.hole = {1: ["As", "Ah"], 2: ["Ks", "Kh"], 3: ["7c", "2d"]}
        t.board = ["Ad", "Kd", "9c", "5h", "3s"]
        t.total, t.stacks, t.folded = {1: 100, 2: 300, 3: 300}, {1: 0, 2: 0, 3: 0}, set()
        t._showdown()
        self.assertEqual(t.winnings, {1: 300, 2: 400})
        self.assertIn("玩家（1 號位）以三條 A 贏得 300", t.hand_result)

    def test_split_pot_gives_the_odd_chip_left_of_the_button(self):
        t = Table(3, 1000, 10, 20, 4)
        t.button = 3
        t.hole = {1: ["2c", "3d"], 2: ["2d", "3c"], 3: ["7c", "8d"]}
        t.board = ["As", "Ks", "Qh", "Jd", "Tc"]  # 公共牌就是順子，三人平分
        t.total, t.stacks, t.folded = {1: 101, 2: 100, 3: 0}, {1: 0, 2: 0, 3: 0}, {3}
        t._showdown()
        self.assertEqual(t.winnings, {1: 101, 2: 100})

    def test_two_to_ten_players_and_a_full_table_plays_out(self):
        # 使用者要求 2 到 10 人（含玩家）；10 人桌要發得出 20 張不重複的底牌，打到攤牌籌碼不能多也不能少
        for bad in (1, 11):
            with self.assertRaisesRegex(ValueError, "2 到 10 人"):
                MODULE.clean_settings({"players": bad})
        t = MODULE.new_state(settings_for(10, seed=3))
        dealt = [c for hole in t.hole.values() for c in hole]
        self.assertEqual((len(set(dealt)), len(t.roles)), (20, 10))
        while not t.hand_over:
            t = MODULE.apply(t, "check" if t.legal()["check"] else "call")
        self.assertEqual(len(set(dealt + t.board)), 25)
        self.assertEqual(sum(t.stacks.values()), 10 * 1000)

    def test_leaving_between_hands_takes_the_current_chips(self):
        # 使用者要求可以離場：帶走目前的籌碼、牌局結束，之後不能再有任何行動
        t = Table(2, 1000, 10, 20, 4)
        t.apply("fold")  # 先行動的人棄牌，這手牌結束
        chips = t.stacks[HUMAN]
        t.apply("leave")
        winner, text = t.result()
        self.assertIn(f"帶走 {chips} 籌碼（起始 1000，", text)
        self.assertEqual(winner, HUMAN if chips > 1000 else 3 - HUMAN)
        with self.assertRaisesRegex(ValueError, "已經離場"):
            t.copy().apply("deal")

    def test_leaving_mid_hand_folds_and_forfeits_chips_already_bet(self):
        seed = next(s for s in range(100) if Table(3, 1000, 10, 20, s).to_act != HUMAN
                    and Table(3, 1000, 10, 20, s).bb_seat == HUMAN)
        t = Table(3, 1000, 10, 20, seed)
        with self.assertRaisesRegex(ValueError, "輪到你"):
            t.copy().apply("leave")  # 還沒輪到玩家
        while t.to_act != HUMAN:
            t.apply("call")
        t.apply("leave")
        self.assertIn("帶走 980 籌碼（起始 1000，輸 20）", t.result()[1])  # 大盲 20 留在底池

    def test_session_ends_when_the_player_busts(self):
        t = MODULE.new_state(settings_for(3))
        t.hand_over, t.stacks = True, {1: 0, 2: 1700, 3: 1300}
        self.assertEqual(MODULE.outcome(t), (2, False))


class EquityTableTests(TestCase):
    def test_table_covers_every_starting_hand_and_ranks_them_sensibly(self):
        # 使用者要求的起手牌勝率表：169 種起手牌都要有，數字要和牌型判斷一致（AA 最強、不同花的小牌最弱）
        ref = MODULE.reference
        self.assertIsNotNone(ref, "沒有 holdem/preflop_equity.json，請跑 manage.py poker_equity_table")
        keys = [r["key"] for r in ref["rows"]]
        self.assertEqual((len(keys), len(set(keys))), (169, 169))
        self.assertEqual(keys[0], "AA")
        self.assertIn(keys[-1], ("32o", "42o", "72o", "52o", "62o", "43o"))
        data = json.loads(EQUITY_FILE.read_text(encoding="utf-8"))
        aa = next(r for r in data["rows"] if r["hand"] == "AA")
        self.assertAlmostEqual(aa["win"], 0.85, delta=0.01)  # 公認約 85%
        self.assertTrue(all(r["win"] + r["tie"] <= 1 for r in data["rows"]))

    def test_only_poker_pages_show_the_table(self):
        self.assertIn('id="reference-panel"', self.client.get("/holdem/").content.decode())
        self.assertNotIn('id="reference-panel"', self.client.get("/gomoku/").content.decode())


class HiddenInformationTests(SimpleTestCase):
    def test_frame_hides_other_hole_cards_until_showdown(self):
        t = MODULE.new_state(settings_for(6, seed=8))
        frame = json.dumps(MODULE.frame(t, HUMAN))
        for seat, hole in t.hole.items():
            for c in hole:
                self.assertEqual(f'"{c}"' in frame, seat == HUMAN, (seat, c))
        # 牌型也是底牌資訊：攤牌前只有自己的座位有
        names = {s["seat"]: s["hand_name"] for s in MODULE.frame(t, HUMAN)["seats"]}
        self.assertTrue(names[HUMAN])
        self.assertFalse(any(name for seat, name in names.items() if seat != HUMAN))
        while not t.hand_over:  # 大家都跟注或過牌到攤牌
            t = MODULE.apply(t, "check" if t.legal()["check"] else "call")
        shown = {s["seat"]: s["cards"] for s in MODULE.frame(t, HUMAN)["seats"]}
        self.assertEqual(shown, t.hole)

    def test_agent_tools_do_not_depend_on_opponents_hole_cards(self):
        # 分析只能用決策當下看得到的資訊：把對手的底牌換掉，Agent 查到的事實必須一模一樣
        seed, t = find_seed(3, lambda t: t.to_act != HUMAN)
        swapped = t.copy()
        others = [s for s in t.hole if s != t.to_act]
        spare = [c for c in t.deck if c not in t.deck[:5]]
        swapped.hole = {**t.hole, **{s: spare[2 * i:2 * i + 2] for i, s in enumerate(others)}}
        for tool in ("find_threats", "list_candidates"):
            a = HoldemModule().run_tool(t, tool, {})
            b = HoldemModule().run_tool(swapped, tool, {})
            self.assertEqual(a, b, tool)


def flop_after_limp(seed):
    """單挑、對手在按鈕位跛入、玩家過牌看翻牌之後的局面（翻牌後玩家先行動）。"""
    t = MODULE.new_state(settings_for(2, seed=seed))
    if t.button == HUMAN:
        return None
    return MODULE.apply(MODULE.apply(t, "call"), "check")


def flop_seed(test, limit=400):
    for seed in range(limit):
        t = flop_after_limp(seed)
        if t and test(t):
            return seed, t
    raise AssertionError("找不到符合條件的種子")


class ReviewTests(SimpleTestCase):
    def test_folding_aces_to_a_raise_is_a_key_moment_with_the_reminder(self):
        seed, t = find_seed(2, lambda t: t.button != HUMAN and cards.top_fraction(*t.hole[HUMAN]) <= 0.01)
        moments = MODULE.review(settings_for(2, seed=seed), ["raise 60", "fold"], HUMAN)
        self.assertEqual([(m["index"], m["played"], m["level"]) for m in moments], [(1, "fold", "key")])
        texts = [p["text"] for p in moments[0]["points"]]
        self.assertTrue(any("單手輸贏不等於決策好壞" in x for x in texts))
        self.assertTrue(any(p["evidence"] == CONDITIONAL and "假設" in p["text"] for p in moments[0]["points"]))
        self.assertNotEqual(moments[0]["best"], "fold")

    def test_calling_an_all_in_with_trash_is_a_key_moment(self):
        seed, t = find_seed(2, lambda t: t.button != HUMAN and cards.top_fraction(*t.hole[HUMAN]) >= 0.9)
        moments = MODULE.review(settings_for(2, seed=seed), ["allin", "call"], HUMAN)
        self.assertEqual([(m["played"], m["best"], m["level"]) for m in moments], [("call", "fold", "key")])

    def test_all_in_bluff_with_trash_is_evaluated(self):
        # 使用者要求加注與 All in 也要評估：拿爛牌對加注者 All in，對手只要用最強的牌跟，就虧很多
        seed, t = find_seed(2, lambda t: t.button != HUMAN and cards.top_fraction(*t.hole[HUMAN]) >= 0.9)
        moments = MODULE.review(settings_for(2, seed=seed), ["raise 60", "allin"], HUMAN)
        self.assertEqual([(m["played"], m["best"], m["title"]) for m in moments], [("allin", "fold", "All in 風險太高")])

    def test_checking_a_monster_misses_value(self):
        # 牌力領先時只過牌，對手不用付錢；要列出「少賺了」，建議下注讓對手多付
        seed, t = flop_seed(lambda t: cards.evaluate(t.hole[HUMAN] + t.board)[0] >= 3)
        moments = MODULE.review(settings_for(2, seed=seed), ["call", "check", "check"], HUMAN)
        self.assertEqual(len(moments), 1)
        m = moments[0]
        self.assertEqual((m["index"], m["played"]), (2, "check"))
        self.assertIn("少賺", m["title"])
        self.assertTrue(m["best"].startswith(("raise", "allin")))

    def test_reading_a_raise_turns_a_fold_into_a_precise_stop_loss(self):
        # 使用者要求依對手的動作判斷是否比你大：中等的一對，被對手加注後勝率要下降，這時棄牌要記為精準止損
        def middle_pair(t):
            mine = cards.evaluate(t.hole[HUMAN] + t.board)
            return (mine[0] == 1 and mine[0] > cards.evaluate(t.board)[0]
                    and mine[1] < max(cards.value(c) for c in t.board))
        for seed in range(400):
            t = flop_after_limp(seed)
            if not (t and middle_pair(t)):
                continue
            t = MODULE.apply(MODULE.apply(t, "raise 40"), "raise 160")
            naive_equity = MODULE._naive_call(t)[0]
            read_equity = MODULE.analysis(t, HUMAN)["equity"]
            moments = MODULE.review(settings_for(2, seed=seed), ["call", "check", "raise 40", "raise 160", "fold"], HUMAN)
            if moments and moments[-1]["level"] == "good":
                self.assertLess(read_equity, naive_equity)
                self.assertEqual((moments[-1]["index"], moments[-1]["title"]), (4, "精準止損：對手的動作顯示牌可能比你大"))
                return
        self.fail("中等的一對被加注後，從來沒有被記為精準止損")

    def test_a_small_difference_is_a_suggestion_not_a_mistake(self):
        # 使用者要求「建議」：差距比小失誤門檻小（但超過模擬誤差）時，不說你錯，只推薦更好的打法
        seed, t = flop_seed(lambda t: True)
        fake = {"equity": 0.6, "pot": 200, "to_call": 0, "legal": t.legal(HUMAN), "best": "raise 100",
                "ev": {"check": 100.0, "raise 100": 107.0}, "pot_odds": 0.0, "ev_call": 0.0, "fair": 0.5}
        with patch.object(MODULE, "analysis", return_value=fake):
            m = MODULE._judge(t, 2, "check")
        self.assertEqual((m["level"], m["title"]), ("suggest", "建議：可以加注到 100，讓對手多付一點"))
        self.assertTrue(any("不算失誤" in p["text"] for p in m["points"]))
        fake["ev"]["raise 100"] = 103.0  # 只差 3：在模擬誤差內，什麼都不列
        with patch.object(MODULE, "analysis", return_value=fake):
            self.assertIsNone(MODULE._judge(t, 2, "check"))

    def test_overall_comment_counts_the_players_style(self):
        # 整體評論的打法統計：主動入池、翻牌前加注、攤牌、贏下的手數都要照紀錄算對
        seed, t = find_seed(2, lambda t: t.button == HUMAN)  # 單挑：玩家是按鈕，翻牌前先說話
        s = settings_for(2, seed=seed)
        text = " ".join(x for _, x in MODULE.overall_points(s, ["raise 60", "fold", "deal"], HUMAN))
        self.assertIn("你打完了 1 手牌：主動入池（翻牌前跟注或加注）1 手（100%），翻牌前加注 1 手（100%），打到攤牌 0 手，贏下 1 手。", text)
        self.assertIn("籌碼：起始 1000，現在 1020（+20）。", text)
        # 好壞由程式依人數判斷後寫進事實，不交給語言模型自己評價
        self.assertIn("2 人桌一般建議主動入池約 50%–80%（經驗值，不是這盤算出來的）；你這盤 100%，偏鬆（玩太多手）。", text)

    def test_every_problem_is_listed_with_a_level(self):
        # 使用者要求問題全部列出（不再只列 5 個），分成關鍵時刻與小失誤
        s = settings_for(6, seed=5)
        t, actions = MODULE.new_state(s), []
        while t.hand_no <= 6 and not MODULE.outcome(t):
            if t.hand_over:
                action = "deal"
            elif t.to_act == HUMAN:  # 玩家一律跟注或過牌：該加注、該棄牌的時候都會出問題
                action = "check" if t.legal()["check"] else "call"
            else:
                action = (MODULE.forced(t) or MODULE.engine_move(t))["action"]
            t = MODULE.apply(t, action)
            actions.append(action)
        moments = MODULE.review(s, actions, HUMAN)
        self.assertGreater(len(moments), 5)
        self.assertTrue({m["level"] for m in moments} <= {"key", "minor", "suggest", "good"})
        self.assertEqual([m["index"] for m in moments], sorted(m["index"] for m in moments))

    def test_engine_playing_every_seat_is_never_flagged(self):
        # 問題檢查的標準就是引擎的決策標準；引擎替玩家做的決定不能被挑錯
        s = settings_for(4, seed=11)
        t, actions = MODULE.new_state(s), []
        while t.hand_no <= 3 and not MODULE.outcome(t):
            if t.hand_over:
                action = "deal"
            else:
                action = (MODULE.forced(t) or MODULE.engine_move(t))["action"]
            t = MODULE.apply(t, action)
            actions.append(action)
        self.assertGreater(actions.count("fold") + actions.count("call"), 3)
        self.assertEqual([m for m in MODULE.review(s, actions, HUMAN) if m["level"] != "good"], [])


class AgentTests(SimpleTestCase):
    def sample(self):
        actions = MODULE.benchmark_samples[0]
        return MODULE.replay(MODULE.clean_settings({}), actions), actions

    def test_bots_act_without_the_model_and_the_agent_seat_does_not(self):
        t, _ = self.sample()
        self.assertEqual(t.roles[t.to_act], "agent")
        self.assertIsNone(MODULE.forced(t))
        bot_turn = MODULE.new_state(MODULE.clean_settings({}))
        self.assertIn(bot_turn.roles[bot_turn.to_act], ("tag", "lag", "station", "rock"))
        self.assertIsNotNone(MODULE.forced(bot_turn)["action"])

    def test_agent_and_bots_never_see_the_players_hole_cards(self):
        # 公平：AI 決策時拿到的所有文字（系統提示、局面描述、每一個工具結果）都不能出現玩家的底牌
        for actions in MODULE.benchmark_samples:  # 翻牌前、翻牌、轉牌各一個輪到 Agent 的局面
            t = MODULE.replay(MODULE.clean_settings({}), actions)
            sent = []

            def recording(messages, config, tools, queue=[
                    [("find_threats", {})], [("list_candidates", {})], [("check_move", {"action": "raise 100"})],
                    [("play_move", {"action": "check", "reason": "在這些假設下過牌"})]]):
                sent.extend(str(m.get("content", "")) for m in messages)
                return {"content": "", "tool_calls": [{"function": {"name": n, "arguments": a}} for n, a in queue.pop(0)]}

            with patch("arena.agent._chat", side_effect=recording):
                r = agent.agent_move(MODULE, t, actions, "qwen3:8b", 5)
            text = "\n".join(sent) + json.dumps(r["facts"], ensure_ascii=False)
            self.assertIn(cards.show_all(t.hole[2]), text)  # Agent 看得到自己的牌（確認有記錄到內容）
            for c in t.hole[HUMAN]:
                self.assertNotIn(cards.show(c), text, (actions, c))
        t = MODULE.new_state(MODULE.clean_settings({}))
        while not t.hand_over:
            bot = MODULE.forced(t)
            if bot:
                facts = " ".join(text for _, text in bot["points"])
                for c in t.hole[HUMAN]:
                    self.assertNotIn(cards.show(c), facts)
            t = MODULE.apply(t, bot["action"] if bot else ("check" if t.legal()["check"] else "call"))

    def test_agent_uses_conditional_facts_and_cannot_fold_a_free_check(self):
        t, actions = self.sample()
        self.assertEqual(MODULE.rejection(t, "fold"), "可以免費過牌，不能棄牌")
        self.assertEqual(MODULE.rejection(t, "leave"), "AI 不能離場")
        with patch("arena.agent._chat", side_effect=fake_chat(
                [("list_candidates", {})], [("play_move", {"action": "fold", "reason": "不玩"})],
                [("play_move", {"action": "check", "reason": "在這些假設下過牌"})])):
            r = agent.agent_move(MODULE, t, actions, "qwen3:8b", 4)
        self.assertEqual((r["action"], r["fallback"]), ("check", False))
        self.assertTrue(any(p["evidence"] == CONDITIONAL for p in r["facts"]["points"]))
        self.assertIn("被退回", r["narration"]["text"])


@override_settings(OLLAMA_URL="http://127.0.0.1:9", OLLAMA_TIMEOUT=2)
class GameApiTests(TestCase):
    def setUp(self):
        # Ollama 不在時 Agent 改由引擎代下；直接讓模型呼叫失敗，不必等 Windows 拒絕連線（每次約 2 秒）
        patcher = patch("arena.agent._chat", side_effect=OSError("模型沒有回應"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def post(self, url, body):
        return self.client.post(url, json.dumps(body), content_type="application/json")

    def start(self, players=6):
        """開一桌、而且第一手牌要等玩家決定（還沒結束），回傳 (回應, 對局)。"""
        for seed in range(30):
            with patch.object(MODULE, "new_secret", return_value={"seed": seed}):
                r = self.post("/api/holdem/games/", {"seat": 1, "settings": {"players": players}})
            if not r.json()["frame"]["hand_over"] and not (r.json()["ai_moves"] and r.json()["ai_moves"][-1]["frame"]["hand_over"]):
                return r, Game.objects.get(pk=r.json()["game"]["id"])
        raise AssertionError("找不到第一手要玩家決定的種子")

    def assert_no_ai_cards(self, body, game):
        state = MODULE.replay({**game.settings, "secret": game.secret}, game.actions)
        for seat, hole in state.hole.items():
            if seat != HUMAN:
                for c in hole:
                    self.assertNotIn(f'"{c}"', body, (seat, c))
                    self.assertNotIn(cards.show(c), body, (seat, c))

    def test_ai_hole_cards_never_reach_the_page_during_the_hand(self):
        r, game = self.start()
        self.assertNotIn("seed", r.content.decode())  # 洗牌種子只在伺服器
        self.assert_no_ai_cards(r.content.decode(), game)
        self.assertTrue(all(m.get("hidden") for m in r.json()["ai_moves"]))
        detail = self.client.get(f"/api/holdem/games/{game.id}/").content.decode()
        self.assert_no_ai_cards(detail, game)
        note = AiNote.objects.filter(game=game).first()
        narrate = self.post(f"/api/holdem/games/{game.id}/notes/{note.index}/narrate/", {})
        self.assertEqual(narrate.status_code, 400)

    def test_notes_become_visible_when_the_hand_ends(self):
        r, game = self.start()
        r = self.post(f"/api/holdem/games/{game.id}/move/", {"action": "fold"}).json()
        self.assertTrue(r["ai_moves"][-1]["frame"]["hand_over"] if r["ai_moves"] else r["frame"]["hand_over"])
        notes = self.client.get(f"/api/holdem/games/{game.id}/").json()["notes"]
        self.assertTrue(notes and not any(n.get("hidden") for n in notes))

    def test_a_session_plays_several_hands_and_saves_legal_actions(self):
        r, game = self.start(players=2)
        frame = r.json()["ai_moves"][-1]["frame"] if r.json()["ai_moves"] else r.json()["frame"]
        for _ in range(40):
            if frame["result"]:
                break
            action = "deal" if frame["hand_over"] else "check" if frame["legal"]["check"] else "call"
            r = self.post(f"/api/holdem/games/{game.id}/move/", {"action": action})
            self.assertEqual(r.status_code, 200, r.content)
            data = r.json()
            frame = data["ai_moves"][-1]["frame"] if data["ai_moves"] else data["frame"]
        game.refresh_from_db()
        self.assertGreaterEqual(game.actions.count("deal"), 2)
        MODULE.replay({**game.settings, "secret": game.secret}, game.actions)

    def test_leaving_ends_the_session_and_shows_all_notes(self):
        r, game = self.start()
        r = self.post(f"/api/holdem/games/{game.id}/move/", {"action": "離場"})
        self.assertEqual(r.status_code, 200, r.content)
        game.refresh_from_db()
        self.assertTrue(game.finished)
        self.assertEqual(game.actions[-1], "leave")
        self.assertIn("你離場，帶走", r.json()["frame"]["result"])
        detail = self.client.get(f"/api/holdem/games/{game.id}/").json()
        self.assertFalse(any(n.get("hidden") for n in detail["notes"]))  # 牌局結束，思路可以完整看
        review = self.client.get(f"/api/holdem/games/{game.id}/review/").json()["moments"]
        self.assertNotIn(len(game.actions) - 1, [m["index"] for m in review])  # 離場本身不評

    def test_illegal_action_is_rejected(self):
        r, game = self.start()
        bad = self.post(f"/api/holdem/games/{game.id}/move/", {"action": "raise 5"})
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(Game.objects.get(pk=game.id).actions, game.actions)
