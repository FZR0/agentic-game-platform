import codecs
import csv
import importlib
import io
import json
import platform
import socket
import tempfile
import urllib.error
import zipfile
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.apps import apps
from django.conf import settings
from django.core.management import call_command
from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.utils import timezone as django_timezone

from gomoku.module import MODULE as GOMOKU
from weiqi.module import MODULE as WEIQI

from . import agent, games, health, narrator, operations, orchestrator
from .base import ESTIMATE
from .management.commands.model_report import summarize
from .models import AiNote, ChatMessage, Game, GamePlayer, LoginRecord, ModelRun, Presence, Profile


class AdminClient(Client):
    """測試用的瀏覽器：第一次送出請求前以管理員登入（2026-11-05 起所有頁面與 API 都要登入）。
    管理員看得到沒有擁有者的舊對局，所以直接用 Game.objects.create 建對局的測試照樣能用。
    要測沒登入或一般帳號的情況，用 django.test.Client 自己登入（AccountTests）。"""

    def request(self, **request):
        if "_auth_user_id" not in self.session:
            user = User.objects.filter(username="tester").first() or User.objects.create_superuser("tester")
            self.force_login(user)
        return super().request(**request)


def poker_table(owner, others=(), ai=(), players=4, seed=None):
    """撲克開放牌桌（2026-11-16）：owner 開房，others 用密碼進來，大家上桌、房主補 AI（ai 裡是 "agent"／"bot"）。
    座位照空位順序坐（真的網站是隨機），測試才固定。還沒按開始。"""
    from holdem.module import MODULE as HOLDEM
    secret = patch.object(HOLDEM, "new_secret", return_value={"seed": seed}) if seed is not None else nullcontext()
    with secret, patch("arena.operations.secrets.choice", side_effect=lambda seats: seats[0]):
        game, _ = operations.start_game(HOLDEM, 1, {"players": players}, owner, "pvp")
        for user in others:
            operations.join_game(game.room_code, user)
        for user in (owner, *others):
            game, _ = operations.table_action(HOLDEM, game, user, "sit")
        for kind in ai:
            game, _ = operations.table_action(HOLDEM, game, owner, "add_ai", kind)
    game.refresh_from_db()
    return game


def start_now(game):
    """房主按「開始」，倒數立刻結束（測試不等三秒）。真人對戰坐滿後要這樣才能下（2026-11-13）。"""
    game.refresh_from_db()
    with patch.object(operations, "START_COUNTDOWN", 0):
        return operations.start_room(game, game.owner)


class NarrationCheckTests(SimpleTestCase):
    FACTS = {"kind": "ai_move", "points": [{"evidence": ESTIMATE, "text": "依引擎估計，H9 的作用：形成活三（橫向）。"}]}

    def test_rejects_coordinates_not_in_facts(self):
        # 語言模型自己編出的棋步不能送到玩家面前。
        self.assertIsNotNone(narrator.check_text("我下在 H9，之後可以接 J11。", self.FACTS, GOMOKU.coord_re))

    def test_rejects_certainty_when_only_estimated(self):
        self.assertIsNotNone(narrator.check_text("H9 一定是最好的一手。", self.FACTS, GOMOKU.coord_re))

    def test_accepts_faithful_text(self):
        self.assertIsNone(narrator.check_text("依引擎估計，我下在 H9 是為了形成橫向活三。", self.FACTS, GOMOKU.coord_re))

    def test_the_solved_wording_needs_a_solved_fact(self):
        # 2026-11-16 情境測試：約 9% 的說明把「在搜尋範圍內已確認」寫在條件式計算或引擎估計上
        # （撲克「在搜尋範圍內已確認第 10 步沒有止損」），玩家會以為那是算到底確認的
        conditional = {"kind": "review", "points": [{"evidence": "條件式計算", "text": "在這些假設下，跟注的期望值約 -19。"},
                                                    {"evidence": "規則確認", "text": "第 1 手牌河牌：底池 80。"}]}
        self.assertIsNotNone(narrator.check_text("在搜尋範圍內已確認跟注划不來。", conditional, GOMOKU.coord_re))
        self.assertIsNone(narrator.check_text("在這些假設下，跟注划不來。", conditional, GOMOKU.coord_re))
        solved = {"kind": "review", "points": [{"evidence": "求解確認", "text": "在搜尋範圍內已確認，黑棋下 H9 可以連續衝四取勝。"}]}
        self.assertIsNone(narrator.check_text("在搜尋範圍內已確認，下 H9 就能連續衝四取勝。", solved, GOMOKU.coord_re))
        # 引擎事實本身的「在搜尋範圍內未發現」是估計的說法，照寫沒問題
        estimate = {"kind": "review", "points": [{"evidence": ESTIMATE, "text": "在搜尋範圍內未發現白棋有連續衝四。"}]}
        self.assertIsNone(narrator.check_text("在搜尋範圍內未發現白棋有連續衝四。", estimate, GOMOKU.coord_re))

    def test_the_overall_prompt_does_not_ask_for_mistakes_that_are_not_there(self):
        # 2026-11-17 使用者回報：提示一律要模型寫「最值得改進的一兩點」，沒有失誤時它就把「建議」說成要檢討的失誤，
        # 還自己下「表現不理想」的評語。沒有失誤就照實說，評語只能用事實裡寫好的
        intro = narrator.INTROS["overall"]
        self.assertIn("有失誤（關鍵時刻、小失誤）才說最值得改進的一兩點", intro)
        self.assertIn("事實寫沒有失誤就照實說沒有失誤", intro)
        self.assertIn("「建議」不是失誤", intro)
        # 只點名「穩定」時，模型改說「精準」「良好」（2026-11-17 真模型重現），所以要求不用任何形容表現好壞的詞
        self.assertIn("不要用任何形容整體表現好壞的詞", intro)

    def test_a_hedge_is_not_a_certainty(self):
        # 2026-11-16 情境測試：對手觀察的建議寫「他不一定找得到殺法」，模型照著說卻被當成「一定」擋下。
        # 「不一定」正是估計該有的保留說法；「一定」照樣要擋
        self.assertIsNone(narrator.check_text("依引擎估計，H9 不一定是最好的一手。", self.FACTS, GOMOKU.coord_re))
        self.assertIsNotNone(narrator.check_text("H9 不錯，一定能贏。", self.FACTS, GOMOKU.coord_re))

    def test_thinking_only_model_is_called_with_thinking_on(self):
        # 只能思考的模型（例如原開發機的 qwen3:4b）關掉思考時會把推理過程寫進回答，所以必須開啟並留足生成長度。
        thinking_only = {"name": "thinking-only:4b", "label": "思考版", "think": True, "agent": False}
        body = narrator.request_body(self.FACTS, thinking_only, GOMOKU.name)
        self.assertTrue(body["think"])
        self.assertGreater(body["options"]["num_predict"], 1000)
        self.assertFalse(narrator.request_body(self.FACTS, narrator.model_config("qwen3:8b"), GOMOKU.name)["think"])

    def test_prompt_names_the_game(self):
        # 同一個說明模組服務所有遊戲；模型必須知道自己在講哪個遊戲。
        body = narrator.request_body(self.FACTS, narrator.model_config(), GOMOKU.name)
        self.assertTrue(body["messages"][0]["content"].startswith("你是五子棋教練"))

    def test_key_point_is_split_from_explanation(self):
        # 「重點」要單獨醒目顯示；模型常加 Markdown 或把兩段寫在同一行，都要拆得出來。
        cases = {
            "重點：要擋 I5。\n說明：白棋在 I5 能成活四。": ("要擋 I5。", "白棋在 I5 能成活四。"),
            "重點：要擋 I5。說明：白棋在 I5 能成活四。": ("要擋 I5。", "白棋在 I5 能成活四。"),
            "## 重點：要擋 I5。\n- 說明：白棋在 I5 能成活四。": ("要擋 I5。", "白棋在 I5 能成活四。"),
            "重點：要擋 I5。": ("要擋 I5。", ""),
            "白棋在 I5 能成活四。": (None, "白棋在 I5 能成活四。"),
        }
        for text, expected in cases.items():
            self.assertEqual(narrator.split_key_point(text), expected, text)

    def test_ask_prompt_puts_the_players_question_first(self):
        # 回答要針對玩家問的事，不能只是把引擎事實從頭唸一遍。
        facts = {**self.FACTS, "kind": "ask", "question": "為什麼我常看不到 H9？"}
        prompt = narrator.request_body(facts, narrator.model_config(), GOMOKU.name)["messages"][1]["content"]
        self.assertTrue(prompt.startswith("玩家的問題：「為什麼我常看不到 H9？」"))
        self.assertIn("正面回應", prompt)

    def test_unknown_model_is_rejected(self):
        with self.assertRaises(ValueError):
            narrator.model_config("not-a-model:1b")

    @override_settings(OLLAMA_URL="http://127.0.0.1:9", OLLAMA_TIMEOUT=2)
    def test_falls_back_to_facts_when_ollama_is_unreachable(self):
        # 沒有 Ollama 時仍要能說明，而且內容就是引擎的事實原文。
        result = narrator.narrate(self.FACTS, None, GOMOKU)
        self.assertEqual(result["source"], "template")
        self.assertEqual(result["text"], self.FACTS["points"][0]["text"])


class OllamaRetryTests(SimpleTestCase):
    # 研究用伺服器 2026-10-11 卡住兩次：每次呼叫都等滿 OLLAMA_TIMEOUT 才逾時，Agent 一手要呼叫好幾次，遊戲看起來像當掉。
    # 只用遠端之後沒有本機可以備援，所以連不上或逾時一次後，接下來一段時間要直接改用引擎。
    BODY = {"model": "qwen3:8b", "messages": []}

    def setUp(self):
        narrator._retry_at = 0.0  # 其他測試連 127.0.0.1:9 被拒絕時也會觸發暫停
        self.addCleanup(setattr, narrator, "_retry_at", 0.0)
        # 逾時後的原因檢查（health）會再連一次埠；這裡只測暫停，不讓它真的連線
        connect = patch("arena.health._connect", return_value=("failed", "連不上（timed out）"))
        connect.start()
        self.addCleanup(connect.stop)

    def test_hung_server_makes_only_the_first_agent_move_wait(self):
        actions = GOMOKU.benchmark_samples[0]
        state = GOMOKU.replay(GOMOKU.clean_settings({}), actions)
        with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")) as urlopen:
            moves = [agent.agent_move(GOMOKU, state, actions, "qwen3:8b", 3) for _ in range(3)]
        self.assertEqual(urlopen.call_count, 1)
        for r in moves:
            self.assertTrue(r["fallback"])
            # 測速靠這個字樣停下來，不把「呼叫不到」當成模型的表現
            self.assertIn(agent.UNREACHABLE, r["facts"]["points"][0]["text"])

    def test_server_is_tried_again_after_the_wait(self):
        clock = [1000.0]
        with patch("time.monotonic", side_effect=lambda: clock[0]), \
                patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")) as urlopen:
            with self.assertRaises(OSError):
                narrator.chat(self.BODY)
            clock[0] += settings.OLLAMA_RETRY_AFTER - 1
            with self.assertRaises(OSError):
                narrator.chat(self.BODY)
            self.assertEqual(urlopen.call_count, 1)
            clock[0] += 2  # 伺服器修好後要能自動恢復，不必重新啟動網站
            with self.assertRaises(OSError):
                narrator.chat(self.BODY)
            self.assertEqual(urlopen.call_count, 2)

    def test_an_absent_server_costs_only_the_connect_timeout(self):
        # 伺服器不在（例如沒連學校 VPN）時，Windows 要等約 21 秒才回報連不上，每個頁面都卡那麼久（2026-11-07）。
        # 建立連線只能等 OLLAMA_CONNECT_TIMEOUT；連上之後等回答仍是 OLLAMA_TIMEOUT，模型第一次載入要 40 秒以上。
        self.assertLess(settings.OLLAMA_CONNECT_TIMEOUT, 10)
        with patch("socket.create_connection", side_effect=TimeoutError("timed out")) as connect:
            with self.assertRaises(OSError):
                urllib.request.urlopen("http://example.invalid:11434/api/tags", timeout=120)
        self.assertEqual(connect.call_args.args[1], settings.OLLAMA_CONNECT_TIMEOUT)
        sock = MagicMock()
        with patch("socket.create_connection", return_value=sock):
            conn = narrator._QuickConnection("example.invalid", 11434, timeout=120)
            conn.connect()
        sock.settimeout.assert_called_with(120)

    def test_error_reply_from_the_server_does_not_pause_calls(self):
        # 伺服器有回應（例如選的模型不存在）不是卡住；暫停的話，換別的模型也要白等好幾分鐘。
        error = urllib.error.HTTPError(f"{settings.OLLAMA_URL}/api/chat", 404, "model not found", None, None)
        with patch("urllib.request.urlopen", side_effect=error) as urlopen:
            for _ in range(2):
                with self.assertRaises(urllib.error.HTTPError):
                    narrator.chat(self.BODY)
        self.assertEqual(urlopen.call_count, 2)


class NumCtxTests(SimpleTestCase):
    # 共用伺服器不指定 num_ctx 時會用很大的預設值，把記憶體吃光後整台當掉；任何送出的請求都不能漏掉它。
    FACTS = NarrationCheckTests.FACTS

    class _Reply:
        def __init__(self, body):
            self.body = body

        def read(self, *a):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def close(self):
            pass

    def setUp(self):
        narrator._retry_at = 0.0  # 其他測試觸發的暫停會讓請求根本不送出
        self.addCleanup(setattr, narrator, "_retry_at", 0.0)

    def sent_bodies(self, run):
        """執行 run，回傳它送給 Ollama 的所有請求本文（不真的連線）。"""
        sent = []

        def fake_urlopen(req, timeout=None):
            if isinstance(req, str) or req.data is None:  # GET，例如 /api/ps
                return self._Reply(b'{"models": [{"name": "qwen3:8b"}]}')
            sent.append(json.loads(req.data))
            return self._Reply(b'{"message": {"content": "ok"}}')

        with patch("urllib.request.urlopen", fake_urlopen):
            run()
        return sent

    @override_settings(OLLAMA_NUM_CTX=4096)
    def test_narration_request_carries_num_ctx(self):
        sent = self.sent_bodies(lambda: narrator._ask_ollama(self.FACTS, narrator.model_config(), GOMOKU.name))
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["options"]["num_ctx"], 4096)

    @override_settings(OLLAMA_NUM_CTX=4096)
    def test_agent_request_carries_num_ctx(self):
        sent = self.sent_bodies(lambda: agent._chat([], narrator.model_config(), []))
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["options"]["num_ctx"], 4096)

    @override_settings(OLLAMA_NUM_CTX=4096)
    def test_request_agent_carries_num_ctx(self):
        sent = self.sent_bodies(lambda: orchestrator._chat([], []))
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["options"]["num_ctx"], 4096)

    @override_settings(OLLAMA_NUM_CTX=4096)
    def test_benchmark_unload_request_carries_num_ctx(self):
        from .management.commands import benchmark_models
        sent = self.sent_bodies(lambda: benchmark_models._unload("qwen3:8b"))
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["options"]["num_ctx"], 4096)

    @override_settings(OLLAMA_NUM_CTX=4096)
    def test_answers_to_questions_may_explain_the_reasons_at_length(self):
        # 2026-11-18 使用者：AI 教練回答問題「沒有詳細說明只是一昧著依照引擎」。提問的說明放寬到 3 到 5 句、
        # 要求講出因果，生成長度也跟著加大，否則說到一半被截斷；其他說明維持原本的長度
        ask = {"kind": "ask", "question": "為什麼 H9 不好？", "points": self.FACTS["points"]}
        sent = self.sent_bodies(lambda: narrator._ask_ollama(ask, narrator.model_config(), GOMOKU.name))
        self.assertEqual(sent[0]["options"]["num_ctx"], 4096)
        self.assertEqual(sent[0]["options"]["num_predict"], narrator.ASK_NUM_PREDICT)
        self.assertIn("3 到 5 句", sent[0]["messages"][0]["content"])
        self.assertIn("後續變化", sent[0]["messages"][1]["content"])
        sent = self.sent_bodies(lambda: narrator._ask_ollama(self.FACTS, narrator.model_config(), GOMOKU.name))
        self.assertEqual(sent[0]["options"]["num_predict"], narrator.NUM_PREDICT[False])
        self.assertIn("1 到 3 句", sent[0]["messages"][0]["content"])

    def test_default_is_8192(self):
        self.assertEqual(settings.OLLAMA_NUM_CTX, 8192)  # 沒設環境變數 OLLAMA_NUM_CTX 時


class TunnelProtocolTests(SimpleTestCase):
    """連著學校 VPN 時，cloudflared 預設的 QUIC（UDP）一直斷線重連、從沒連上 Cloudflare，朋友打開網址只看到
    Error 1033；TCP 是通的（2026-11-10）。所以 VPN 版的通道要走 TCP，本機 AI 版維持預設（使用者決定）。
    不真的開通道：開通道會把這台電腦公開到網路上，只由使用者自己啟動。"""

    def tunnel_command(self, **kwargs):
        from tools import start_tunnel
        started = []

        class Group:
            def start(self, cmd, **_):
                started.append(cmd)
                return SimpleNamespace(stderr=iter(["|  https://abc-def.trycloudflare.com  |\n"]))

        with patch.object(start_tunnel, "wait_until", return_value=True), \
                patch("builtins.input", side_effect=EOFError), patch("sys.stdout", new_callable=StringIO):
            self.assertEqual(start_tunnel.run(Group(), 8010, open_browser=False, **kwargs), 0)
        return next(cmd for cmd in started if "tunnel" in cmd)

    def test_vpn_launcher_tunnel_goes_over_tcp(self):
        cmd = self.tunnel_command(over_tcp=True)
        self.assertEqual(cmd[cmd.index("--protocol") + 1], "http2")
        self.assertEqual(cmd[-2:], ["--url", "http://127.0.0.1:8010"])

    def test_local_ai_launcher_keeps_the_default_protocol(self):
        self.assertNotIn("--protocol", self.tunnel_command())


class EngineSetupTests(SimpleTestCase):
    # 新電腦部署時由 tools/setup_engines.py 下載遊戲引擎（2026-10-15）；這些是會直接在使用者電腦上執行的程式。

    def test_download_that_does_not_match_the_official_hash_is_deleted(self):
        # 下載壞掉或被換掉的執行檔絕不能留下來被執行。
        from tools import setup_engines
        data = b"not the real engine"
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "katago.zip"
            with patch("urllib.request.urlopen", return_value=io.BytesIO(data)):
                with self.assertRaises(ValueError):
                    setup_engines.download("https://example.invalid/x", len(data), "0" * 64, dest)
            self.assertFalse(dest.exists())

    def test_existing_files_are_kept(self):
        # 已經調過的設定檔（例如 KataGo 的執行緒數）不能被下載的預設值蓋掉；缺的檔案才補上。
        from tools import setup_engines
        with tempfile.TemporaryDirectory() as tmp:
            src, dest = Path(tmp) / "src", Path(tmp) / "dest"
            src.mkdir()
            dest.mkdir()
            (src / "analysis_example.cfg").write_text("default", encoding="utf-8")
            (src / "katago.exe").write_text("new", encoding="utf-8")
            (dest / "analysis_example.cfg").write_text("tuned", encoding="utf-8")
            setup_engines.copy_missing(src, dest)
            self.assertEqual((dest / "analysis_example.cfg").read_text(encoding="utf-8"), "tuned")
            self.assertEqual((dest / "katago.exe").read_text(encoding="utf-8"), "new")

    def test_stockfish_lands_where_the_settings_look_for_it(self):
        # 新電腦的國際象棋只靠這個執行檔：官方壓縮檔多包一層 stockfish/ 資料夾（2026-10-14 下載的那份），
        # 解開後執行檔必須正好在 settings.STOCKFISH["exe"]，GPL 授權檔也要跟著它，否則國際象棋找不到引擎。
        from tools import setup_engines

        def fake_download(url, size, sha256, dest):
            with zipfile.ZipFile(dest, "w") as z:
                z.writestr("stockfish/stockfish-windows-x86-64-universal.exe", "exe")
                z.writestr("stockfish/Copying.txt", "GPL")

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            exe = tmp / "tools" / "stockfish" / Path(settings.STOCKFISH["exe"]).name
            (tmp / "a").mkdir()
            (tmp / "b").mkdir()
            with patch.object(setup_engines, "download", fake_download), patch("builtins.print"):
                setup_engines.install_stockfish(tmp / "a", exe)
                self.assertEqual(exe.read_text(encoding="utf-8"), "exe")
                self.assertTrue(exe.with_name("Copying.txt").exists())
                with self.assertRaises(ValueError):  # 用 STOCKFISH_EXE 改了檔名時要明說，不能當成裝好了
                    setup_engines.install_stockfish(tmp / "b", tmp / "other" / "sf.exe")

    def test_computer_without_opencl_gets_a_cpu_build(self):
        # 沒有顯示卡驅動（沒有 OpenCL）的電腦裝 OpenCL 版會跑不起來，要改用 CPU 版；舊 CPU 沒有 AVX2 時用一般版。
        from tools import setup_engines
        with tempfile.TemporaryDirectory() as tmp:
            system32 = Path(tmp) / "System32"
            system32.mkdir()
            with patch.dict("os.environ", {"SystemRoot": tmp}), \
                    patch("ctypes.windll.kernel32.IsProcessorFeaturePresent", return_value=1):
                self.assertEqual(setup_engines.katago_build(), "eigenavx2")
                (system32 / "OpenCL.dll").write_bytes(b"")
                self.assertEqual(setup_engines.katago_build(), "opencl")
                (system32 / "OpenCL.dll").unlink()
            with patch.dict("os.environ", {"SystemRoot": tmp}), \
                    patch("ctypes.windll.kernel32.IsProcessorFeaturePresent", return_value=0):
                self.assertEqual(setup_engines.katago_build(), "eigen")


class BenchmarkFileTests(SimpleTestCase):
    def test_each_server_keeps_its_own_measurements(self):
        # 同名模型（例如 qwen3:8b）在本機和研究用伺服器上速度不同；選單上的秒數必須是目前連線那台量的，
        # 在遠端測速也不能蓋掉本機的結果。
        with override_settings(OLLAMA_URL="http://localhost:11434"):
            local = narrator.benchmark_file()
        with override_settings(OLLAMA_URL="http://192.0.2.10:11434", OLLAMA_SERVER_NAMES={}):
            remote = narrator.benchmark_file()
        self.assertNotEqual(local, remote)
        # 本機在分開存之前量的結果要照樣讀得到
        self.assertEqual(local.name, "model_benchmarks.json")

    def test_the_file_name_does_not_reveal_the_server_address(self):
        # 測速檔放在公開的儲存庫，研究用伺服器的位址是機密（2026-10-21），所以有代稱時檔名只用代稱
        with override_settings(OLLAMA_URL="http://192.0.2.10:11434", OLLAMA_SERVER_NAMES={"192.0.2.10": "research"}):
            path = narrator.benchmark_file()
        self.assertEqual(path.name, "model_benchmarks_research.json")


class BenchmarkCommandTests(SimpleTestCase):
    # 2026-10-11 研究用伺服器測到一半不回應：卸載模型逾時讓整批中斷，已測完的 4 個模型結果全部沒存到。
    NAMES = ("qwen3:8b", "gemma4:e2b")  # 依清單順序先測 qwen3:8b
    MOVE = {"action": "H8", "fallback": False, "narration": None, "facts": {"points": []}, "run": None}

    def run_benchmark(self, path, agent_move):
        with patch("arena.narrator.benchmark_file", return_value=path), \
                patch("arena.narrator.installed_models", return_value=set(self.NAMES)), \
                patch("arena.agent.agent_move", side_effect=agent_move), \
                patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            call_command("benchmark_models", "--game", "gomoku", "--rounds", "1", stdout=StringIO(), stderr=StringIO())

    def test_unload_timeout_does_not_stop_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "benchmarks.json"
            self.run_benchmark(path, lambda *args: self.MOVE)
            self.assertEqual(set(json.loads(path.read_text(encoding="utf-8"))["gomoku"]), set(self.NAMES))

    def test_models_measured_before_a_crash_are_kept(self):
        def agent_move(module, state, actions, name, steps):
            if name == "gemma4:e2b":
                raise RuntimeError("測到一半出錯")
            return self.MOVE

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "benchmarks.json"
            with self.assertRaises(RuntimeError):
                self.run_benchmark(path, agent_move)
            self.assertEqual(set(json.loads(path.read_text(encoding="utf-8"))["gomoku"]), {"qwen3:8b"})

    def test_unreachable_model_is_not_recorded_as_a_measurement(self):
        # 同一天第二次卡住：每次呼叫都等滿逾時，測速把「每手 120 秒、成功 0%」存成 4 個模型的結果，選單照樣顯示。
        # 呼叫不到模型不是模型的表現：要停止測速、不存這個模型，前面測完的照樣保留。
        def agent_move(module, state, actions, name, steps):
            if name == "gemma4:e2b":
                return agent._fallback(module, state, f"{agent.UNREACHABLE}：timed out", [])
            return self.MOVE

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "benchmarks.json"
            self.run_benchmark(path, agent_move)
            self.assertEqual(set(json.loads(path.read_text(encoding="utf-8"))["gomoku"]), {"qwen3:8b"})


def fake_chat(*replies):
    """假的模型：每次依序回傳一組工具呼叫；None 代表呼叫不到模型。"""
    replies = list(replies)

    def chat(messages, config, tools):
        reply = replies.pop(0)
        if reply is None:
            raise OSError("timed out")
        return {"content": "", "tool_calls": [{"function": {"name": n, "arguments": a}} for n, a in reply]}
    return chat


@override_settings(OLLAMA_URL="http://127.0.0.1:9", OLLAMA_TIMEOUT=2)
class ModelRunTests(TestCase):
    client_class = AdminClient
    # 使用者要比較各模型的速度與成功率（2026-10-19）。以前只有 Agent 自己下成功的手記了模型與秒數：
    # 改由引擎代下的手、說明、提問都沒記，算不出成功率，秒數也只算到順利的手而偏快。
    OPEN_THREE = ["H8", "A15", "I8", "O15", "J8"]  # 黑 H8-J8 活三，輪到白（Agent）
    FACTS = {"kind": "review", "points": [{"evidence": ESTIMATE, "text": "白棋下 H9。"}]}

    def post(self, url, body):
        return self.client.post(url, json.dumps(body), content_type="application/json")

    @staticmethod
    def uses():
        """模型用在下棋、說明、提問的紀錄；不含 AI 助理處理請求的 req_* 紀錄（2026-10-28 起每個請求都有一筆）。"""
        return ModelRun.objects.exclude(kind__startswith="req_").order_by("id")

    def agent_run(self, *replies):
        with patch("arena.agent._chat", side_effect=fake_chat(*replies)):
            return agent.agent_move(GOMOKU, GOMOKU.replay({}, self.OPEN_THREE), self.OPEN_THREE, "qwen3:8b", 4)["run"]

    def make(self, model, outcome, seconds, server="192.0.2.10"):
        return ModelRun.objects.create(kind="agent", game_type="gomoku", model=model, server=server, machine="PC",
                                       seconds=seconds, outcome=outcome)

    def test_a_decided_move_records_how_the_model_got_there(self):
        # 被退回的次數解釋了模型之間的差異（國際象棋的小模型常寫 Nc3 而被退回，最後由引擎代下）
        run = self.agent_run([("find_threats", {})], [("play_move", {"coord": "Z99"})],
                             [("play_move", {"coord": "G8", "reason": "擋住"})])
        self.assertEqual((run["model"], run["outcome"], run["calls"], run["rejected"], run["steps"]),
                         ("qwen3:8b", "ok", 3, 1, 4))
        self.assertLessEqual(run["model_seconds"], run["seconds"])  # 等模型的時間是這一手總時間的一部分

    def test_engine_takeover_still_counts_against_the_model(self):
        run = self.agent_run(*[[("list_candidates", {})]] * 4)
        self.assertEqual((run["model"], run["outcome"], run["calls"]), ("qwen3:8b", "fallback", 4))
        self.assertIn("次內沒有決定", run["problem"])

    def test_unreachable_server_is_not_the_models_failure(self):
        # 伺服器卡住時每個模型都會失敗；算成模型的失敗，比較就沒有意義
        run = self.agent_run(None)
        self.assertEqual((run["outcome"], run["calls"]), ("error", 1))
        self.assertIn(agent.UNREACHABLE, run["problem"])

    def test_moves_in_a_game_are_saved_with_the_game_and_server(self):
        game_id = self.post("/api/gomoku/games/", {"seat": 1}).json()["game"]["id"]
        with patch("arena.agent._chat", side_effect=fake_chat([("play_move", {"coord": "H9", "reason": "跟著下"})])):
            self.post(f"/api/gomoku/games/{game_id}/move/", {"action": "H8", "model": "qwen3:8b"})
        run = self.uses().get()
        self.assertEqual((run.kind, run.game_id, run.index, run.model, run.outcome),
                         ("agent", game_id, 1, "qwen3:8b", "ok"))
        # 同名模型在不同伺服器、不同電腦上速度不同，比較時要分得開
        self.assertEqual((run.server, run.machine), ("127.0.0.1", platform.node()))

    def test_moves_that_need_no_model_are_not_recorded(self):
        # AI 執黑的第一手由程式直接下；記進去會把模型的秒數拉低
        self.post("/api/gomoku/games/", {"seat": 2})
        self.assertFalse(self.uses().exists())

    def test_explanation_failures_are_told_apart(self):
        # 說明沒通過檢查是模型寫錯；呼叫不到是伺服器的問題，要分開算
        for reply in ("重點：擋住。\n說明：先擋。", "重點：下 N2。\n說明：N2 比較好。", OSError("timed out")):
            with patch("arena.narrator._ask_ollama", side_effect=[reply]):
                self.post("/api/gomoku/narrate/", {"facts": self.FACTS, "model": "qwen3:8b"})
        smart = narrator.review_model("qwen3:8b")  # 復盤的說明實際用的模型（ReviewModelTests、LocalReviewModelTests）
        self.assertEqual([(r.kind, r.model, r.outcome) for r in self.uses()],
                         [("review", smart, "ok"), ("review", smart, "fallback"), ("review", smart, "error")])

    def test_a_saved_explanation_is_not_counted_again(self):
        game = Game.objects.create(game_type="gomoku", human_seat=1, actions=["H8", "H9"])
        AiNote.objects.create(game=game, index=1, seat=2, facts={**self.FACTS, "kind": "ai_move"})
        with patch("arena.narrator._ask_ollama", return_value="重點：跟著下。\n說明：靠近黑棋。"):
            for _ in range(2):
                self.post(f"/api/gomoku/games/{game.id}/notes/1/narrate/", {"model": "qwen3:8b"})
            self.post(f"/api/gomoku/games/{game.id}/ask/", {"question": "這手好嗎？", "at": 1, "model": "qwen3:8b"})
        # 第二次直接回傳存好的說明，沒有用到模型
        self.assertEqual([(r.kind, r.game, r.index) for r in self.uses()],
                         [("ai_move", game, 1), ("ask", game, 1)])

    def test_every_benchmark_move_is_kept_across_reruns(self):
        # 測速檔每個模型只留最新一次；紀錄表要保留每次重測，才看得出前後的差異
        move = {**BenchmarkCommandTests.MOVE, "run": {"model": "qwen3:8b", "seconds": 1.0, "outcome": "ok"}}
        with tempfile.TemporaryDirectory() as tmp:
            for _ in range(2):
                with patch("arena.narrator.benchmark_file", return_value=Path(tmp) / "b.json"), \
                        patch("arena.narrator.installed_models", return_value={"qwen3:8b"}), \
                        patch("arena.agent.agent_move", return_value=move), \
                        patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
                    call_command("benchmark_models", "--game", "gomoku", "--rounds", "1", "--model", "qwen3:8b",
                                 stdout=StringIO(), stderr=StringIO())
        self.assertEqual(ModelRun.objects.filter(kind="benchmark").count(), 2 * len(GOMOKU.benchmark_samples))

    def test_old_agent_moves_are_imported_without_a_server(self):
        migration = importlib.import_module("arena.migrations.0004_modelrun")
        game = Game.objects.create(game_type="gomoku", human_seat=1, actions=["H8", "H9", "H10", "I9"])
        facts = {"kind": "ai_move", "points": []}
        AiNote.objects.create(game=game, index=1, seat=2, facts=facts,
                              narration={"source": "agent", "model": "qwen3:8b", "seconds": 3.4})
        AiNote.objects.create(game=game, index=3, seat=2, facts=facts, narration=None)  # 引擎代下，沒記模型
        # 之後才請模型寫的說明：這是說明用的模型、沒有秒數，不是 Agent 下棋
        AiNote.objects.create(game=game, index=5, seat=2, facts=facts,
                              narration={"source": "llm", "model": "gemma4:26b", "key": "x", "text": "x"})
        migration.import_old_notes(apps, SimpleNamespace(connection=connection))  # 只用到正在 migrate 的資料庫名稱
        run = ModelRun.objects.get()
        self.assertEqual((run.kind, run.index, run.model, run.seconds, run.outcome, run.server),
                         ("agent", 1, "qwen3:8b", 3.4, "ok", ""))
        self.assertEqual(run.created_at, game.created_at)

    def test_report_counts_only_what_the_model_did(self):
        for outcome, seconds in (("ok", 2), ("ok", 4), ("ok", 6), ("fallback", 8), ("error", 120), ("error", 0)):
            self.make("qwen3:8b", outcome, seconds)
        for seconds in (1, 2, 3):
            self.make("gemma4:26b", "ok", seconds)
        rows = {r["model"]: r for r in summarize(ModelRun.objects.all(), "gemma4:26b")}
        q = rows["qwen3:8b"]
        # 逾時等了 120 秒是伺服器的問題：不算進模型的秒數，也不算進成功率
        self.assertEqual((q["count"], q["error"], q["rate"], q["median"], q["max"]), (6, 2, 75, 5.0, 8.0))
        self.assertEqual((q["ratio"], q["rate_diff"]), (2.5, -25))  # 基準 gemma4:26b：中位數 2 秒、100%

    def test_report_compares_only_like_with_like(self):
        self.make("gemma4:26b", "ok", 1)
        self.make("qwen3:8b", "ok", 9, server="127.0.0.1")  # 本機的模型不能和研究用伺服器上的基準比
        self.make("qwen3:8b", "ok", 3, server="")  # 舊紀錄只有成功的手，100% 不是真的成功率
        rows = {(r["model"], r["server"]): r for r in summarize(ModelRun.objects.all(), "gemma4:26b")}
        self.assertIsNone(rows[("qwen3:8b", "127.0.0.1")]["ratio"])
        self.assertIsNone(rows[("qwen3:8b", "")]["rate"])

    def test_since_leaves_out_records_from_before_a_change(self):
        # 改了設定（例如 num_ctx）之後，要能只看改之後的紀錄
        old = self.make("qwen3:8b", "ok", 2)
        ModelRun.objects.filter(pk=old.pk).update(created_at=datetime(2026, 10, 1, tzinfo=timezone.utc))
        self.make("gemma4:26b", "ok", 1)
        out = StringIO()
        call_command("model_report", "--since", "2026-10-10", stdout=out)
        self.assertIn("模型使用紀錄：1 筆", out.getvalue())

    def test_csv_opens_in_excel_with_readable_chinese(self):
        self.make("qwen3:8b", "ok", 2)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.csv"
            call_command("model_report", "--csv", str(path), stdout=StringIO())
            raw = path.read_bytes()
        self.assertTrue(raw.startswith(codecs.BOM_UTF8))  # 沒有 BOM 時 Excel 會把中文讀成亂碼
        rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))
        self.assertEqual(rows[0][:3], ["用途", "遊戲", "模型"])
        self.assertEqual(rows[1][:3], ["Agent 下棋", "五子棋", "qwen3:8b"])


def obedient(seen=None, values=None):
    """假的 AI 助理模型：依序呼叫提供的每個工具一次，參數從 values 取（可以是函式，例如剛建立的棋局編號）。
    seen 收集它看到的所有訊息。每個新請求（訊息只有系統提示與任務）重新開始。"""
    values = values or {}
    called = []

    def chat(messages, tools, model=None):
        if len(messages) == 2:
            called.clear()
        if seen is not None:
            seen.extend(str(m.get("content", "")) for m in messages)
        for t in tools:
            name = t["function"]["name"]
            if name not in called:
                called.append(name)
                args = {k: (v() if callable(v) else v) for k, v in values.get(name, {}).items()}
                return {"content": "", "tool_calls": [{"function": {"name": name, "arguments": args}}]}
        return {"content": "完成", "tool_calls": []}
    return chat


def scripted(*replies):
    """假的 AI 助理模型：每次依序回傳一組工具呼叫，用完就回覆完成；None 代表呼叫不到模型。"""
    replies = list(replies)

    def chat(messages, tools, model=None):
        reply = replies.pop(0) if replies else []
        if reply is None:
            raise OSError("timed out")
        return {"content": "完成", "tool_calls": [{"function": {"name": n, "arguments": a}} for n, a in reply]}
    return chat


def assert_agent_gives_what_the_old_code_gave(test, module, make_game, action):
    """每個交給 AI 助理的遊戲都要過（使用者決定，2026-10-28：換成 Agent 處理不能改變任何功能）：
    同一步棋，Agent 完成的回應與存下的棋譜，和原本的程式（模型呼叫不到時的補完）一模一樣。
    make_game() 建一盤新棋。AI 棋手呼叫不到模型時由（假的）引擎代下，結果固定，兩條路才比得出是否相同。"""
    by_agent, by_code = make_game(), make_game()

    def move(game, model_chat):
        with patch("arena.orchestrator._chat", side_effect=model_chat), \
                patch("arena.agent._chat", side_effect=OSError("測試不連 Ollama")):
            return test.client.post(f"/api/{module.key}/games/{game.id}/move/", json.dumps({"action": action}),
                                    content_type="application/json").json()
    a = move(by_agent, obedient(values={"play_human_move": {"game_id": by_agent.id, "action": action},
                                        "run_ai_turns": {"game_id": by_agent.id}}))
    b = move(by_code, OSError("timed out"))
    test.assertEqual((a["handled"]["by"], b["handled"]["by"]), ("agent", "fallback"))
    for key in ("action", "frame", "winner", "draw"):
        test.assertEqual(a[key], b[key], key)
    test.assertEqual([m["action"] for m in a["ai_moves"]], [m["action"] for m in b["ai_moves"]])
    by_agent.refresh_from_db()
    by_code.refresh_from_db()
    test.assertEqual(by_agent.actions, by_code.actions)
    test.assertGreater(len(by_agent.actions), 1)  # 玩家的一步加上 AI 的回應都存了


def assert_question_focus_reaches_the_position(test, module, game, at, focus):
    """提問時 Agent 在 explain_position 填的 focus 文字要能被這個遊戲的 question_focus 解析：
    查到的是那個位置的分析，而不是沒有 focus 時的一般分析，否則 Agent 指名的位置就白填了。"""
    test.assertTrue(module.question_focus(focus), focus)
    with patch("arena.orchestrator._chat", side_effect=scripted([("explain_position", {"focus": focus})])), \
            patch("arena.narrator._ask_ollama", return_value="重點：還可以。\n說明：這是常見的下法。"):
        r = test.client.post(f"/api/{module.key}/games/{game.id}/ask/",
                             json.dumps({"question": f"{focus} 好不好？", "at": at}), content_type="application/json").json()
    test.assertEqual(r["handled"]["by"], "agent")
    test.assertEqual(r["facts"]["points"], operations.position_points(module, game, at, module.question_focus(focus), game.human_seat))
    test.assertNotEqual(r["facts"]["points"], operations.position_points(module, game, at, [], game.human_seat))


class RequestAgentTests(TestCase):
    client_class = AdminClient
    # 使用者決定（2026-10-28）：每個請求都交給 AI 助理處理，為了證明 Agentic 系統可行。
    # 這些測試守的是：功能和原本完全一樣、Agent 不能改變使用者要求的事、沒完成時一定有人補完並告訴玩家。
    def setUp(self):
        # AI 棋手呼叫不到模型時由引擎代下，結果固定，兩條路才比得出是否相同
        patcher = patch("arena.agent._chat", side_effect=OSError("測試不連 Ollama"))
        patcher.start()
        self.addCleanup(patcher.stop)
        # 其他測試連 127.0.0.1:9 失敗時留下的 AI 伺服器問題，畫面會改顯示那個說明、不顯示一般的提示
        health._current = None
        self.addCleanup(setattr, health, "_current", None)

    def post(self, url, body):
        return self.client.post(url, json.dumps(body), content_type="application/json")

    def move(self, game, action, model_chat):
        with patch("arena.orchestrator._chat", side_effect=model_chat):
            return self.post(f"/api/gomoku/games/{game.id}/move/", {"action": action})

    def new_game(self):
        return Game.objects.create(game_type="gomoku", human_seat=1, actions=[])

    def runs(self, kind):
        return list(ModelRun.objects.filter(kind=f"req_{kind}"))

    def test_agent_gives_exactly_what_the_old_code_gave(self):
        # 換成 Agent 處理不能改變任何功能：同一步棋，Agent 完成的回應和原本程式的回應一模一樣
        by_agent, by_code = self.new_game(), self.new_game()
        a = self.move(by_agent, "H8", obedient(values={"play_human_move": {"game_id": by_agent.id, "action": "H8"},
                                                       "run_ai_turns": {"game_id": by_agent.id}})).json()
        b = self.move(by_code, "H8", OSError("timed out")).json()
        self.assertEqual((a["handled"]["by"], b["handled"]["by"]), ("agent", "fallback"))
        for key in ("action", "frame", "winner", "draw"):
            self.assertEqual(a[key], b[key], key)
        self.assertEqual([m["action"] for m in a["ai_moves"]], [m["action"] for m in b["ai_moves"]])
        by_agent.refresh_from_db()
        by_code.refresh_from_db()
        self.assertEqual(by_agent.actions, by_code.actions)
        self.assertEqual(self.runs("move")[0].outcome, "ok")

    def test_unreachable_model_still_completes_and_tells_the_player(self):
        # 研究用伺服器當過三次；當掉時網站照樣能用，而且玩家知道這次不是 AI 助理完成的
        game = self.new_game()
        r = self.move(game, "H8", OSError("timed out")).json()
        self.assertEqual(r["handled"]["by"], "fallback")
        self.assertEqual(r["handled"]["notice"], orchestrator.NOTICES["error"])
        game.refresh_from_db()
        self.assertEqual(len(game.actions), 2)  # 玩家一步、AI 一步
        run = self.runs("move")[0]
        self.assertEqual(run.outcome, "error")  # 伺服器的問題，不算進模型的成功率
        self.assertIn(agent.UNREACHABLE, run.problem)

    def test_agent_cannot_change_the_players_move(self):
        # 按鈕帶來的參數是鎖定的：Agent 想替玩家換一步棋時退回、不執行，最後照玩家下的存檔
        game = self.new_game()
        r = self.move(game, "H8", scripted([("play_human_move", {"game_id": game.id, "action": "I9"})])).json()
        game.refresh_from_db()
        self.assertEqual(game.actions[0], "H8")
        self.assertEqual(r["handled"]["by"], "fallback")
        run = self.runs("move")[0]
        self.assertEqual((run.outcome, run.rejected), ("fallback", 1))

    def test_stopping_halfway_never_saves_a_move_twice(self):
        # Agent 存了玩家的一步就停下：補完時只讓 AI 行動，玩家的那一步不會再存一次
        game = self.new_game()
        r = self.move(game, "H8", scripted([("play_human_move", {"game_id": game.id, "action": "H8"})])).json()
        game.refresh_from_db()
        self.assertEqual(len(game.actions), 2)
        self.assertEqual(game.actions.count("H8"), 1)
        self.assertEqual(r["handled"]["by"], "fallback")

    def test_ai_cannot_act_before_the_players_move_is_saved(self):
        # 先讓 AI 行動的話，輪到的是玩家、AI 什麼都不會做，玩家的那一步之後就沒有人回應
        game = self.new_game()
        r = self.move(game, "H8", scripted([("run_ai_turns", {"game_id": game.id})],
                                           [("play_human_move", {"game_id": game.id, "action": "H8"}),
                                            ("run_ai_turns", {"game_id": game.id})])).json()
        self.assertEqual(r["handled"]["by"], "agent")
        self.assertEqual(len(r["ai_moves"]), 1)

    def test_illegal_move_is_refused_like_before(self):
        game = Game.objects.create(game_type="gomoku", human_seat=1, actions=["H8", "H9"])
        r = self.move(game, "H8", obedient(values={"play_human_move": {"game_id": game.id, "action": "H8"}}))
        self.assertEqual(r.status_code, 400)
        game.refresh_from_db()
        self.assertEqual(game.actions, ["H8", "H9"])
        run = self.runs("move")[0]
        self.assertEqual(run.outcome, "ok")  # Agent 照請求做了，是請求本身不成立
        self.assertIn("請求不成立", run.problem)

    def test_tools_outside_the_request_are_refused(self):
        # 復盤時 Agent 不能順手結束對局：每種請求只能用自己的工具
        game = Game.objects.create(game_type="gomoku", human_seat=1, actions=["H8", "H9"])
        with patch("arena.orchestrator._chat", side_effect=scripted([("end_game", {"game_id": game.id}),
                                                                     ("review_game", {"game_id": game.id})])):
            r = self.client.get(f"/api/gomoku/games/{game.id}/review/").json()
        game.refresh_from_db()
        self.assertFalse(game.ended)
        self.assertEqual(r["handled"]["by"], "agent")
        self.assertIn("moments", r)
        self.assertEqual(self.runs("review")[0].rejected, 1)

    def test_agent_chooses_what_to_look_up_for_a_question(self):
        # 提問由 Agent 決定查什麼：「一開始那手」沒寫成「第 1 手」，正規表示式抓不到，Agent 可以查
        game = Game.objects.create(game_type="gomoku", human_seat=1, actions=["H8", "H9", "I8"])
        expected = operations.action_points(GOMOKU, game, 1, 1)
        with patch("arena.orchestrator._chat", side_effect=scripted([("explain_action", {"n": 1})])), \
                patch("arena.narrator._ask_ollama", return_value="重點：還可以。\n說明：開局常見。"):
            r = self.post(f"/api/gomoku/games/{game.id}/ask/", {"question": "我一開始那手好嗎？", "at": 3}).json()
        self.assertEqual(r["handled"]["by"], "agent")
        self.assertEqual(r["facts"]["points"], expected)
        self.assertNotEqual(operations.ask_points_by_rules(GOMOKU, game, "我一開始那手好嗎？", 3, 1), expected)

    def test_answers_from_agent_chosen_facts_are_still_checked(self):
        # Agent 只決定查什麼；回答照樣要通過 check_text，提到事實裡沒有的座標就改顯示事實原文
        game = Game.objects.create(game_type="gomoku", human_seat=1, actions=["H8", "H9", "I8"])
        with patch("arena.orchestrator._chat", side_effect=scripted([("explain_action", {"n": 1})])), \
                patch("arena.narrator._ask_ollama", return_value="重點：應該下 A1。\n說明：A1 比較好。"):
            r = self.post(f"/api/gomoku/games/{game.id}/ask/", {"question": "第 1 手好嗎？", "at": 3}).json()
        self.assertEqual(r["source"], "template")

    def test_the_question_says_which_side_asked(self):
        # 2026-11-16 情境測試：執白棋的玩家問「我這一步哪裡不好？」，AI 助理查了黑棋的上一步，模型也照著回答黑棋那一步。
        # 交給 AI 助理的任務和交給模型的說明都要寫明提問的人是哪一方
        game = Game.objects.create(game_type="gomoku", human_seat=2, actions=["H8", "H9", "I8"])
        seen = []
        with patch("arena.orchestrator._chat", side_effect=obedient(seen, {"explain_action": {"n": 2}})),                 patch("arena.narrator._ask_ollama", side_effect=OSError("timed out")):
            r = self.post(f"/api/gomoku/games/{game.id}/ask/", {"question": "我這一步哪裡不好？", "at": 1}).json()
        self.assertTrue(any("使用者是白棋" in text for text in seen), seen)
        prompt = narrator.request_body(r["facts"], narrator.model_config(), GOMOKU.name)["messages"][1]["content"]
        self.assertIn("提問的玩家（問題裡的「我」）是白棋。", prompt)

    def test_question_falls_back_to_the_old_lookup(self):
        game = Game.objects.create(game_type="gomoku", human_seat=1, actions=["H8", "H9", "I8"])
        with patch("arena.orchestrator._chat", side_effect=OSError("timed out")), \
                patch("arena.narrator._ask_ollama", side_effect=OSError("timed out")):
            r = self.post(f"/api/gomoku/games/{game.id}/ask/", {"question": "第 1 手好嗎？", "at": 3}).json()
        self.assertEqual(r["handled"]["by"], "fallback")
        self.assertEqual(r["facts"]["points"], operations.ask_points_by_rules(GOMOKU, game, "第 1 手好嗎？", 3, 1))

    def test_fallback_answers_about_the_named_move_after_the_game_ended(self):
        # 2026-11-22 例行檢查：復盤預設停在最後局面，AI 連不上時問「第 3 手為什麼不好？」，
        # 補上的事實只有終局與認輸前的局面，完全沒有第 3 手，答非所問。結束的局面也要照問題裡的「第 N 手」去查
        game = Game.objects.create(game_type="gomoku", human_seat=1, actions=["H8", "H9", "I8", "I9", "resign"],
                                   winner=2)
        points = operations.ask_points_by_rules(GOMOKU, game, "第 3 手為什麼不好？", 5, 1)
        for p in operations.action_points(GOMOKU, game, 3, 1):
            self.assertIn(p, points)
        # 問的是認輸那一步時，終局事實已經附上認輸前的局面，不要重複
        points = operations.ask_points_by_rules(GOMOKU, game, "第 5 手為什麼認輸？", 5, 1)
        self.assertEqual(len(points), len({p["text"] for p in points}))

    def test_a_question_cannot_look_up_every_step(self):
        # 2026-11-19 情境測試：問「我哪一步最差」時，AI 助理一次查了每一步（14 到 35 次），事實多到 30–90 則，
        # 等 40–240 秒，還讓研究用伺服器的模型出錯一次。一次提問最多查 MAX_ACTION_QUERIES 步，其餘不執行
        game = Game.objects.create(game_type="gomoku", human_seat=1, actions=["H8", "H9", "I8", "I9", "J8", "J9"])
        every_step = [("explain_action", {"n": n}) for n in range(1, 7)]
        with patch("arena.orchestrator._chat", side_effect=scripted(every_step)), \
                patch("arena.narrator._ask_ollama", return_value="重點：還可以。\n說明：開局常見。"):
            r = self.post(f"/api/gomoku/games/{game.id}/ask/", {"question": "我哪一步最差？", "at": 6}).json()
        looked_up = {p["text"] for n in range(1, orchestrator.MAX_ACTION_QUERIES + 1)
                     for p in operations.action_points(GOMOKU, game, n, 1)}
        self.assertEqual({p["text"] for p in r["facts"]["points"]}, looked_up)
        self.assertFalse(any(p["text"].startswith("第 4 手") for p in r["facts"]["points"]))

    def test_whole_game_questions_get_the_mistakes_ranked_by_severity(self):
        # 2026-11-19 情境測試：圍棋問「我哪一步最差」，答的是損失 8 目的第 2 手，復盤自己找到的是損失 32 目的第 14 手。
        # list_problems 一次給出問題檢查的結果，依嚴重程度排序：關鍵時刻在前，規則確認的錯誤在前，再依損失大小
        game = Game.objects.create(game_type="weiqi", human_seat=1, settings={"size": 9, "komi": 7.5},
                                   actions=["E5", "E1", "G4", "A1", "C5", "J4"])
        moment = lambda index, level, loss=None: {  # noqa: E731
            "index": index, "level": level, "title": f"第{index}", "played": "A1", "best": "C3", "pattern": "目數損失",
            "points": [{"evidence": ESTIMATE, "text": "依引擎估計…"}], **({"loss": loss} if loss is not None else {})}
        found = [moment(0, "minor", 3.0), moment(2, "key", 8.0), moment(4, "key", 32.0), moment(5, "key"),
                 moment(1, "suggest", 1.5)]
        with patch.object(WEIQI, "review", return_value=found), \
                patch("arena.orchestrator._chat", side_effect=scripted([("list_problems", {})])), \
                patch("arena.narrator._ask_ollama", return_value="重點：還可以。\n說明：開局常見。"):
            r = self.post(f"/api/weiqi/games/{game.id}/ask/", {"question": "我哪一步最差？", "at": 6}).json()
            overall = operations._overall(WEIQI, game, found, 1)
        ranked = [p["text"] for p in r["facts"]["points"]]
        self.assertIn("問題檢查找到你的失誤 4 個；另有建議 1 個", ranked[0])
        self.assertEqual([t.split("：")[1].split("（")[0] for t in ranked[1:]], ["第 6 手", "第 5 手", "第 3 手", "第 1 手"])
        # 整體評論列出的三步也是最嚴重的三步，不是最早的三步
        self.assertIn("「目數損失」4 次（第 6 手、第 5 手、第 3 手）", " ".join(p["text"] for p in overall["points"]))

    def test_asking_about_an_ai_move_shows_what_the_ai_looked_up(self):
        # 2026-11-19 情境測試：撲克問「AI 第 23 手的想法」只拿到「它跟注了」一則，答不出 AI 用了什麼假設。
        # 問 AI 下的那一步時附上 AI 當時查到的分析；手牌還沒結束、思路含底牌時（hidden）照樣不給
        game = Game.objects.create(game_type="gomoku", human_seat=1, actions=["H8", "H9", "I8"])
        thought = "依引擎估計，白棋下 H9 是為了擋住黑棋的直線。"
        AiNote.objects.create(game=game, index=1, seat=2, facts={"kind": "ai_move", "points": [
            {"evidence": ESTIMATE, "text": thought},
            {"evidence": ESTIMATE, "text": "Agent 沒有完成這一手（逾時），改由引擎代下。"}]})
        texts = [p["text"] for p in operations.action_points(GOMOKU, game, 2, 1)]
        self.assertIn(thought, texts)
        self.assertIn("第 2 手是 AI 下的；以下是 AI 下這一步時查到的分析。", texts)
        self.assertFalse(any(t.startswith("Agent ") for t in texts))  # 研究紀錄不交給模型改寫給玩家
        # 那一步常是正在看的局面的「實際下一步」：查局面（AI 助理沒查第 N 步、或改用原本的查法）時也要有
        self.assertIn(thought, [p["text"] for p in operations.position_points(GOMOKU, game, 1, [], 1)])
        hide = lambda state, notes: [{**n, "hidden": True} for n in notes]  # noqa: E731
        with patch.object(GOMOKU, "public_notes", side_effect=hide):
            self.assertNotIn(thought, [p["text"] for p in operations.action_points(GOMOKU, game, 2, 1)])
            self.assertNotIn(thought, [p["text"] for p in operations.position_points(GOMOKU, game, 1, [], 1)])

    def test_pages_go_through_the_agent_and_show_when_it_failed(self):
        with patch("arena.orchestrator._chat", side_effect=obedient()):
            html = self.client.get("/").content.decode()
        self.assertNotIn(orchestrator.NOTICES["error"], html)
        self.assertEqual(self.runs("home")[0].outcome, "ok")
        with patch("arena.orchestrator._chat", side_effect=OSError("timed out")):
            html = self.client.get("/").content.decode()
        self.assertIn(orchestrator.NOTICES["error"], html)

    def test_games_not_converted_yet_skip_the_agent(self):
        # 還沒改的遊戲（agentic_requests = False，新遊戲的預設）照原本的程式處理，也不記請求紀錄。
        # 現有的遊戲都改了（2026-11-02），所以這裡暫時把圍棋設回 False 來測
        with patch.object(WEIQI, "agentic_requests", False), patch("arena.orchestrator._chat") as chat:
            r = self.client.get("/api/weiqi/games/").json()
        chat.assert_not_called()
        self.assertEqual(r["handled"], {"by": "direct"})
        self.assertFalse(ModelRun.objects.exists())


SMART = {"name": "smart:70b", "label": "最聰明", "think": False, "agent": True}
SMALL = {"name": "small:2b", "label": "最小", "think": False, "agent": True}
DEFAULT = {"name": "default:26b", "label": "預設", "think": False, "agent": True}


# 研究用伺服器這類非本機的 Ollama（測試環境的 OLLAMA_URL 是 127.0.0.1，會被當成本機 AI 模式）；這裡不會真的連線
REMOTE_OLLAMA = "http://192.0.2.1:9"


@override_settings(OLLAMA_MODELS=[SMART, DEFAULT, SMALL], OLLAMA_MODEL=DEFAULT["name"], OLLAMA_URL=REMOTE_OLLAMA)
class ReviewModelTests(TestCase):
    # 使用者決定（2026-11-16）：復盤 AI 一律用最聰明的模型（OLLAMA_MODELS 依聰明程度排，第一個），
    # 不跟著玩家在進階設定選的模型：玩家為了下棋快選小模型時，復盤的說明與回答不該跟著變差。
    # AI 下棋那一手的思路說明不在內：它說明的是玩家選的那個模型自己下的棋。
    # 本機 AI 模式例外（LocalReviewModelTests）。
    client_class = AdminClient
    FACTS = {"kind": "review", "points": [{"evidence": ESTIMATE, "text": "白棋下 H9。"}]}

    def setUp(self):
        self.narrated = []  # 每次說明用的模型

        def ask(facts, config, game_name):
            self.narrated.append((facts["kind"], config["name"]))
            return "重點：擋住。\n說明：先擋。"
        for target, effect in (("arena.narrator._ask_ollama", ask),
                               ("arena.agent._chat", OSError("測試不連 Ollama"))):
            patcher = patch(target, side_effect=effect)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.installed = {SMART["name"], DEFAULT["name"], SMALL["name"]}  # 這台 Ollama 伺服器上有的模型
        patcher = patch("arena.narrator.installed_models", side_effect=lambda: self.installed)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.assistant = []  # AI 助理每次呼叫用的模型

        def chat(messages, tools, model=None):
            self.assistant.append(model)
            return {"content": "完成", "tool_calls": []}
        patcher = patch("arena.orchestrator._chat", side_effect=chat)
        patcher.start()
        self.addCleanup(patcher.stop)

    def post(self, url, body):
        return self.client.post(url, json.dumps(body), content_type="application/json")

    def test_review_explanations_use_the_smartest_model_whatever_the_player_picked(self):
        for kind in ("review", "overall", "opponent"):
            self.post("/api/gomoku/narrate/", {"facts": {**self.FACTS, "kind": kind}, "model": SMALL["name"]})
        self.assertEqual(self.narrated, [("review", SMART["name"]), ("overall", SMART["name"]),
                                         ("opponent", SMART["name"])])
        # 模型使用紀錄記的是實際用的模型，報表才不會把最聰明的模型的秒數算到玩家選的小模型上
        self.assertEqual({r.model for r in ModelRun.objects.exclude(kind__startswith="req_")}, {SMART["name"]})

    def test_the_smartest_model_is_the_smartest_one_this_server_has(self):
        # 本機 AI 模式的本機 Ollama 通常只有小模型：固定用清單第一個的話，每次復盤都找不到模型、只能顯示事實原文
        self.installed = {DEFAULT["name"], SMALL["name"]}
        self.post("/api/gomoku/narrate/", {"facts": self.FACTS, "model": SMALL["name"]})
        self.assertEqual(self.narrated, [("review", DEFAULT["name"])])

    def test_questions_are_answered_by_the_smartest_model(self):
        game = Game.objects.create(game_type="gomoku", human_seat=1, actions=["H8", "H9", "I8"])
        r = self.post(f"/api/gomoku/games/{game.id}/ask/", {"question": "第 1 手好嗎？", "at": 3, "model": SMALL["name"]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.narrated, [("ask", SMART["name"])])

    PANEL = "復盤和「問 AI」一律用 AI 伺服器上最聰明的模型"

    def test_the_settings_panel_says_which_model_reviews(self):
        # 設定面板要照實說：本機 AI 模式寫「最聰明的模型」，玩家會以為復盤比較聰明
        html = self.client.get("/gomoku/").content.decode()
        self.assertIn(self.PANEL, html)
        self.assertNotIn({LocalReviewModelTests.PANEL, ReviewModelTests.PANEL}.difference({self.PANEL}).pop(), html)

    def test_the_ai_moves_own_explanation_keeps_the_players_model(self):
        game = Game.objects.create(game_type="gomoku", human_seat=1, actions=["H8", "H9"])
        AiNote.objects.create(game=game, index=1, seat=2, facts={**self.FACTS, "kind": "ai_move"})
        self.post(f"/api/gomoku/games/{game.id}/notes/1/narrate/", {"model": SMALL["name"]})
        self.assertEqual(self.narrated, [("ai_move", SMALL["name"])])

    def test_the_assistant_handling_review_requests_is_the_smartest_model_too(self):
        game = Game.objects.create(game_type="gomoku", human_seat=1, actions=["H8", "H9", "I8"])
        self.client.get(f"/api/gomoku/games/{game.id}/review/")
        self.post(f"/api/gomoku/games/{game.id}/ask/", {"question": "第 1 手好嗎？", "at": 3, "model": SMALL["name"]})
        self.post("/api/gomoku/narrate/", {"facts": self.FACTS, "model": SMALL["name"]})
        review_calls = list(self.assistant)
        self.post(f"/api/gomoku/games/{game.id}/move/", {"action": "A1", "model": SMALL["name"]})
        self.assertEqual(set(review_calls), {SMART["name"]})
        # 其他請求（下棋、開頁面）照舊用預設模型：只有復盤改
        self.assertEqual(set(self.assistant[len(review_calls):]), {DEFAULT["name"]})
        self.assertEqual({r.kind: r.model for r in ModelRun.objects.filter(kind__startswith="req_")},
                         {"req_review": SMART["name"], "req_ask": SMART["name"], "req_narrate": SMART["name"],
                          "req_move": DEFAULT["name"]})



@override_settings(OLLAMA_URL="http://127.0.0.1:11434")
class LocalReviewModelTests(ReviewModelTests):
    # 使用者決定（2026-11-20）：本機 AI 模式的復盤也用下棋的小模型。原開發機（RTX 4060 8 GB）本機實測：
    # 復盤要另外載入 qwen3:8b，連兩次載入逾時（120 秒），載入時顯示卡滿載，瀏覽器播影片會卡。
    # 同一個模型下棋又復盤，本機 Ollama 全程只載入一個模型。研究用伺服器照舊用最聰明的（上面的 ReviewModelTests）。
    PANEL = "復盤和「問 AI」也用這個模型"

    def test_review_explanations_use_the_smartest_model_whatever_the_player_picked(self):
        for kind in ("review", "overall", "opponent"):
            self.post("/api/gomoku/narrate/", {"facts": {**self.FACTS, "kind": kind}, "model": SMALL["name"]})
        self.assertEqual(self.narrated, [(k, SMALL["name"]) for k in ("review", "overall", "opponent")])
        self.assertEqual({r.model for r in ModelRun.objects.exclude(kind__startswith="req_")}, {SMALL["name"]})

    def test_the_smartest_model_is_the_smartest_one_this_server_has(self):
        # 本機上裝了比較聰明的模型也不換過去：換過去就要多載入一個模型
        self.post("/api/gomoku/narrate/", {"facts": self.FACTS, "model": SMALL["name"]})
        self.assertEqual(self.narrated, [("review", SMALL["name"])])

    def test_questions_are_answered_by_the_smartest_model(self):
        game = Game.objects.create(game_type="gomoku", human_seat=1, actions=["H8", "H9", "I8"])
        r = self.post(f"/api/gomoku/games/{game.id}/ask/", {"question": "第 1 手好嗎？", "at": 3, "model": SMALL["name"]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.narrated, [("ask", SMALL["name"])])

    def test_the_assistant_handling_review_requests_is_the_smartest_model_too(self):
        # AI 助理本來就用預設模型（本機 AI 模式是 gemma4:e2b）；復盤的請求也一樣，不另外載入最聰明的
        game = Game.objects.create(game_type="gomoku", human_seat=1, actions=["H8", "H9", "I8"])
        self.client.get(f"/api/gomoku/games/{game.id}/review/")
        self.post(f"/api/gomoku/games/{game.id}/ask/", {"question": "第 1 手好嗎？", "at": 3, "model": SMALL["name"]})
        self.post("/api/gomoku/narrate/", {"facts": self.FACTS, "model": SMALL["name"]})
        self.assertEqual(set(self.assistant), {DEFAULT["name"]})
        self.assertEqual({r.model for r in ModelRun.objects.filter(kind__startswith="req_")}, {DEFAULT["name"]})

class RulesPanelTests(TestCase):
    client_class = AdminClient

    def test_every_game_page_explains_its_rules(self):
        # 使用者要求每個遊戲都要有規則說明；新增遊戲時忘了寫規則，這個測試要擋下來
        from arena import games
        for key, module in games.GAMES.items():
            self.assertTrue(module.rules, key)
            html = self.client.get(f"/{key}/").content.decode()
            for section in module.rules:
                self.assertIn(f"<h3>{section['title']}</h3>", html, key)


class HomePageTests(TestCase):
    client_class = AdminClient

    def test_every_game_has_a_card_with_picture_and_description(self):
        # 首頁的遊戲卡片要有圖和一句介紹；新增遊戲時忘了附圖，首頁的 ?v= 版本號找不到檔案會整頁出錯
        from django.contrib.staticfiles import finders

        from arena import games
        html = self.client.get("/").content.decode()
        for key, module in games.GAMES.items():
            self.assertTrue(module.tagline, key)
            self.assertIn(module.tagline, html, key)
            self.assertTrue(module.icon and finders.find(module.icon), key)
            self.assertIn(f"/static/{module.icon}?v=", html, key)

    def test_unfinished_game_can_be_resumed_but_untouched_ones_are_not_listed(self):
        # 打開對局頁時 AI 先行動就會存一盤；玩家一步都沒下的不能列在「繼續上次的對局」，否則每開一次頁面就多一盤
        note = {"kind": "ai_move", "points": []}
        played = Game.objects.create(game_type="gomoku", human_seat=1, actions=["H8", "H9"])
        AiNote.objects.create(game=played, index=1, seat=2, facts=note)
        untouched = Game.objects.create(game_type="gomoku", human_seat=2, actions=["H8"])
        AiNote.objects.create(game=untouched, index=0, seat=1, facts=note)
        html = self.client.get("/").content.decode()
        self.assertIn(f'/gomoku/?resume={played.id}"', html)
        self.assertNotIn(f'?resume={untouched.id}"', html)
        self.assertNotIn(f'?review={untouched.id}"', html)

    def test_record_counts_only_finished_games(self):
        # 勝率是「下完的對局裡贏了幾盤」；沒下完的不算輸，否則中途離開的對局會把勝率拉低
        for result in ({"winner": 1}, {"winner": 2}, {"draw": True}, {}):
            Game.objects.create(game_type="gomoku", human_seat=1, actions=["H8"], **result)
        html = self.client.get("/").content.decode()
        for text in ("1 勝", "1 負", "1 和", "33%"):
            self.assertIn(text, html)


class HistoryPageTests(TestCase):
    client_class = AdminClient

    def setUp(self):
        self.won = Game.objects.create(game_type="gomoku", human_seat=1, actions=["H8"], winner=1)
        self.open = Game.objects.create(game_type="gomoku", human_seat=1, actions=["H8"])
        self.lost = Game.objects.create(game_type="chess", human_seat=1, actions=["e2e4"], winner=2)

    def listed(self, query=""):
        html = self.client.get(f"/history/{query}").content.decode()
        return {g.id for g in (self.won, self.open, self.lost) if f'?review={g.id}"' in html}

    def test_filters_by_game_and_result(self):
        self.assertEqual(self.listed(), {self.won.id, self.open.id, self.lost.id})
        self.assertEqual(self.listed("?game=gomoku"), {self.won.id, self.open.id})
        self.assertEqual(self.listed("?game=gomoku&result=unfinished"), {self.open.id})
        self.assertEqual(self.listed("?result=loss"), {self.lost.id})
        self.assertEqual(self.listed("?game=nope&result=nope"), {self.won.id, self.open.id, self.lost.id})

    def test_only_unfinished_games_can_be_continued(self):
        # 下完的對局伺服器不接受新的一步，所以不能出現「繼續下」
        html = self.client.get("/history/").content.decode()
        self.assertIn(f'?resume={self.open.id}"', html)
        self.assertNotIn(f'?resume={self.won.id}"', html)
        self.assertNotIn(f'?resume={self.lost.id}"', html)


class EndGameTests(TestCase):
    client_class = AdminClient
    # 使用者要求能結束不想下的對局，而且「不計勝負」和認輸分開（2026-10-21）
    def setUp(self):
        self.game = Game.objects.create(game_type="gomoku", human_seat=1, actions=["H8"])

    def end(self):
        return self.client.post(f"/api/gomoku/games/{self.game.id}/end/", "{}", content_type="application/json")

    def test_ended_game_cannot_be_continued_and_is_not_counted(self):
        self.assertEqual(self.end().status_code, 200)
        self.game.refresh_from_db()
        self.assertTrue(self.game.ended and self.game.finished)
        self.assertIsNone(self.game.winner)
        move = self.client.post(f"/api/gomoku/games/{self.game.id}/move/", '{"action": "I9"}',
                                content_type="application/json")
        self.assertEqual(move.status_code, 400)
        # 不能再出現在「繼續上次的對局」，戰績也不能把它算成輸
        html = self.client.get("/").content.decode()
        self.assertNotIn(f'?resume={self.game.id}"', html)
        self.assertNotIn("1 負", html)
        self.assertIn("不計勝負", html)

    def test_finished_game_cannot_be_ended_again(self):
        # 已經分出勝負的對局不能改成不計勝負，否則輸掉的對局可以從戰績裡消失
        self.game.winner = 2
        self.game.save()
        self.assertEqual(self.end().status_code, 400)
        self.game.refresh_from_db()
        self.assertFalse(self.game.ended)

    def test_list_button_ends_the_game_and_returns_to_the_same_list(self):
        back = "/history/?result=unfinished"
        r = self.client.post(f"/games/{self.game.id}/end/", {"next": back})
        self.assertRedirects(r, back, fetch_redirect_response=False)
        self.game.refresh_from_db()
        self.assertTrue(self.game.ended)
        # 只回到本站的網址，不能被拿來轉到別的網站
        other = Game.objects.create(game_type="gomoku", human_seat=1, actions=["H8"])
        r = self.client.post(f"/games/{other.id}/end/", {"next": "https://example.com/"})
        self.assertRedirects(r, "/", fetch_redirect_response=False)


class GuidePageTests(TestCase):
    client_class = AdminClient

    def test_every_games_rules_can_be_read_before_playing(self):
        # 「怎麼玩」要在進對局頁之前就看得到每個遊戲的規則
        from arena import games
        html = self.client.get("/guide/").content.decode()
        for key, module in games.GAMES.items():
            self.assertIn(f'id="{key}"', html)
            for section in module.rules:
                self.assertIn(f"<h3>{section['title']}</h3>", html, key)


class SqlServerSetupTests(SimpleTestCase):
    # 資料庫改用每台電腦本機的 SQL Server（2026-10-19 使用者決定）；tools/setup_sqlserver.py 在每台電腦第一次啟動時請管理者選。

    def test_unconfigured_computer_stops_instead_of_using_another_database(self):
        # 使用者決定：這台電腦沒設定好 SQL Server 就停下來說明。悄悄改用 SQLite 的話，對局會分散在兩個地方
        from django.core.exceptions import ImproperlyConfigured

        from config import sqlserver
        with patch.object(sqlserver, "load", return_value={"another-pc": {"mode": "auto", "host": "localhost"}}):
            with self.assertRaises(ImproperlyConfigured):
                sqlserver.database()

    def test_automatic_choice_follows_the_announced_order(self):
        # 管理者選「自動」前看到的說明就是這個順序：預設執行個體優先，LocalDB 最後（要用時才啟動，比較慢）
        from tools import setup_sqlserver
        self.assertEqual(setup_sqlserver.candidates(["SQL2022", "SQLEXPRESS", "MSSQLSERVER"], localdb=True),
                         ["localhost", r"localhost\SQLEXPRESS", r"localhost\SQL2022", setup_sqlserver.LOCALDB])
        self.assertEqual(setup_sqlserver.candidates([], localdb=False), [])

    def test_only_a_working_choice_is_remembered(self):
        # 手動輸入的名稱打錯、連不上時不能記下來，否則之後每次啟動都連不上，也不會再問；連得上才記住
        import pyodbc

        from config import sqlserver
        from tools import setup_sqlserver
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "sqlserver.json"
            with patch.object(sqlserver, "CONFIG_FILE", config), patch("builtins.print"), \
                    patch.object(pyodbc, "drivers", return_value=["ODBC Driver 18 for SQL Server"]):
                with patch("builtins.input", side_effect=["2", r"localhost\TYPO"]), \
                        patch.object(setup_sqlserver, "connect", side_effect=pyodbc.Error("08001", "x")):
                    self.assertEqual(setup_sqlserver.main(), 1)
                self.assertFalse(config.exists())
                with patch("builtins.input", side_effect=["2", r"localhost\SQL2022"]), \
                        patch.object(setup_sqlserver, "connect"):
                    self.assertEqual(setup_sqlserver.main(), 0)
            self.assertEqual(json.loads(config.read_text(encoding="utf-8"))[sqlserver.MACHINE],
                             {"mode": "manual", "host": r"localhost\SQL2022"})


class SqliteImportTests(TestCase):
    # 改用 SQL Server 之前的對局在 db.sqlite3，啟動時搬一次（manage.py import_sqlite_games）。
    databases = {"default", "sqlite"}  # 測試時 sqlite 是記憶體裡的測試資料庫，不會碰到真的 db.sqlite3

    def test_old_games_keep_their_numbers_times_notes_and_model_runs(self):
        # 復盤靠編號找對局，AiNote、ModelRun 靠編號對到 Game；歷史對局的時間也不能變成搬移的當下
        from arena.models import AiNote, Game, ModelRun
        old = datetime(2026, 10, 1, 8, 30, tzinfo=timezone.utc)
        game = Game.objects.using("sqlite").create(id=41, game_type="gomoku", human_seat=1, actions=["H8", "H9"])
        Game.objects.using("sqlite").filter(id=41).update(created_at=old, updated_at=old)
        AiNote.objects.using("sqlite").create(game=game, index=1, seat=2, facts={"kind": "ai_move", "points": []})
        ModelRun.objects.using("sqlite").create(kind="agent", game_type="gomoku", game=game, index=1, model="m",
                                                seconds=1.5, outcome="ok")
        call_command("import_sqlite_games", stdout=StringIO())
        saved = Game.objects.get(pk=41)
        self.assertEqual((saved.actions, saved.created_at, saved.updated_at), (["H8", "H9"], old, old))
        self.assertEqual(saved.ai_notes.get().index, 1)
        self.assertEqual(saved.model_runs.get().seconds, 1.5)
        # 新的對局不能用到搬過來的編號，否則會和舊對局混在一起
        self.assertGreater(Game.objects.create(game_type="gomoku", human_seat=1).id, 41)

    def test_nothing_is_copied_once_sql_server_has_data(self):
        # 每次啟動都會呼叫；SQL Server 已經有資料（連只有測速紀錄也算）時再搬，同一個編號會蓋掉原本那一筆
        from arena.models import Game, ModelRun
        here = ModelRun.objects.create(kind="benchmark", game_type="gomoku", model="here", seconds=2.0, outcome="ok")
        game = Game.objects.using("sqlite").create(game_type="gomoku", human_seat=1, actions=["H8"])
        ModelRun.objects.using("sqlite").create(id=here.id, kind="agent", game_type="gomoku", game=game,
                                                model="old", seconds=9.0, outcome="ok")
        call_command("import_sqlite_games", stdout=StringIO())
        self.assertFalse(Game.objects.exists())
        self.assertEqual(ModelRun.objects.get().model, "here")


class PublicRepositoryTests(SimpleTestCase):
    # 專案放在公開的 GitHub 儲存庫（2026-10-21 使用者決定）：研究用伺服器的位址與 SECRET_KEY 不能進去，
    # 只放在私人儲存庫（private/）或每台電腦自己的 config/local.json（config/local.py、tools/git_menu.py）。

    def test_a_commit_that_adds_the_server_address_is_refused(self):
        from tools import git_menu
        diff = ("diff --git a/docs/x.md b/docs/x.md\n--- a/docs/x.md\n+++ b/docs/x.md\n"
                "@@ -1 +1 @@\n-舊的說明\n+伺服器在 http://192.0.2.10:11434\n")
        self.assertEqual(git_menu.files_with_secrets(diff, ["192.0.2.10"]), ["docs/x.md"])

    def test_a_file_named_after_the_server_is_refused(self):
        # 測速檔以前用主機名稱命名；檔名本身也會公開
        from tools import git_menu
        diff = ("diff --git a/arena/model_benchmarks_192.0.2.10.json b/arena/model_benchmarks_192.0.2.10.json\n"
                "new file mode 100644\n--- /dev/null\n+++ b/arena/model_benchmarks_192.0.2.10.json\n@@ -0,0 +1 @@\n+{}\n")
        self.assertEqual(git_menu.files_with_secrets(diff, ["192.0.2.10"]), ["arena/model_benchmarks_192.0.2.10.json"])

    def test_removing_the_server_address_is_allowed(self):
        # 把機密從檔案裡拿掉正是要做的事，不能因為刪掉的那行含有機密就擋下
        from tools import git_menu
        diff = "--- a/docs/x.md\n+++ b/docs/x.md\n@@ -1 +1 @@\n-伺服器 192.0.2.10\n+研究用伺服器\n"
        self.assertEqual(git_menu.files_with_secrets(diff, ["192.0.2.10"]), [])

    def test_only_remote_servers_count_as_secret(self):
        # 本機 Ollama 的位址到處都寫著（測試、說明），擋下它會讓所有人都存不了檔
        from config import local
        from tools import git_menu
        with tempfile.TemporaryDirectory() as tmp:
            private, mine = Path(tmp) / "private.json", Path(tmp) / "local.json"
            private.write_text(json.dumps({"OLLAMA_URL": "http://192.0.2.10:11434"}), encoding="utf-8")
            mine.write_text(json.dumps({"OLLAMA_URL": "http://127.0.0.1:11434", "SECRET_KEY": "k" * 50}), encoding="utf-8")
            with patch.object(local, "PRIVATE_FILE", private), patch.object(local, "LOCAL_FILE", mine):
                self.assertEqual(sorted(git_menu.secret_values()), ["192.0.2.10", "k" * 50])

    def test_the_shared_address_wins_once_a_member_is_invited(self):
        # 還沒受邀時成員在本機輸入過位址（或選了本機 Ollama）；受邀後要改用全組共用的研究用伺服器
        from config import local
        with tempfile.TemporaryDirectory() as tmp:
            private, mine = Path(tmp) / "private.json", Path(tmp) / "local.json"
            mine.write_text(json.dumps({"OLLAMA_URL": "http://127.0.0.1:11434"}), encoding="utf-8")
            with patch.object(local, "PRIVATE_FILE", private), patch.object(local, "LOCAL_FILE", mine), \
                    patch.dict("os.environ", {}, clear=False) as env:
                env.pop("OLLAMA_URL", None)
                self.assertEqual(local.get("OLLAMA_URL"), "http://127.0.0.1:11434")
                private.write_text(json.dumps({"OLLAMA_URL": "http://192.0.2.10:11434"}), encoding="utf-8")
                self.assertEqual(local.get("OLLAMA_URL"), "http://192.0.2.10:11434")
                # 本機測試模式用環境變數暫時改用本機 Ollama，要能蓋過共用的設定
                env["OLLAMA_URL"] = "http://127.0.0.1:11434"
                self.assertEqual(local.get("OLLAMA_URL"), "http://127.0.0.1:11434")

    def test_each_machine_keeps_one_secret_key(self):
        # 第一次執行時產生，之後每次啟動都要是同一個，否則每次重開網站都會換一把
        from config import local
        with tempfile.TemporaryDirectory() as tmp:
            mine = Path(tmp) / "local.json"
            with patch.object(local, "PRIVATE_FILE", Path(tmp) / "none.json"), patch.object(local, "LOCAL_FILE", mine):
                first = local.secret_key()
                self.assertEqual(local.secret_key(), first)
            self.assertEqual(json.loads(mine.read_text(encoding="utf-8"))["SECRET_KEY"], first)
        self.assertNotIn("django-insecure", settings.SECRET_KEY)  # 原本寫死在 settings.py 的那把不能再用

    def test_a_git_worktree_finds_the_files_that_are_not_in_the_repository(self):
        # git worktree（例如審核送審時）裡沒有引擎、本機設定與 private/，要用主要專案資料夾裡的
        from config import local
        with tempfile.TemporaryDirectory() as tmp:
            main, tree = Path(tmp) / "main", Path(tmp) / "main" / ".claude" / "worktrees" / "pr-3"
            (main / ".git" / "worktrees" / "pr-3").mkdir(parents=True)
            tree.mkdir(parents=True)
            (tree / ".git").write_text(f"gitdir: {main / '.git' / 'worktrees' / 'pr-3'}\n", encoding="utf-8")
            self.assertEqual(local.main_checkout(tree), main.resolve())
            self.assertEqual(local.main_checkout(main), main)

    def test_the_work_log_goes_to_the_private_repository(self):
        # 工作紀錄是對話全文，有 private/ 時一定寫在那裡，不能留在會公開的專案根目錄
        import importlib.util
        spec = importlib.util.spec_from_file_location("log_progress", settings.BASE_DIR / ".claude" / "hooks" / "log_progress.py")
        hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook)
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            self.assertEqual(hook.log_path(project), project / "工作紀錄.md")
            (project / "private").mkdir()
            self.assertEqual(hook.log_path(project), project / "private" / "工作紀錄.md")

    def test_documents_go_only_to_the_private_repository(self):
        # .md 只給成員看（2026-10-21 使用者決定）：兩個儲存庫共用同一個資料夾，公開的看不到 .md 與 private/，
        # 私人儲存庫只管這些，不能把程式、套件或引擎裡的說明檔收進去。
        # 根目錄的 README.md 是公開頁面的說明，歸公開的儲存庫（2026-10-23 使用者決定）；其他資料夾的 README.md 仍是文件
        import shutil
        import subprocess
        from tools import git_menu
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("CLAUDE.md", "README.md", "docs/README.md", "docs/交接.md", ".claude/skills/x/SKILL.md",
                         "private/settings.json", "arena/views.py", ".venv/Lib/site-packages/pkg/README.md",
                         "tools/stockfish/README.md"):
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text("x", encoding="utf-8")
            shutil.copy(settings.BASE_DIR / ".gitignore", root / ".gitignore")
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "init", "-q", str(root / "private")], check=True)
            git_menu.write_exclude(root / ".git", git_menu.PUBLIC_EXCLUDE)
            git_menu.write_exclude(root / "private" / ".git", git_menu.PRIVATE_EXCLUDE)

            def untracked(*git):
                result = subprocess.run(["git", "-c", "core.quotepath=false", *git, "status", "--porcelain", "-uall"],
                                        cwd=root, capture_output=True, text=True, encoding="utf-8", check=True)
                return sorted(line[3:] for line in result.stdout.splitlines())

            self.assertEqual(untracked(), [".gitignore", "README.md", "arena/views.py"])
            self.assertEqual(untracked(f"--git-dir={root / 'private' / '.git'}", f"--work-tree={root}"),
                             [".claude/skills/x/SKILL.md", "CLAUDE.md", "docs/README.md", "docs/交接.md",
                              "private/settings.json"])

    def test_a_commit_that_adds_documents_to_the_public_repository_is_refused(self):
        # 排除清單還沒寫好的電腦、或用 git add -f 硬加的，存檔前也要擋下；只放行公開頁面的說明（根目錄的 README.md）
        from tools import git_menu
        self.assertEqual(git_menu.private_files(["docs/交接.md", "arena/views.py", "README.md", "docs/README.MD",
                                                 "private", "private/x.json"]),
                         ["docs/交接.md", "docs/README.MD", "private", "private/x.json"])


def git_in(folder, *args):
    """測試用：在 folder 執行 git，作者資料固定（測試的電腦不一定有設定）。"""
    import subprocess
    return subprocess.run(["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid",
                           "-c", "core.quotepath=false", *args], cwd=folder, capture_output=True, text=True,
                          encoding="utf-8", check=True).stdout.strip()


class SavePromptTests(SimpleTestCase):
    # 2026-10-21 使用者要求：Claude Code 的工作結束時，問成員要不要存檔或送審（.claude/hooks/ask_save.py），
    # 成員也能主動叫 Claude 存檔、送審（tools/git_menu.py --save／--submit，.claude/skills/save-work）。

    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("ask_save", settings.BASE_DIR / ".claude" / "hooks" / "ask_save.py")
        self.hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.hook)
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        (self.root / "a.py").write_text("x", encoding="utf-8")
        git_in(self.root, "init", "-q", "-b", "main")
        git_in(self.root, "add", "-A")
        git_in(self.root, "commit", "-q", "-m", "first")

    def tearDown(self):
        self.tmp.cleanup()

    def stop(self, **data):
        return self.hook.decide({"session_id": "s1", **data}, self.root)

    def test_unsaved_work_is_asked_about_once(self):
        # 問過「先不要」以後，同樣的改動每次回應完都再問一次會很煩；有新的改動才再問
        self.assertIsNone(self.stop())  # 沒有改動
        (self.root / "a.py").write_text("y", encoding="utf-8")
        asked = self.stop()
        self.assertEqual(asked["decision"], "block")
        self.assertIn("a.py", asked["reason"])
        self.assertIn("存檔並送審", asked["reason"])
        self.assertIn("存檔時會先開工作分支", asked["reason"])  # 在主線上改了程式，--save 會先開工作分支
        self.assertIsNone(self.stop())
        (self.root / "a.py").write_text("z", encoding="utf-8")
        self.assertIsNotNone(self.stop())

    def test_no_question_while_claude_is_already_asking(self):
        # Claude 因為這個 hook 繼續回應（去問成員）時再擋一次，就會一直迴圈
        (self.root / "a.py").write_text("y", encoding="utf-8")
        self.assertIsNone(self.stop(stop_hook_active=True))

    def test_a_muted_conversation_is_not_asked_again(self):
        with patch("builtins.print"):
            self.hook.mute(self.root, "s1")
        (self.root / "a.py").write_text("y", encoding="utf-8")
        self.assertIsNone(self.stop())
        self.assertIsNotNone(self.hook.decide({"session_id": "s2"}, self.root))

    def test_documents_are_asked_about_but_the_work_log_is_not(self):
        # 工作紀錄每次回應完都會變（Stop hook 寫的），算進去就會每次都問；文件只存進私人儲存庫，不用送審
        from tools import git_menu
        (self.root / "private").mkdir()
        (self.root / "private" / "工作紀錄.md").write_text("log", encoding="utf-8")
        (self.root / "CLAUDE.md").write_text("doc", encoding="utf-8")
        git_in(self.root, "init", "-q", "-b", "main", str(self.root / "private"))
        git_menu.write_exclude(self.root / ".git", git_menu.PUBLIC_EXCLUDE)
        git_menu.write_exclude(self.root / "private" / ".git", git_menu.PRIVATE_EXCLUDE)
        private = (f"--git-dir={self.root / 'private' / '.git'}", f"--work-tree={self.root}")
        git_in(self.root, *private, "add", "-A")
        git_in(self.root, *private, "commit", "-q", "-m", "docs")
        (self.root / "private" / "工作紀錄.md").write_text("log 2", encoding="utf-8")
        self.assertIsNone(self.stop())
        (self.root / "CLAUDE.md").write_text("doc 2", encoding="utf-8")
        asked = self.stop()
        self.assertIn("CLAUDE.md", asked["reason"])
        self.assertNotIn("存檔並送審", asked["reason"])
        # 只有文件時直接存進私人儲存庫、不開工作分支，提示不能說會開（2026-10-21 在新對話實際看到時發現）
        self.assertNotIn("開工作分支", asked["reason"])

    def clone_with_menu(self):
        """有 origin 的暫存專案，裡面放一份 tools/git_menu.py（它只管自己所在的專案，所以不能拿這個專案的來跑）。"""
        import shutil
        origin, work = self.root / "origin.git", self.root / "work"
        git_in(self.root, "init", "-q", "--bare", "-b", "main", str(origin))
        git_in(self.root, "clone", "-q", str(origin), str(work))
        # .gitignore 也要帶：git_menu 匯入 config/local.py 時會產生 __pycache__，沒忽略就被當成程式改動
        # （2026-11-02 在 LAPTOP-R80UAAH6 發現：只改文件的存檔因此開了工作分支，文件沒上傳）
        for name in ("tools/git_menu.py", "config/local.py", "config/__init__.py", ".gitignore"):
            (work / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(settings.BASE_DIR / name, work / name)
        (work / "b.py").write_text("x", encoding="utf-8")
        git_in(work, "add", "-A")
        git_in(work, "commit", "-q", "-m", "first")
        git_in(work, "push", "-q", "origin", "main")
        (work / "b.py").write_text("y", encoding="utf-8")
        return work

    def save(self, work, *args):
        import os
        import subprocess
        import sys
        return subprocess.run([sys.executable, str(work / "tools" / "git_menu.py"), "--save", *args], cwd=work,
                              capture_output=True, text=True, encoding="utf-8", env={**os.environ, "PYTHONIOENCODING": "utf-8"})

    def test_saving_on_main_starts_a_work_branch(self):
        # 主線只放審核過的版本：成員在主線上改了程式，Claude 存檔時要先開工作分支，主線不能多出存檔
        work = self.clone_with_menu()
        git_in(work, "config", "user.name", "test")
        git_in(work, "config", "user.email", "test@example.invalid")
        git_in(work, "config", "core.hooksPath", "tools/githooks")
        result = self.save(work, "改 b", "--name", "Fix B")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(git_in(work, "branch", "--show-current"), "work/fix-b")
        self.assertEqual(git_in(work, "rev-parse", "main"), git_in(work, "rev-parse", "origin/main"))
        self.assertEqual(git_in(work, "log", "-1", "--format=%s"), "改 b")

    def test_saving_refuses_a_machine_that_was_never_set_up(self):
        # 沒設定隱藏信箱的電腦存檔，真實信箱會公開在 GitHub；也沒有存檔前的機密檢查
        work = self.clone_with_menu()
        result = self.save(work, "改 b", "--name", "fix-b")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(git_in(work, "log", "--all", "--format=%s"), "first")

    def test_saving_only_documents_uploads_them_right_away(self):
        # 文件不經過送審：存了沒上傳，其他成員就一直看不到，存檔提醒也不會再提（它只看還沒存檔的改動）
        work = self.clone_with_menu()
        git_in(work, "checkout", "-q", "--", "b.py")  # 只留文件的改動
        origin, seed = self.root / "private-origin.git", self.root / "seed"
        git_in(self.root, "init", "-q", "--bare", "-b", "main", str(origin))
        (seed / "private").mkdir(parents=True)
        (seed / "private" / "settings.json").write_text("{}", encoding="utf-8")
        (seed / "CLAUDE.md").write_text("doc", encoding="utf-8")
        git_in(seed, "init", "-q", "-b", "main")
        git_in(seed, "add", "-A")
        git_in(seed, "commit", "-q", "-m", "seed")
        git_in(seed, "push", "-q", str(origin), "main")
        # 和 git_menu.fetch_private 一樣：只下載 git 資料，工作範圍設成整個專案資料夾再取出檔案
        git_in(work, "clone", "-q", "--no-checkout", str(origin), str(work / "private"))
        git_in(work / "private", "config", "core.worktree", "..")
        git_in(work, f"--git-dir={work / 'private' / '.git'}", f"--work-tree={work}", "checkout", "-q", "-f", "main")
        for folder in (work, work / "private"):
            git_in(folder, "config", "user.name", "test")
            git_in(folder, "config", "user.email", "test@example.invalid")
        git_in(work, "config", "core.hooksPath", "tools/githooks")
        (work / "CLAUDE.md").write_text("doc 2", encoding="utf-8")
        result = self.save(work, "更新文件")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(git_in(self.root, f"--git-dir={origin}", "log", "-1", "--format=%s"), "更新文件")
        self.assertEqual(git_in(work, "branch", "--show-current"), "main")  # 文件不用開工作分支

    def test_mute_without_a_conversation_id_explains_itself(self):
        # Claude 照提示執行時漏了編號：要看得懂哪裡錯，不能掉去讀 stdin、印出看不懂的 JSON 錯誤
        import os
        import subprocess
        import sys
        result = subprocess.run([sys.executable, str(settings.BASE_DIR / ".claude" / "hooks" / "ask_save.py"), "--mute"],
                                cwd=self.root, input="", capture_output=True, text=True, encoding="utf-8",
                                env={**os.environ, "CLAUDE_PROJECT_DIR": str(self.root), "PYTHONIOENCODING": "utf-8"})
        self.assertEqual(result.returncode, 2)
        self.assertIn("用法", result.stderr)
        self.assertNotIn("JSONDecodeError", result.stderr)

    def test_no_mute_option_without_a_conversation_id(self):
        (self.root / "a.py").write_text("y", encoding="utf-8")
        asked = self.hook.decide({}, self.root)
        self.assertNotIn("--mute", asked["reason"])

    def test_member_list_flags_people_in_only_one_repository(self):
        # 只在公開儲存庫的成員拿不到文件與伺服器位址，只在私人的不能上傳程式，兩邊都要有才算加入完成
        from tools import git_menu
        joined = {"FZR0": {"公開", "私人"}, "alice": {"公開"}, "bob": {"私人"}}
        self.assertEqual(git_menu.incomplete(joined), ["alice（只在公開儲存庫）", "bob（只在私人儲存庫）"])

class RunLogTests(TestCase):
    client_class = AdminClient
    # 使用者 2026-10-28：啟動檔沒有運行紀錄，出了問題分不出是 AI 伺服器、AI 助理還是程式。
    # 紀錄要讓人一眼看出是哪一邊：伺服器連不上、AI 助理被擋下、改用原本的程式補完，各自有一行。
    def setUp(self):
        narrator._retry_at = 0.0
        self.addCleanup(setattr, narrator, "_retry_at", 0.0)
        # 逾時後的原因檢查（health）會再連一次埠；測試不能真的連到任何位址
        connect = patch("arena.health._connect", return_value=("failed", "連不上（timed out）"))
        connect.start()
        self.addCleanup(connect.stop)
        self.game = Game.objects.create(game_type="gomoku", human_seat=1, actions=[])

    def move(self, action="H8"):
        return self.client.post(f"/api/gomoku/games/{self.game.id}/move/", json.dumps({"action": action}),
                                content_type="application/json")

    def test_a_server_problem_is_logged_as_the_servers(self):
        with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")), \
                self.assertLogs("arena", "WARNING") as logs:
            self.assertEqual(self.move().json()["handled"]["by"], "fallback")
        text = "\n".join(logs.output)
        self.assertIn("呼叫本機的 gemma4:26b 失敗（連不上或逾時", text)
        self.assertIn("改用原本的程式補完", text)

    @override_settings(OLLAMA_URL="http://10.20.30.40:11434", OLLAMA_SERVER_NAMES={"10.20.30.40": "research"})
    def test_the_research_servers_address_never_reaches_the_log(self):
        # 紀錄檔可能被複製給別人看；位址不能公開（2026-10-21），只寫代稱
        with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")), \
                self.assertLogs("arena", "INFO") as logs:
            self.move()
        text = "\n".join(logs.output)
        self.assertIn("research 伺服器", text)
        self.assertNotIn("10.20.30.40", text)

    def test_every_step_of_the_assistant_is_logged_and_refusals_stand_out(self):
        # 看得出 AI 助理做了什麼；它想替玩家換一步棋被擋下時是 WARNING，不會淹沒在一般紀錄裡
        cheat = scripted([("play_human_move", {"game_id": self.game.id, "action": "I9"})])
        with patch("arena.orchestrator._chat", side_effect=cheat), \
                patch("arena.agent._chat", side_effect=OSError("測試不連 Ollama")), \
                self.assertLogs("arena", "INFO") as logs:
            self.move()
        refused = [r for r in logs.records if "play_human_move" in r.getMessage()]
        self.assertEqual(len(refused), 1)
        self.assertEqual(refused[0].levelname, "WARNING")
        self.assertIn("I9", refused[0].getMessage())
        self.assertIn("action 必須是 H8", refused[0].getMessage())
        self.assertTrue(any("請求：下棋" in r.getMessage() and r.levelname == "WARNING" for r in logs.records))


REMOTE = override_settings(OLLAMA_URL="http://192.0.2.10:11434", OLLAMA_SERVER_NAMES={"192.0.2.10": "research"})


class AiHealthTests(TestCase):
    client_class = AdminClient
    # 使用者 2026-10-30：與其每個請求都去連、都失敗，不如出問題時檢查一次，告訴使用者原因與排除步驟；
    # 查不出原因時列出可能原因並附故障報告。原因判斷錯了，使用者就會照錯的步驟去修。
    BODY = {"model": "gemma4:26b", "messages": []}
    TIMEOUT = TimeoutError("timed out")

    def setUp(self):
        narrator._retry_at = 0.0
        health._current = None
        self.addCleanup(setattr, narrator, "_retry_at", 0.0)
        self.addCleanup(setattr, health, "_current", None)

    def chat_failing(self, error, connect=("failed", "連不上（timed out）"), installed=TimeoutError("timed out")):
        """narrator.chat 失敗一次，回傳檢測到的問題；檢測時的連線（埠、模型清單）都是假的。"""
        tags = (patch("arena.health._installed", side_effect=installed) if isinstance(installed, Exception)
                else patch("arena.health._installed", return_value=installed))
        with patch("urllib.request.urlopen", side_effect=error), \
                patch("arena.health._connect", return_value=connect), tags, self.assertRaises(OSError):
            narrator.chat(self.BODY)
        return health.current()

    def refused(self):
        return urllib.error.URLError(ConnectionRefusedError(10061, "目標電腦拒絕連線"))

    def test_local_ollama_that_is_installed_but_not_running_says_how_to_start_it(self):
        with patch("arena.health.OLLAMA_EXE", Path(__file__)):
            p = self.chat_failing(self.refused())
        self.assertEqual(p["title"], "這台電腦的 Ollama 沒有啟動")
        self.assertTrue(p["known"])
        self.assertTrue(any("ollama serve" in s for s in p["steps"]))

    def test_local_ollama_that_is_not_installed_says_so(self):
        # 啟動檔本來該用研究用伺服器：先提醒確認位址，而不是叫人去裝 Ollama
        with patch("arena.health.OLLAMA_EXE", Path("C:/不存在/ollama.exe")), patch("shutil.which", return_value=None):
            p = self.chat_failing(self.refused())
        self.assertEqual(p["title"], "這台電腦沒有安裝 Ollama")
        self.assertIn("不是本機", p["steps"][0])

    @REMOTE
    def test_remote_server_that_refuses_means_ollama_is_not_running_there(self):
        p = self.chat_failing(self.refused())
        self.assertEqual(p["title"], "research 伺服器有開機，但 Ollama 沒有在接收連線")
        self.assertTrue(any("OLLAMA_HOST=0.0.0.0" in s for s in p["steps"]))

    @REMOTE
    def test_unknown_server_name_is_a_known_cause_and_the_address_never_shows(self):
        p = self.chat_failing(urllib.error.URLError(socket.gaierror(11001, "getaddrinfo failed: 192.0.2.10")))
        self.assertEqual(p["title"], "找不到 research 伺服器（伺服器名稱查不到）")
        self.assertTrue(p["known"])
        # 畫面與故障報告可能被轉給別人；研究用伺服器的位址不能公開（2026-10-21）
        self.assertNotIn("192.0.2.10", json.dumps(p, ensure_ascii=False))

    @REMOTE
    def test_timeout_without_any_connection_lists_possible_causes_and_a_report(self):
        p = self.chat_failing(self.TIMEOUT)
        self.assertEqual(p["title"], "連不到 research 伺服器（沒有回應）")
        self.assertFalse(p["known"])
        self.assertTrue(any("VPN" in c for c in p["causes"]))
        self.assertIn("連線到埠 11434：連不上", p["report"])
        self.assertNotIn("192.0.2.10", p["report"])

    @REMOTE
    def test_timeout_when_the_port_answers_but_ollama_does_not_means_it_is_hung(self):
        # 研究用伺服器卡住過三次，都是重新啟動 Ollama 才恢復
        p = self.chat_failing(self.TIMEOUT, connect=("ok", "連得上"))
        self.assertEqual(p["title"], "research 伺服器的 Ollama 沒有回應（可能卡住了）")
        self.assertIn("重新啟動", p["steps"][0])

    @REMOTE
    def test_timeout_with_a_healthy_server_tells_missing_model_from_slow_model(self):
        p = self.chat_failing(self.TIMEOUT, connect=("ok", "連得上"), installed={"qwen3:8b"})
        self.assertEqual(p["title"], "research 伺服器上沒有模型 gemma4:26b")
        health._current = None
        narrator._retry_at = 0.0
        p = self.chat_failing(self.TIMEOUT, connect=("ok", "連得上"), installed={"gemma4:26b"})
        self.assertTrue(p["title"].endswith(f"沒有在 {settings.OLLAMA_TIMEOUT} 秒內回答"))
        self.assertFalse(p["known"])  # 冷啟動、太忙、卡住都有可能，只能列可能原因

    def test_model_not_found_reply_says_which_model_to_pull(self):
        body = json.dumps({"error": "model 'gemma4:26b' not found"}).encode()
        error = urllib.error.HTTPError(f"{settings.OLLAMA_URL}/api/chat", 404, "Not Found", None, io.BytesIO(body))
        p = self.chat_failing(error)
        self.assertEqual(p["title"], "本機上沒有模型 gemma4:26b")
        self.assertIn("ollama pull gemma4:26b", p["steps"][0])

    def test_the_check_runs_once_and_nothing_connects_until_the_pause_ends(self):
        # 這就是使用者要的：出問題時查一次，暫停期間的請求（含設定頁的模型清單）都不再連線
        clock = [1000.0]
        with patch("time.monotonic", side_effect=lambda: clock[0]), \
                patch("urllib.request.urlopen", side_effect=self.TIMEOUT) as urlopen, \
                patch("arena.health._connect", return_value=("failed", "連不上")) as connect:
            with self.assertRaises(OSError):
                narrator.chat(self.BODY)
            first = health.current()
            with self.assertRaises(OSError):
                narrator.chat(self.BODY)
            self.assertIsNone(narrator.installed_models())
            self.assertEqual((urlopen.call_count, connect.call_count), (1, 1))
            self.assertIs(health.current(), first)
        clock[0] += settings.OLLAMA_RETRY_AFTER + 1
        reply = io.BytesIO(json.dumps({"message": {"content": "好"}}).encode())
        with patch("time.monotonic", side_effect=lambda: clock[0]), patch("urllib.request.urlopen", return_value=reply):
            narrator.chat(self.BODY)
        self.assertIsNone(health.current())  # 修好後自動恢復，畫面的說明跟著收起來

    def test_a_lasting_problem_is_reported_once(self):
        # 伺服器整天連不上時，每 3 分鐘一份報告會塞滿資料夾；同一個問題只存第一次
        with tempfile.TemporaryDirectory() as folder, override_settings(FAULT_REPORT_DIR=Path(folder) / "故障報告"):
            for _ in range(2):
                narrator._retry_at = 0.0
                self.chat_failing(self.TIMEOUT)
            files = list((Path(folder) / "故障報告").iterdir())
            self.assertEqual(len(files), 1)
            self.assertIn("判斷：連不到", files[0].read_text(encoding="utf-8"))

    def test_pages_and_responses_show_the_problem_and_drop_it_when_fixed(self):
        with patch("arena.health.OLLAMA_EXE", Path(__file__)):
            health.report_failure(self.refused(), "gemma4:26b")
        narrator._retry_at = 1e12  # 暫停中：請求不連線，沿用檢測結果
        page = self.client.get("/").content.decode()
        self.assertIn("AI 暫時無法使用：這台電腦的 Ollama 沒有啟動", page)
        self.assertNotIn("AI 助理目前連不上 AI 伺服器", page)  # 已經說明原因，不再重複一般的提示
        data = self.client.get("/api/gomoku/models/").json()
        self.assertEqual(data["ai_problem"]["title"], "這台電腦的 Ollama 沒有啟動")
        health._current = None
        self.assertIsNone(self.client.get("/api/gomoku/models/").json()["ai_problem"])


class AccountTests(TestCase):
    """帳號（2026-11-05，docs/交接.md 4.10）：網站要放到 Cloudflare 通道上給別人連，
    所以沒登入什麼都不能用（否則別人能用研究用伺服器），各人只看得到自己的對局，管理員看得到登入 IP 與在線的人。"""

    def setUp(self):
        self.alice = User.objects.create_user("alice", password="pw-for-tests-1")
        self.bob = User.objects.create_user("bob", password="pw-for-tests-2")

    def client_for(self, user):
        client = Client()
        client.force_login(user)
        return client

    def test_nothing_works_without_logging_in(self):
        # 頁面與 API 都要登入；沒登入的請求不能走到 AI 助理（每個頁面都會呼叫模型）
        with patch.object(orchestrator, "handle", side_effect=AssertionError("沒登入不能呼叫 AI 助理")):
            for url in ["/", "/history/", "/guide/", "/gomoku/", "/api/gomoku/games/", "/api/gomoku/models/"]:
                response = Client().get(url)
                self.assertEqual(response.status_code, 302, url)
                self.assertTrue(response["Location"].startswith("/accounts/login/"), url)
            self.assertEqual(Client().post("/api/gomoku/games/", "{}", content_type="application/json").status_code, 302)

    def test_login_and_signup_pages_never_reach_the_ai_assistant(self):
        # 密碼不能交給模型；登入頁在 AI 伺服器連不上時也要能用
        with patch.object(orchestrator, "handle", side_effect=AssertionError("登入頁不能呼叫 AI 助理")):
            self.assertEqual(Client().get("/accounts/login/").status_code, 200)
            self.assertEqual(Client().get("/accounts/signup/").status_code, 200)

    def test_anyone_can_sign_up_and_is_logged_in_with_their_ip_recorded(self):
        client = Client()
        response = client.post("/accounts/signup/", {"username": "carol", "name": "小卡", "password1": "a-Long-pass-77",
                                                     "password2": "a-Long-pass-77"}, REMOTE_ADDR="203.0.113.5")
        self.assertRedirects(response, "/", fetch_redirect_response=False)
        carol = User.objects.get(username="carol")
        self.assertEqual(int(client.session["_auth_user_id"]), carol.id)
        self.assertEqual(LoginRecord.objects.get(user=carol).ip, "203.0.113.5")

    def test_login_records_the_real_ip_behind_the_cloudflare_tunnel(self):
        # 經過 cloudflared 時連線來自本機，真正的位址在 CF-Connecting-IP
        Client().post("/accounts/login/", {"username": "alice", "password": "pw-for-tests-1"},
                      REMOTE_ADDR="127.0.0.1", HTTP_CF_CONNECTING_IP="198.51.100.7")
        self.assertEqual(LoginRecord.objects.get(user=self.alice).ip, "198.51.100.7")

    def test_a_forged_cloudflare_header_from_elsewhere_is_ignored(self):
        # 不是從本機（cloudflared）來的連線，標頭是使用者自己填的，不能信
        Client().post("/accounts/login/", {"username": "alice", "password": "pw-for-tests-1"},
                      REMOTE_ADDR="203.0.113.9", HTTP_CF_CONNECTING_IP="198.51.100.7")
        self.assertEqual(LoginRecord.objects.get(user=self.alice).ip, "203.0.113.9")

    def test_players_only_see_their_own_games(self):
        mine = Game.objects.create(game_type="gomoku", owner=self.alice, human_seat=1, actions=["H8"])
        alice, bob = self.client_for(self.alice), self.client_for(self.bob)
        with patch.object(orchestrator, "_chat", side_effect=OSError("測試不連模型")):
            self.assertContains(alice.get("/history/"), f"?review={mine.id}")
            self.assertNotContains(bob.get("/history/"), f"?review={mine.id}")
            self.assertEqual(bob.get(f"/api/gomoku/games/{mine.id}/").status_code, 404)
            self.assertEqual(bob.get(f"/api/gomoku/games/{mine.id}/review/").status_code, 404)
            # 別人的對局不能替他下、也不能替他結束
            move = bob.post(f"/api/gomoku/games/{mine.id}/move/", json.dumps({"action": "H9"}),
                            content_type="application/json")
            self.assertEqual(move.status_code, 404)
            self.assertEqual(bob.post(f"/games/{mine.id}/end/").status_code, 404)
        mine.refresh_from_db()
        self.assertEqual((mine.actions, mine.ended), (["H8"], False))

    def test_new_games_belong_to_the_player_who_started_them(self):
        with patch.object(orchestrator, "_chat", side_effect=OSError("測試不連模型")), \
                patch.object(agent, "_chat", side_effect=OSError("測試不連模型")):
            response = self.client_for(self.alice).post("/api/gomoku/games/", json.dumps({"seat": 1}),
                                                        content_type="application/json")
        self.assertEqual(Game.objects.get(pk=response.json()["game"]["id"]).owner, self.alice)

    def test_old_games_without_an_owner_are_only_visible_to_admins(self):
        old = Game.objects.create(game_type="gomoku", human_seat=1, actions=["H8"])
        admin = User.objects.create_superuser("boss")
        with patch.object(orchestrator, "_chat", side_effect=OSError("測試不連模型")):
            self.assertEqual(self.client_for(admin).get(f"/api/gomoku/games/{old.id}/").status_code, 200)
            self.assertEqual(self.client_for(self.alice).get(f"/api/gomoku/games/{old.id}/").status_code, 404)


class PresenceTests(TestCase):
    """後台要看得到目前有誰在瀏覽網站（使用者要求，2026-11-05），包括還沒登入、停在登入頁的訪客。"""

    def test_visitors_are_recorded_with_their_account_and_ip(self):
        Client().get("/accounts/login/", REMOTE_ADDR="203.0.113.20")
        user = User.objects.create_user("dave", password="x")
        client = Client()
        client.force_login(user)
        with patch.object(orchestrator, "_chat", side_effect=OSError("測試不連模型")):
            client.get("/guide/", REMOTE_ADDR="203.0.113.21")
        anonymous = Presence.objects.get(ip="203.0.113.20")
        self.assertIsNone(anonymous.user)
        self.assertEqual(anonymous.path, "/accounts/login/")
        self.assertEqual(Presence.objects.get(ip="203.0.113.21").user, user)

    def test_admin_lists_only_people_online_by_default(self):
        from django.utils import timezone as dj_timezone
        Presence.objects.create(session_key="old", ip="203.0.113.30",
                                last_seen=dj_timezone.now() - Presence.ONLINE - Presence.ONLINE)
        Presence.objects.create(session_key="now", ip="203.0.113.31")
        admin = Client()
        admin.force_login(User.objects.create_superuser("boss"))
        page = admin.get("/admin/arena/presence/")
        self.assertContains(page, "203.0.113.31")
        self.assertNotContains(page, "203.0.113.30")
        self.assertContains(admin.get("/admin/arena/presence/?status=all"), "203.0.113.30")
        self.assertEqual(admin.get("/admin/arena/loginrecord/").status_code, 200)


class PvpTests(TestCase):
    """真人對戰（2026-11-05，docs/交接.md 4.10）：兩位真人各自登入、用房間密碼加入，各下自己的座位；
    對戰中不能用 AI 教練（否則是幫其中一方作弊），結束後每個人從自己的座位復盤。"""

    def setUp(self):
        self.alice = User.objects.create_user("alice", password="x")
        self.bob = User.objects.create_user("bob", password="x")
        self.carol = User.objects.create_user("carol", password="x")
        self.clients = {}
        for user in (self.alice, self.bob, self.carol):
            self.clients[user.username] = Client()
            self.clients[user.username].force_login(user)
        patcher = patch.object(orchestrator, "_chat", side_effect=OSError("測試不連模型"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def post(self, who, url, body):
        return self.clients[who].post(url, json.dumps(body), content_type="application/json")

    def open_room(self, seat=1):
        r = self.post("alice", "/api/gomoku/games/", {"seat": seat, "mode": "pvp"}).json()
        return Game.objects.get(pk=r["game"]["id"]), r

    def join(self, who, code):
        return self.clients[who].post("/join/", {"code": code})

    def test_a_friend_joins_with_the_room_code_and_takes_the_other_seat(self):
        game, r = self.open_room(seat=2)
        self.assertTrue(r["game"]["waiting"])
        self.assertEqual(len(r["game"]["room_code"]), 6)
        self.assertEqual(r["ai_moves"], [])  # 沒有 AI：黑棋是真人，等對手加入
        response = self.join("bob", game.room_code.lower())  # 小寫也認得
        self.assertRedirects(response, f"/gomoku/?resume={game.id}", fetch_redirect_response=False)
        game.refresh_from_db()
        self.assertEqual(game.seat_of(self.bob), 1)
        self.assertFalse(game.waiting)

    def test_opening_an_invite_link_does_not_join_until_confirmed(self):
        # 用 GET 就加入的話，別的網站放一張圖片就能讓人坐進陌生的對局
        game, _ = self.open_room()
        page = self.clients["bob"].get(f"/join/{game.room_code}/")
        self.assertContains(page, game.room_code)
        game.refresh_from_db()
        self.assertTrue(game.waiting)

    def test_no_moves_until_the_opponent_joins(self):
        game, _ = self.open_room()
        self.assertEqual(self.post("alice", f"/api/gomoku/games/{game.id}/move/", {"action": "H8"}).status_code, 400)

    def test_each_player_only_moves_for_their_own_seat_and_no_ai_plays(self):
        game, _ = self.open_room()
        self.join("bob", game.room_code)
        start_now(game)
        url = f"/api/gomoku/games/{game.id}/move/"
        self.assertEqual(self.post("bob", url, {"action": "H8"}).status_code, 400)  # 黑棋是 alice
        r = self.post("alice", url, {"action": "H8"}).json()
        self.assertEqual(r["ai_moves"], [])  # 白棋是真人，AI 不能替 bob 下
        self.assertEqual(self.post("alice", url, {"action": "H9"}).status_code, 400)  # 輪到 bob
        self.assertEqual(self.post("bob", url, {"action": "H9"}).status_code, 200)
        game.refresh_from_db()
        self.assertEqual(game.actions, ["H8", "H9"])
        self.assertFalse(game.ai_notes.exists())

    def test_a_third_person_cannot_see_the_game_until_joining_and_a_full_room_makes_them_a_spectator(self):
        game, _ = self.open_room()
        self.join("bob", game.room_code)
        self.assertEqual(self.clients["carol"].get(f"/api/gomoku/games/{game.id}/").status_code, 404)
        # 座位坐滿時按「加入」改成觀戰（使用者要求，2026-11-13），不會搶走任何人的座位
        self.assertRedirects(self.join("carol", game.room_code), f"/gomoku/?watch={game.id}&full=1",
                             fetch_redirect_response=False)
        game.refresh_from_db()
        self.assertIsNone(game.seat_of(self.carol))
        self.assertEqual((game.seat_of(self.alice), game.seat_of(self.bob)), (1, 2))
        self.assertTrue(game.spectators.filter(user=self.carol).exists())
        self.assertIsNone(self.clients["carol"].get(f"/api/gomoku/games/{game.id}/").json()["my_seat"])

    def test_the_ai_coach_is_off_until_the_game_ends(self):
        game, _ = self.open_room()
        self.join("bob", game.room_code)
        start_now(game)
        self.post("alice", f"/api/gomoku/games/{game.id}/move/", {"action": "H8"})
        with patch.object(orchestrator, "handle", wraps=orchestrator.handle) as handle:
            for who in ("alice", "bob"):
                self.assertEqual(self.clients[who].get(f"/api/gomoku/games/{game.id}/review/").status_code, 400)
                ask = self.post(who, f"/api/gomoku/games/{game.id}/ask/", {"question": "下哪裡好？", "at": 1})
                self.assertEqual(ask.status_code, 400)
            self.assertFalse(any(c.args[0] in ("review", "ask") for c in handle.call_args_list))

    def test_pvp_games_cannot_be_ended_without_a_result(self):
        # 快輸的一方按「不計勝負」就能讓輸棋不算；要結束只能認輸
        game, _ = self.open_room()
        self.join("bob", game.room_code)
        self.assertEqual(self.post("bob", f"/api/gomoku/games/{game.id}/end/", {}).status_code, 400)
        self.clients["bob"].post(f"/games/{game.id}/end/")
        game.refresh_from_db()
        self.assertFalse(game.ended)

    def test_after_the_game_each_player_reviews_their_own_moves(self):
        game, _ = self.open_room()
        self.join("bob", game.room_code)
        start_now(game)
        self.post("alice", f"/api/gomoku/games/{game.id}/move/", {"action": "H8"})
        self.post("bob", f"/api/gomoku/games/{game.id}/move/", {"action": "A1"})
        self.post("alice", f"/api/gomoku/games/{game.id}/move/", {"action": "resign"})
        game.refresh_from_db()
        self.assertEqual(game.winner, 2)
        for who, seat in (("alice", 1), ("bob", 2)):
            r = self.clients[who].get(f"/api/gomoku/games/{game.id}/review/")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["moments"], GOMOKU.review(game.settings, game.actions, seat))
            detail = self.clients[who].get(f"/api/gomoku/games/{game.id}/").json()
            self.assertEqual(detail["my_seat"], seat)
        # 戰績：bob 贏、alice 輸
        self.assertEqual(operations._result(game, self.bob), "win")
        self.assertEqual(operations._result(game, self.alice), "loss")


class RealtimeTests(TestCase):
    """真人對戰的 WebSocket（arena/realtime.py）：只有坐在這盤的真人連得上；有人下了一步就通知，
    每條連線依自己的座位重新產生畫面（和打開對局用同一個函式），不經過 AI 助理。"""

    def setUp(self):
        self.alice = User.objects.create_user("alice", password="x")
        self.bob = User.objects.create_user("bob", password="x")
        self.game, _ = operations.start_game(GOMOKU, 1, {}, self.alice, "pvp")
        operations.join_game(self.game.room_code, self.bob)
        start_now(self.game)

    def test_only_players_of_this_pvp_game_may_connect(self):
        from django.contrib.auth.models import AnonymousUser
        from . import realtime
        self.assertEqual(realtime.seat_for(self.alice, self.game.id)[1], 1)
        self.assertEqual(realtime.seat_for(self.bob, self.game.id)[1], 2)
        self.assertEqual(realtime.seat_for(User.objects.create_user("carol"), self.game.id), (None, None))
        self.assertEqual(realtime.seat_for(AnonymousUser(), self.game.id), (None, None))
        ai_game, _ = operations.start_game(GOMOKU, 1, {}, self.alice)
        self.assertEqual(realtime.seat_for(self.alice, ai_game.id), (None, None))  # 和 AI 的對局不用即時同步

    def test_a_move_notifies_everyone_in_the_game(self):
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer
        from . import realtime
        layer = get_channel_layer()
        channel = async_to_sync(layer.new_channel)()
        async_to_sync(layer.group_add)(f"game_{self.game.id}", channel)
        client = Client()
        client.force_login(self.alice)
        with patch.object(orchestrator, "_chat", side_effect=OSError("測試不連模型")):
            client.post(f"/api/gomoku/games/{self.game.id}/move/", json.dumps({"action": "H8"}),
                        content_type="application/json")
        self.assertEqual(async_to_sync(layer.receive)(channel), {"type": "game.changed"})

    def test_each_connection_gets_the_position_from_its_own_seat_without_the_ai_assistant(self):
        from . import realtime
        sent = []
        consumer = realtime.GameConsumer()
        consumer.game_id, consumer.seat = self.game.id, 2
        consumer.scope = {"user": self.bob}  # 每次推送都依這個帳號現在的座位重算（房間裡可以換座位，2026-11-15）
        consumer.send_json = sent.append
        with patch.object(orchestrator, "handle", side_effect=AssertionError("推送不經過 AI 助理")):
            consumer.game_changed({"type": "game.changed"})
        self.game.refresh_from_db()
        self.assertEqual(sent, [{"type": "update", **operations.game_detail(GOMOKU, self.game, 2, self.bob)}])


class OpponentObservationTests(TestCase):
    """對手觀察（使用者要求，2026-11-05）：真人對戰結束後，告訴玩家對手有什麼傾向、在第幾步出現跡象、知道後可以怎麼應對。
    傾向必須來自電腦對對手每一步的檢查（同一套復盤），應對建議由程式寫好，語言模型只能改寫。"""

    def setUp(self):
        self.alice = User.objects.create_user("alice", password="x")
        self.bob = User.objects.create_user("bob", password="x")

    def play(self, moves):
        game, _ = operations.start_game(GOMOKU, 1, {}, self.alice, "pvp")
        operations.join_game(game.room_code, self.bob)
        game = start_now(game)
        for i, move in enumerate(moves):
            operations.play_human_move(GOMOKU, game, move, 1 + i % 2)
        return game

    def test_names_the_habit_the_step_it_showed_and_how_to_respond(self):
        # 黑棋（alice）第 9 手沒有擋白棋 A1–A4 的衝四，白棋下一手連五
        game = self.play(["H8", "A1", "J10", "A2", "L3", "A3", "C12", "A4", "N14", "A5"])
        self.assertEqual(game.winner, 2)
        rivals = operations.review(GOMOKU, game, 2)["opponents"]
        self.assertEqual([r["seat"] for r in rivals], [1])  # bob 只觀察對手，不觀察自己
        texts = [p["text"] for p in rivals[0]["facts"]["points"]]
        habit = next(t for t in texts if "沒有擋住對手的衝四" in t)
        self.assertIn("alice（黑棋）", habit)
        self.assertIn("第 9 手", habit)
        self.assertIn(GOMOKU.opponent_tips["沒有擋住對手的衝四"], " ".join(texts))
        self.assertIn(8, [s["index"] for s in rivals[0]["signs"]])
        self.assertEqual(rivals[0]["facts"]["kind"], "opponent")

    def test_each_player_reads_the_result_from_their_own_side(self):
        # 2026-11-16 情境測試：總評只寫「白棋（真人）贏了」時，兩個人看到同一句話，模型不一定說得清楚誰輸誰贏
        game = self.play(["H8", "A1", "J10", "A2", "L3", "A3", "C12", "A4", "N14", "A5"])
        first = {seat: operations.review(GOMOKU, game, seat)["overall"]["points"][0]["text"] for seat in (1, 2)}
        self.assertEqual(first, {1: "結果：你（黑棋）輸了，白棋（真人）贏了。", 2: "結果：你（白棋）贏了。"})

    def test_advice_is_marked_as_experience_not_a_computed_fact(self):
        game = self.play(["H8", "A1", "J10", "A2", "L3", "A3", "C12", "A4", "N14", "A5"])
        points = operations.review(GOMOKU, game, 2)["opponents"][0]["facts"]["points"]
        for p in points:
            if "應對建議" in p["text"]:
                self.assertEqual(p["evidence"], ESTIMATE)
                self.assertIn("經驗做法", p["text"])

    def test_games_against_the_ai_have_no_opponent_observation(self):
        # AI 的每一步就是引擎的選擇，用同一套標準檢查本來就找不到問題
        game, _ = operations.start_game(GOMOKU, 1, {}, self.alice)
        operations.play_human_move(GOMOKU, game, "H8", 1)
        self.assertEqual(operations.review(GOMOKU, game, 1)["opponents"], [])

    def test_the_language_model_is_told_to_add_nothing_beyond_the_facts(self):
        self.assertIn("opponent", narrator.INTROS)
        self.assertIn("不要加上事實裡沒有的應對方法", narrator.INTROS["opponent"])


class JoinRaceTests(TestCase):
    """兩個人同時用房間密碼加入時，同一個座位只能給一個人（2026-11-05 審核 #10 時發現）：
    先讀到空位、再存檔的話，後存的會蓋掉先存的，被蓋掉的人以為加入了其實沒有座位。"""

    def test_a_seat_taken_in_between_is_not_overwritten(self):
        alice = User.objects.create_user("alice")
        bob = User.objects.create_user("bob")
        carol = User.objects.create_user("carol")
        game, _ = operations.start_game(GOMOKU, 1, {}, alice, "pvp")
        seat = game.players.get(user__isnull=True)
        real_filter = game.players.filter

        class ReadThenBobSits:
            """carol 讀到空位之後、寫入之前，bob 先坐下了。"""
            def __init__(self, *args, **kwargs):
                self.qs = real_filter(*args, **kwargs)

            def order_by(self, *fields):
                rows = list(self.qs.order_by(*fields))
                GamePlayer.objects.filter(pk=seat.pk).update(user=bob)
                return rows

        with patch.object(type(game.players), "filter", side_effect=ReadThenBobSits):
            _, carol_seat = operations.join_game(game.room_code, carol)
        self.assertIsNone(carol_seat)  # 沒搶到座位：改成觀戰（2026-11-13），不是坐進 bob 的位子
        seat.refresh_from_db()
        self.assertEqual(seat.user, bob)


class GuestTests(TestCase):
    """遊客（使用者要求，2026-11-06）：不註冊也能玩，能做的事和帳號一樣；之後設定帳號名稱與密碼就變成正式帳號，
    下過的對局、戰績、真人對戰的座位都要跟著保留（是同一個帳號，不是搬資料）。"""

    def enter_as_guest(self, client=None, **extra):
        client = client or Client()
        response = client.post("/accounts/guest/", extra.pop("data", {}), REMOTE_ADDR="203.0.113.40", **extra)
        return client, response, User.objects.get(pk=client.session["_auth_user_id"])

    def test_a_guest_is_a_logged_in_account_without_a_password(self):
        with patch.object(orchestrator, "handle", side_effect=AssertionError("遊客登入不經過 AI 助理")):
            client, response, guest = self.enter_as_guest()
        self.assertRedirects(response, "/", fetch_redirect_response=False)
        self.assertTrue(guest.username.startswith("遊客-"))
        self.assertFalse(guest.has_usable_password())  # 沒有密碼：別人不能用這個名稱登入
        self.assertEqual(LoginRecord.objects.get(user=guest).ip, "203.0.113.40")

    def test_an_invite_link_still_leads_to_the_join_page(self):
        _, response, _ = self.enter_as_guest(data={"next": "/join/ABCDEF/"})
        self.assertRedirects(response, "/join/ABCDEF/", fetch_redirect_response=False)
        _, response, _ = self.enter_as_guest(data={"next": "https://evil.example.com/"})
        self.assertRedirects(response, "/", fetch_redirect_response=False)

    def test_guests_can_play_and_join_pvp_like_accounts(self):
        alice = User.objects.create_user("alice", password="x")
        game, _ = operations.start_game(GOMOKU, 1, {}, alice, "pvp")
        client, _, guest = self.enter_as_guest()
        with patch.object(orchestrator, "_chat", side_effect=OSError("測試不連模型")):
            client.post("/join/", {"code": game.room_code})
        game.refresh_from_db()
        self.assertEqual(game.seat_of(guest), 2)

    def test_upgrading_keeps_the_same_account_and_its_games(self):
        client, _, guest = self.enter_as_guest()
        with patch.object(orchestrator, "_chat", side_effect=OSError("測試不連模型")), \
                patch.object(agent, "_chat", side_effect=OSError("測試不連模型")):
            game_id = client.post("/api/gomoku/games/", json.dumps({"seat": 2}),
                                  content_type="application/json").json()["game"]["id"]
        response = client.post("/accounts/upgrade/", {"username": "dora", "name": "朵拉", "password1": "a-Long-pass-88",
                                                      "password2": "a-Long-pass-88"})
        self.assertRedirects(response, "/", fetch_redirect_response=False)
        dora = User.objects.get(pk=guest.pk)
        self.assertEqual(dora.username, "dora")
        self.assertTrue(dora.check_password("a-Long-pass-88"))
        self.assertEqual(Game.objects.get(pk=game_id).owner, dora)
        self.assertEqual(int(client.session["_auth_user_id"]), dora.pk)  # 改密碼後仍然登入
        self.assertTrue(Client().login(username="dora", password="a-Long-pass-88"))  # 別台電腦也能登入了

    def test_only_guests_can_use_the_upgrade_page(self):
        # 正式帳號不能從這裡改名、改密碼（沒有驗證舊密碼）
        member = User.objects.create_user("erin", password="pw-for-tests-3")
        client = Client()
        client.force_login(member)
        client.post("/accounts/upgrade/", {"username": "mallory", "name": "馬洛里", "password1": "a-Long-pass-99",
                                           "password2": "a-Long-pass-99"})
        member.refresh_from_db()
        self.assertEqual(member.username, "erin")
        self.assertTrue(member.check_password("pw-for-tests-3"))

    def test_the_upgrade_form_starts_with_an_empty_account(self):
        # 帳號欄是空的，讓遊客自己取一個登入用的帳號（使用者名稱欄另外先填好，見 AccountNameTests）
        client, _, guest = self.enter_as_guest()
        page = client.get("/accounts/upgrade/").content.decode()
        self.assertNotIn(guest.username, page.split('name="username"')[1].split(">")[0])

    def test_opening_the_guest_address_leads_to_the_login_page_without_creating_an_account(self):
        # 聊天軟體產生連結預覽時也會打開網址：打開網址就建立帳號的話，每傳一次連結就多一個沒人用的遊客。
        # 但也不能只回看不懂的 405，要帶到登入頁按按鈕，邀請連結的去處也要保留（2026-11-09）
        response = Client().get("/accounts/guest/", {"next": "/join/ABCDEF/"})
        self.assertRedirects(response, "/accounts/login/?next=/join/ABCDEF/", fetch_redirect_response=False)
        self.assertRedirects(Client().get("/accounts/guest/"), "/accounts/login/", fetch_redirect_response=False)
        self.assertFalse(User.objects.exists())


def sign_up(client, username, name, password="a-Long-pass-55"):
    return client.post("/accounts/signup/", {"username": username, "name": name, "password1": password,
                                             "password2": password})


class AccountNameTests(TestCase):
    """帳號的三個欄位（使用者要求，2026-11-20：「帳號類別要有三個 一個是使用者名稱 一個是帳號 一個是密碼」）。
    帳號只用來登入，使用者名稱是別人在畫面上看到的；分開之後，房間裡的對手、觀戰的人、房間列表都只看到名稱，
    猜不到你登入用的帳號。使用者決定：名稱不能和別人重複（房間的換座位、請人離開、轉讓房主用名稱找人，也不能冒充別人）、
    只用帳號登入、舊帳號的名稱沿用帳號、遊客的帳號和名稱都是「遊客-XXXXXX」。"""

    def setUp(self):
        for patcher in (patch.object(orchestrator, "_chat", side_effect=OSError("測試不連模型")),
                        patch.object(agent, "_chat", side_effect=OSError("測試不連模型"))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_signing_up_keeps_the_account_and_the_name_apart(self):
        client = Client()
        self.assertRedirects(sign_up(client, "carol01", "小卡"), "/", fetch_redirect_response=False)
        carol = User.objects.get(username="carol01")
        self.assertEqual(carol.profile.name, "小卡")
        self.assertContains(client.get("/"), '<span class="account-name">小卡</span>')
        # 只用帳號登入：名稱是公開的，拿名稱也能登入的話，登入用的 ID 就等於公開了
        self.assertTrue(Client().login(username="carol01", password="a-Long-pass-55"))
        self.assertFalse(Client().login(username="小卡", password="a-Long-pass-55"))

    def test_the_signup_form_asks_for_all_three_and_calls_the_login_id_an_account(self):
        # Django 的 username 欄中文叫「使用者名稱」，不改的話畫面上會有兩個「使用者名稱」，玩家分不清哪個用來登入
        page = Client().get("/accounts/signup/").content.decode()
        for field in ('name="username"', 'name="name"', 'name="password1"', 'name="password2"'):
            self.assertIn(field, page)
        self.assertIn("帳號", page.split('name="username"')[0].rsplit("<label", 1)[1])
        self.assertNotIn("使用者名稱", page.split('name="username"')[0].rsplit("<label", 1)[1])
        self.assertIn("使用者名稱", page.split('name="name"')[0].rsplit("<label", 1)[1])

    def test_a_name_someone_else_has_is_refused(self):
        sign_up(Client(), "carol01", "小卡")
        for name in ("小卡", " 小卡 "):  # 前後空白不算不同的名稱
            self.assertContains(sign_up(Client(), "mallory", name), "這個使用者名稱已經有人用了")
        self.assertRedirects(sign_up(Client(), "mallory", "Bob"), "/", fetch_redirect_response=False)
        self.assertContains(sign_up(Client(), "eve", "bob"), "這個使用者名稱已經有人用了")  # 不分大小寫，否則看起來一樣
        self.assertContains(sign_up(Client(), "CAROL01", "卡卡"), "這個帳號已經有人用了")
        self.assertEqual(set(User.objects.values_list("username", flat=True)), {"carol01", "mallory"})

    def test_a_name_taken_at_the_same_moment_saves_nothing(self):
        # 兩個人同時用同一個名稱註冊：表單檢查都過了，資料庫的唯一索引擋下後到的那個；不能留下一個名稱不對的帳號
        sign_up(Client(), "carol01", "小卡")
        from . import accounts
        with patch.object(accounts.AccountForm, "clean_name", lambda form: form.cleaned_data["name"]):
            response = sign_up(Client(), "mallory", "小卡")
        self.assertContains(response, "這個使用者名稱已經有人用了")
        self.assertFalse(User.objects.filter(username="mallory").exists())

    def test_other_players_see_names_never_login_accounts(self):
        alice = User.objects.create_user("login-alice", password="x")
        bob = User.objects.create_user("login-bob", password="x")
        carol = User.objects.create_user("login-carol", password="x")
        for user, name in ((alice, "愛麗絲"), (bob, "鮑伯"), (carol, "卡蘿")):
            Profile.objects.filter(user=user).update(name=name)
        game, _ = operations.start_game(GOMOKU, 1, {}, alice, "pvp")
        operations.join_game(game.room_code, bob)
        operations.watch_game(game.room_code, carol)
        Presence.objects.create(session_key="alice-online", user=alice)
        clients = {}
        for user in (alice, bob, carol):
            clients[user] = Client()
            clients[user].force_login(user)
        url = f"/api/gomoku/games/{game.id}"
        clients[alice].post(f"{url}/chat/", json.dumps({"text": "你好"}), content_type="application/json")
        seen = clients[bob].get(f"{url}/")
        detail = seen.json()["game"]
        self.assertEqual((detail["players"], detail["owner"], detail["spectators"]),
                         ({"1": "愛麗絲", "2": "鮑伯"}, "愛麗絲", ["卡蘿"]))
        self.assertEqual([m["name"] for m in seen.json()["chat"]], ["愛麗絲"])
        self.assertNotIn("login-alice", seen.content.decode())
        rooms = clients[bob].get("/join/").content.decode()  # 房間列表的房主
        self.assertIn("愛麗絲", rooms)
        self.assertNotIn("login-alice", rooms)
        # 觀戰的人的頁面用名稱標出「你」（app.js 的 ME 和玩家列表的名稱比對）
        page = clients[carol].get(f"/gomoku/?watch={game.id}").content.decode()
        self.assertIn(f'<script id="viewer-name" type="application/json">{json.dumps("卡蘿")}</script>', page)

    def test_the_host_runs_the_room_by_name(self):
        # 網頁只知道別人的名稱：請人離開、轉讓房主都用名稱找人，用帳號反而找不到（名稱唯一，所以不會找錯人）
        alice = User.objects.create_user("login-alice", password="x")
        bob = User.objects.create_user("login-bob", password="x")
        Profile.objects.filter(user=bob).update(name="鮑伯")
        game, _ = operations.start_game(GOMOKU, 1, {}, alice, "pvp")
        operations.join_game(game.room_code, bob)
        host = Client()
        host.force_login(alice)

        def kick(who):
            return host.post(f"/api/gomoku/games/{game.id}/kick/", json.dumps({"player": who}),
                             content_type="application/json")

        self.assertEqual(kick("login-bob").status_code, 400)
        self.assertEqual(kick("鮑伯").status_code, 200)
        self.assertIsNone(game.players.get(seat=2).user)

    def test_old_accounts_keep_their_account_as_their_name(self):
        # migration 0013：舊帳號沿用帳號當名稱（使用者決定），大家看到的名字不變
        migration = importlib.import_module("arena.migrations.0013_profile")
        old = User.objects.create_user("old-timer", password="x")
        Profile.objects.all().delete()
        migration.names_from_accounts(apps, SimpleNamespace(connection=connection))
        self.assertEqual(Profile.objects.get(user=old).name, "old-timer")

    def test_accounts_made_outside_the_signup_page_still_get_a_free_name(self):
        # createsuperuser、後台建立的帳號也要有名稱；帳號和別人的名稱一樣時加編號，不能讓兩個人同名
        sign_up(Client(), "carol01", "boss")
        admin = User.objects.create_superuser("boss")
        self.assertEqual(admin.profile.name, "boss-2")

    def test_a_guest_is_named_like_its_account_and_may_keep_that_name(self):
        client = Client()
        client.post("/accounts/guest/")
        guest = User.objects.get(pk=client.session["_auth_user_id"])
        guest_name = guest.username
        self.assertTrue(guest_name.startswith("遊客-"))
        self.assertEqual(guest.profile.name, guest_name)
        # 升級頁：帳號欄空白，使用者名稱先填好遊客名稱；不改名稱直接送出也行（不算和自己重複）
        page = client.get("/accounts/upgrade/").content.decode()
        self.assertIn(f'value="{guest_name}"', page.split('name="name"')[1].split(">")[0])
        response = client.post("/accounts/upgrade/", {"username": "dora", "name": guest_name,
                                                      "password1": "a-Long-pass-88", "password2": "a-Long-pass-88"})
        self.assertRedirects(response, "/", fetch_redirect_response=False)
        guest = User.objects.get(pk=guest.pk)
        self.assertEqual((guest.username, guest.profile.name), ("dora", guest_name))
        self.assertTrue(guest.has_usable_password())

    def test_a_guest_cannot_upgrade_to_someone_elses_name_and_stays_a_guest(self):
        sign_up(Client(), "carol01", "小卡")
        client = Client()
        client.post("/accounts/guest/")
        guest = User.objects.get(pk=client.session["_auth_user_id"])
        response = client.post("/accounts/upgrade/", {"username": "dora", "name": "小卡",
                                                      "password1": "a-Long-pass-88", "password2": "a-Long-pass-88"})
        self.assertContains(response, "這個使用者名稱已經有人用了")
        self.assertContains(response, "設定帳號密碼")  # 頂端仍是遊客的選項：沒有升級成功
        guest.refresh_from_db()
        self.assertFalse(guest.has_usable_password())
        self.assertTrue(guest.username.startswith("遊客-"))

TUNNEL = {"HTTP_HOST": "abc-def.trycloudflare.com", "HTTP_CF_CONNECTING_IP": "198.51.100.7"}  # 經過通道：從本機的 cloudflared 連進來


@override_settings(PUBLIC=True, ALLOWED_HOSTS=[".trycloudflare.com", "localhost", "127.0.0.1"],
                   SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"))
class PublicSiteTests(TestCase):
    """公開模式的帳號安全（2026-11-09 檢查）：網址傳出去以後，別人不能因為網址而冒用帳號或碰到後台。
    - 通道網址用 http 也打得開的話，登入的 cookie 沒加密，在公共 Wi-Fi 上可能被截取；遊客沒有密碼，cookie 就是整個帳號。
    - 後台登入沒有猜密碼的次數限制，所以不經過通道開放，只能在這台電腦打開。"""

    def test_http_through_the_tunnel_moves_to_https_before_any_cookie_is_set(self):
        response = Client().get("/join/ABCDEF/?x=1", **TUNNEL, HTTP_X_FORWARDED_PROTO="http")
        self.assertEqual(response.status_code, 301)
        self.assertEqual(response["Location"], "https://abc-def.trycloudflare.com/join/ABCDEF/?x=1")  # 邀請連結照樣能用
        self.assertEqual(dict(response.cookies), {})
        # https 照常：沒登入就到登入頁
        https = Client().get("/", **TUNNEL, HTTP_X_FORWARDED_PROTO="https")
        self.assertRedirects(https, "/accounts/login/?next=/", fetch_redirect_response=False)

    def test_the_admin_site_does_not_exist_through_the_tunnel(self):
        boss = Client()
        boss.force_login(User.objects.create_superuser("boss"))
        self.assertEqual(boss.get("/admin/", **TUNNEL, HTTP_X_FORWARDED_PROTO="https").status_code, 404)
        self.assertEqual(Client().get("/admin/login/", **TUNNEL, HTTP_X_FORWARDED_PROTO="https").status_code, 404)
        # 網址寫 localhost 但其實經過 Cloudflare（帶 CF-Connecting-IP）也一樣擋
        self.assertEqual(boss.get("/admin/", HTTP_HOST="localhost", HTTP_CF_CONNECTING_IP="198.51.100.7").status_code, 404)
        # 在這台電腦上打開照常
        self.assertEqual(boss.get("/admin/", HTTP_HOST="localhost:8010").status_code, 200)
        self.assertEqual(boss.get("/admin/arena/loginrecord/", HTTP_HOST="127.0.0.1:8010").status_code, 200)

    @override_settings(PUBLIC=False)
    def test_nothing_changes_outside_public_mode(self):
        boss = Client()
        boss.force_login(User.objects.create_superuser("boss"))
        self.assertEqual(boss.get("/admin/", HTTP_HOST="localhost").status_code, 200)

    def test_public_mode_marks_login_cookies_https_only(self):
        # 設定在 config/settings.py 載入時依 GAME_PUBLIC 決定，所以另開一個 Python 載入設定來看
        import os
        import subprocess
        import sys
        code = ("import django; django.setup(); from django.conf import settings as s; "
                "print(s.SESSION_COOKIE_SECURE, s.CSRF_COOKIE_SECURE, s.DEBUG)")
        env = dict(os.environ, GAME_PUBLIC="1", DJANGO_SETTINGS_MODULE="config.settings")
        out = subprocess.run([sys.executable, "-c", code], cwd=settings.BASE_DIR, env=env,
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.stdout.split()[-3:], ["True", "True", "False"], out.stderr)


class ExpiredFormTests(TestCase):
    """表單驗證碼不符（2026-11-09，手機上看到「CSRF 驗證失敗」）：登入會換新的驗證碼，網站慢時再按一次或按上一頁再送，
    送的就是舊驗證碼。這仍然要擋（防止別的網站替使用者送表單），但要給看得懂的說明和回去重新整理的連結。"""

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)

    def test_a_second_press_after_logging_in_gets_a_plain_explanation(self):
        login_page = self.client.get("/accounts/login/")
        old_token = str(login_page.context["csrf_token"])
        self.client.post("/accounts/guest/", {"csrfmiddlewaretoken": old_token})
        self.assertEqual(User.objects.count(), 1)
        with self.assertLogs("django.security.csrf", "WARNING") as logs:  # 原因寫進網站紀錄，才查得到
            again = self.client.post("/accounts/guest/", {"csrfmiddlewaretoken": old_token},
                                     HTTP_REFERER="http://testserver/accounts/login/")
        self.assertEqual(again.status_code, 403)
        self.assertEqual(User.objects.count(), 1)  # 照樣擋下：沒有多建一個帳號
        self.assertIn("CSRF token", logs.output[0])
        page = again.content.decode()
        self.assertIn("這個頁面已經過期", page)
        self.assertNotIn("CSRF", page)
        self.assertIn('href="http://testserver/accounts/login/"', page)  # 回到送出表單的那頁重新拿驗證碼

    def test_the_retry_link_never_points_to_another_site(self):
        with self.assertLogs("django.security.csrf", "WARNING"):
            page = self.client.post("/accounts/guest/", HTTP_REFERER="https://evil.example.com/").content.decode()
        self.assertNotIn("evil.example.com", page)
        self.assertIn("回到首頁", page)


class RoomSettingsTests(TestCase):
    """建立對戰房間（使用者要求，2026-11-08）：首頁一鍵開房，房主可以自由改設定。
    改的是同一個房間（房間密碼不變，已經傳出去的邀請照樣能用）。原本朋友加入後就鎖住，
    2026-11-15 使用者要求「房主可以自由選擇」：按「開始」之前都能改（朋友照樣坐著），開始後才不能改。"""

    def setUp(self):
        self.alice = User.objects.create_user("alice", password="x")
        self.bob = User.objects.create_user("bob", password="x")
        self.clients = {}
        for user in (self.alice, self.bob):
            self.clients[user.username] = Client()
            self.clients[user.username].force_login(user)
        patcher = patch.object(orchestrator, "_chat", side_effect=OSError("測試不連模型"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def post(self, who, url, body):
        return self.clients[who].post(url, json.dumps(body), content_type="application/json")

    def open_room(self, game="weiqi", **body):
        r = self.post("alice", f"/api/{game}/games/", {"seat": 1, "mode": "pvp", **body}).json()
        return Game.objects.get(pk=r["game"]["id"])

    def test_the_home_page_creates_a_room_in_one_click_and_the_host_picks_the_game_inside(self):
        # 2026-11-16 使用者要求「建立對戰改成按下後直接建立對戰」：不先跳出選遊戲的選單，
        # 一按就用第一個遊戲開房；要玩別的遊戲，房主在房間裡（按開始前）換
        page = self.clients["alice"].get("/").content.decode()
        first = next(iter(games.GAMES))
        self.assertIn(f'href="/{first}/?room=new">建立對戰房間</a>', page)
        self.assertEqual(page.count("?room=new"), 1)
        game = self.open_room(game=first)
        r = self.post("alice", f"/api/{first}/games/{game.id}/room/",
                      {"seat": 1, "game_type": "weiqi", "settings": {"size": 9}})
        self.assertEqual(r.status_code, 200)
        game.refresh_from_db()
        self.assertEqual(game.game_type, "weiqi")

    def test_the_host_changes_settings_in_the_same_room_before_anyone_joins(self):
        game = self.open_room(settings={"size": 9})
        code = game.room_code
        r = self.post("alice", f"/api/weiqi/games/{game.id}/room/", {"seat": 2, "settings": {"size": 13}})
        self.assertEqual(r.status_code, 200)
        game.refresh_from_db()
        self.assertEqual((game.settings["size"], game.human_seat, game.room_code), (13, 2, code))
        self.assertEqual(r.json()["frame"]["size"], 13)
        # 朋友用原本的密碼加入，拿到的是新的設定，坐進房主讓出來的座位
        self.clients["bob"].post("/join/", {"code": code})
        game.refresh_from_db()
        self.assertEqual((game.seat_of(self.alice), game.seat_of(self.bob)), (2, 1))

    def test_the_host_can_still_change_settings_after_a_friend_sits_down(self):
        # 原本朋友加入後就鎖住設定；2026-11-15 使用者決定房主在開始前都能自由改，朋友照樣坐著
        game = self.open_room(settings={"size": 9})
        self.clients["bob"].post("/join/", {"code": game.room_code})
        r = self.post("alice", f"/api/weiqi/games/{game.id}/room/", {"seat": 1, "settings": {"size": 19}})
        self.assertEqual(r.status_code, 200)
        game.refresh_from_db()
        self.assertEqual(game.settings["size"], 19)
        self.assertEqual(game.seat_of(self.bob), 2)  # 朋友的座位還在
        # 按了開始就不能再改
        start_now(game)
        r = self.post("alice", f"/api/weiqi/games/{game.id}/room/", {"seat": 1, "settings": {"size": 9}})
        self.assertEqual(r.status_code, 400)
        game.refresh_from_db()
        self.assertEqual(game.settings["size"], 19)

    def test_only_the_host_can_change_the_room(self):
        game = self.open_room()
        # 還沒加入的人看不到這盤；直接呼叫也會被擋
        self.assertEqual(self.post("bob", f"/api/weiqi/games/{game.id}/room/", {"seat": 1, "settings": {}}).status_code, 404)
        with self.assertRaises(ValueError):
            operations.change_room(WEIQI, game, self.bob, 1, {"size": 9})

    def test_poker_host_can_change_the_table_size_and_who_still_fits_stays_seated(self):
        # 撲克開放牌桌（2026-11-16）：改人數時，座位號碼還在新人數內的人（真人、AI）照樣坐著，其他人改成觀戰
        from holdem.module import MODULE as HOLDEM
        game = self.open_room("holdem", settings={"players": 4})
        old_seed = game.secret["seed"]
        with patch("arena.operations.secrets.choice", side_effect=lambda seats: seats[-1]):
            self.post("alice", f"/api/holdem/games/{game.id}/table/", {"op": "sit"})  # 4 號位
            self.post("alice", f"/api/holdem/games/{game.id}/table/", {"op": "add_ai", "kind": "agent"})  # 3 號位
        r = self.post("alice", f"/api/holdem/games/{game.id}/room/", {"seat": 1, "settings": {"players": 3}})
        self.assertEqual(r.status_code, 200)
        game.refresh_from_db()
        self.assertEqual(game.settings["players"], 3)
        self.assertEqual(HOLDEM.replay(operations.settings_of(game), game.actions).occupant, {3: "agent"})
        self.assertIn("alice", game.summary()["spectators"])
        self.assertNotEqual(game.secret["seed"], old_seed)  # 新的牌局，洗牌種子也換掉
        self.assertNotIn("seed", json.dumps(r.json()))  # 洗牌種子不能送到網頁


class SpectatorTests(TestCase):
    """觀戰（使用者要求，2026-11-08）：有房間密碼或邀請連結的人才看得到；觀戰的人沒有座位，
    只看所有人都看得到的資訊（撲克的底牌要攤牌才看得到），不能下、不能用 AI 教練，觀戰的對局也不算進自己的紀錄。"""

    def setUp(self):
        self.alice = User.objects.create_user("alice", password="x")
        self.bob = User.objects.create_user("bob", password="x")
        self.carol = User.objects.create_user("carol", password="x")
        self.clients = {}
        for user in (self.alice, self.bob, self.carol):
            self.clients[user.username] = Client()
            self.clients[user.username].force_login(user)
        patcher = patch.object(orchestrator, "_chat", side_effect=OSError("測試不連模型"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.game, _ = operations.start_game(GOMOKU, 1, {}, self.alice, "pvp")
        operations.join_game(self.game.room_code, self.bob)
        start_now(self.game)

    def watch(self, who="carol", code=None):
        return self.clients[who].post("/join/", {"code": code or self.game.room_code, "action": "watch"})

    def post(self, who, url, body):
        return self.clients[who].post(url, json.dumps(body), content_type="application/json")

    def test_watching_needs_the_room_code(self):
        self.assertEqual(self.clients["carol"].get(f"/api/gomoku/games/{self.game.id}/").status_code, 404)
        self.assertRedirects(self.watch(), f"/gomoku/?watch={self.game.id}", fetch_redirect_response=False)
        detail = self.clients["carol"].get(f"/api/gomoku/games/{self.game.id}/").json()
        self.assertIsNone(detail["my_seat"])
        self.assertContains(self.watch(code="ZZZZZZ"), "找不到這個房間編號")

    def test_a_full_room_can_still_be_watched_and_players_keep_their_seat(self):
        self.assertRedirects(self.clients["carol"].post("/join/", {"code": self.game.room_code}),
                             f"/gomoku/?watch={self.game.id}&full=1", fetch_redirect_response=False)
        self.assertRedirects(self.watch(), f"/gomoku/?watch={self.game.id}", fetch_redirect_response=False)
        # 坐在這盤的人按「觀戰」照樣回到自己的座位，不會變成觀戰者
        self.assertRedirects(self.watch("bob"), f"/gomoku/?resume={self.game.id}", fetch_redirect_response=False)
        self.assertFalse(self.game.spectators.filter(user=self.bob).exists())

    def test_spectators_cannot_move_or_use_the_ai_coach(self):
        self.watch()
        url = f"/api/gomoku/games/{self.game.id}"
        self.assertEqual(self.post("carol", f"{url}/move/", {"action": "H8"}).status_code, 404)
        self.post("alice", f"{url}/move/", {"action": "H8"})
        self.post("bob", f"{url}/move/", {"action": "A1"})
        self.post("alice", f"{url}/move/", {"action": "resign"})
        # 結束後也不能復盤、提問：分析是從某一方的座位寫的（「你贏了」、撲克要用那個座位的底牌）
        self.assertEqual(self.clients["carol"].get(f"{url}/review/").status_code, 404)
        self.assertEqual(self.post("carol", f"{url}/ask/", {"question": "誰比較好？", "at": 1}).status_code, 404)
        self.assertEqual(self.post("carol", f"{url}/end/", {}).status_code, 404)
        self.game.refresh_from_db()
        self.assertEqual(self.game.actions, ["H8", "A1", "resign"])

    def test_watched_games_do_not_count_as_your_own(self):
        self.watch()
        self.post("alice", f"/api/gomoku/games/{self.game.id}/move/", {"action": "resign"})
        self.assertNotIn(self.game, Game.visible_to(self.carol))
        home = operations.home_context(self.carol)
        self.assertFalse(home["recent"] or home["resume"])

    def test_the_room_lists_players_and_spectators_and_tells_everyone_when_someone_comes_to_watch(self):
        # 使用者要求（2026-11-13）：房間內要有玩家列表。觀戰的人也列出來，進來時房間裡的人馬上看到
        from . import realtime
        detail = self.clients["alice"].get(f"/api/gomoku/games/{self.game.id}/").json()["game"]
        self.assertEqual((detail["players"], detail["spectators"], detail["human_seat"]),
                         ({"1": "alice", "2": "bob"}, [], 1))
        with patch.object(realtime, "notify") as notify:
            self.watch()
        notify.assert_called_once()
        detail = self.clients["bob"].get(f"/api/gomoku/games/{self.game.id}/").json()["game"]
        self.assertEqual(detail["spectators"], ["carol"])

    def test_spectators_get_live_updates_from_no_seat(self):
        from . import realtime
        self.assertEqual(realtime.seat_for(self.carol, self.game.id), (None, None))
        self.watch()
        game, seat = realtime.seat_for(self.carol, self.game.id)
        self.assertEqual((game.id, seat), (self.game.id, None))
        self.assertEqual(realtime.seat_for(self.alice, self.game.id)[1], 1)

    def test_poker_spectators_never_see_hole_cards_before_showdown(self):
        from holdem.module import MODULE as HOLDEM
        game = poker_table(self.alice, [self.bob], ai=("agent", "bot"))
        game = start_now(game)
        with patch.object(agent, "_chat", side_effect=OSError("測試不連模型")):
            operations.table_advance(HOLDEM, game, 0, settings.OLLAMA_MODEL, HOLDEM.default_steps, None)
        operations.watch_game(game.room_code, self.carol)
        game.refresh_from_db()
        detail = self.clients["carol"].get(f"/api/holdem/games/{game.id}/").json()
        state = HOLDEM.replay(operations.settings_of(game), game.actions)
        self.assertFalse(state.hand_over)  # 牌局進行中
        for frame in detail["frames"]:
            for s in frame["seats"]:
                self.assertTrue(all(c is None for c in s["cards"]), s)
                self.assertEqual(s["hand_name"], "")
        self.assertIsNone(detail["frames"][-1]["legal"])
        self.assertTrue(all(n.get("hidden") for n in detail["notes"]))
        self.assertNotIn("seed", json.dumps(detail))

class RefusalAssertions:
    def assertRefused(self, response, reason):
        """API 拒絕時回 400 與 {"error": 原因}；JSON 會把中文跳脫，所以讀出來再比對。"""
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn(reason, response.json()["error"])


class RoomStartTests(RefusalAssertions, TestCase):
    """房主按「開始」（使用者要求，2026-11-13）：朋友坐滿也不會自動開始，由房主按開始，倒數三秒後才能下。
    倒數要由伺服器守住，不只是畫面：倒數中送來的一步、AI 的座位都要等倒數結束，否則先按的人就先下了。"""

    def setUp(self):
        self.alice = User.objects.create_user("alice", password="x")
        self.bob = User.objects.create_user("bob", password="x")
        self.clients = {}
        for user in (self.alice, self.bob):
            self.clients[user.username] = Client()
            self.clients[user.username].force_login(user)
        for patcher in (patch.object(orchestrator, "_chat", side_effect=OSError("測試不連模型")),
                        patch.object(agent, "_chat", side_effect=OSError("測試不連模型"))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def post(self, who, url, body=None):
        return self.clients[who].post(url, json.dumps(body or {}), content_type="application/json")

    def full_room(self, module=GOMOKU, settings=None):
        game, _ = operations.start_game(module, 1, settings or {}, self.alice, "pvp")
        operations.join_game(game.room_code, self.bob)
        game.refresh_from_db()
        return game

    def test_a_full_room_waits_for_the_host_instead_of_starting(self):
        game = self.full_room()
        self.assertIsNone(game.started_at)
        self.assertIsNone(game.summary()["starts_in"])
        r = self.post("alice", f"/api/gomoku/games/{game.id}/move/", {"action": "H8"})
        self.assertRefused(r, "房主還沒按「開始」")

    def test_only_the_host_can_start_and_only_when_every_seat_is_taken(self):
        game, _ = operations.start_game(GOMOKU, 1, {}, self.alice, "pvp")
        self.assertRefused(self.post("alice", f"/api/gomoku/games/{game.id}/start/"), "座位空著")
        operations.join_game(game.room_code, self.bob)
        self.assertRefused(self.post("bob", f"/api/gomoku/games/{game.id}/start/"), "只有開房的人")
        game.refresh_from_db()
        self.assertIsNone(game.started_at)

    def test_moves_sent_during_the_countdown_are_refused(self):
        game = self.full_room()
        with patch.object(operations.time, "sleep") as sleep:
            self.assertEqual(self.post("alice", f"/api/gomoku/games/{game.id}/start/").status_code, 200)
        self.assertAlmostEqual(sleep.call_args.args[0], operations.START_COUNTDOWN, delta=0.5)
        game.refresh_from_db()
        r = self.post("alice", f"/api/gomoku/games/{game.id}/move/", {"action": "H8"})
        self.assertRefused(r, "倒數還沒結束")
        # 倒數結束後就能下
        Game.objects.filter(pk=game.pk).update(started_at=game.started_at - timedelta(seconds=10))
        self.assertEqual(self.post("alice", f"/api/gomoku/games/{game.id}/move/", {"action": "H8"}).status_code, 200)

    def test_pressing_start_twice_does_not_push_the_countdown_back(self):
        game = self.full_room()
        with patch.object(operations.time, "sleep"):
            self.post("alice", f"/api/gomoku/games/{game.id}/start/")
            first = Game.objects.get(pk=game.pk).started_at
            self.assertEqual(self.post("alice", f"/api/gomoku/games/{game.id}/start/").status_code, 400)
        self.assertEqual(Game.objects.get(pk=game.pk).started_at, first)

    def test_everyone_sees_the_countdown_before_any_ai_seat_acts(self):
        # 撲克有 AI 座位可能先行動：要先通知大家開始倒數，倒數完 AI 才行動，不能趕在倒數前下
        from holdem.module import MODULE as HOLDEM
        from . import realtime
        game = poker_table(self.alice, [self.bob], ai=("agent",))
        events = []
        real_ai = operations.run_ai_turns

        def countdown_passes(seconds):
            events.append(("sleep", seconds))
            # sleep 被換掉了，時間沒有真的過去：把開始時間改成現在，讓 AI 在「倒數結束」後行動
            Game.objects.filter(pk=game.pk).update(started_at=django_timezone.now())

        def ai_turns(module, g, *args):
            events.append(("ai",))
            g.refresh_from_db()
            return real_ai(module, g, *args)

        with patch.object(realtime, "notify", side_effect=lambda g: events.append(("notify", g.summary()["starts_in"]))), \
                patch.object(operations.time, "sleep", side_effect=countdown_passes), \
                patch.object(operations, "run_ai_turns", side_effect=ai_turns):
            self.assertEqual(self.post("alice", f"/api/holdem/games/{game.id}/start/").status_code, 200)
        self.assertEqual([e[0] for e in events][:3], ["notify", "sleep", "ai"])
        self.assertGreater(events[0][1], 0)  # 通知時還在倒數
        self.assertEqual(ModelRun.objects.filter(kind="req_start").count(), 1)  # 經過 AI 助理

    def test_the_ai_seats_act_as_soon_as_the_countdown_ends(self):
        # 使用者回報（2026-11-15）：撲克開房間、按開始後輪到 AI 的座位卻一直不行動，牌局卡住（第 140 盤）。
        # 原因：倒數的等待照四捨五入到小數兩位的秒數睡，早醒幾毫秒時 run_ai_turns 以為還沒開始就不行動。
        # 用假時鐘重現：按開始到開始等待之間經過 0.006 秒，剩 2.994 秒，四捨五入成 2.99 就會早醒
        from holdem.module import MODULE as HOLDEM
        from . import realtime
        # 找一個發第一手後由 AI 先說話的洗牌種子（座位照空位順序：alice 1、bob 2、AI 3、4 號位）
        tokens = [HOLDEM.member_token(self.alice), HOLDEM.member_token(self.bob)]
        def first_actor(seed):
            t = HOLDEM.new_state({**HOLDEM.clean_settings({"players": 4, "table": True}), "secret": {"seed": seed}})
            for a in (f"sit 1 {tokens[0]}", f"sit 2 {tokens[1]}", "sit 3 agent", "sit 4 tag", "deal"):
                t = HOLDEM.apply(t, a)
            return t.to_act
        seed = next(n for n in range(50) if first_actor(n) in (3, 4))
        game = poker_table(self.alice, [self.bob], ai=("agent", "bot"), seed=seed)
        clock = [django_timezone.now()]

        def tick(seconds):
            clock[0] += timedelta(seconds=seconds)

        with patch("django.utils.timezone.now", side_effect=lambda: clock[0]), \
                patch.object(realtime, "notify", side_effect=lambda g: tick(0.006)), \
                patch.object(operations.time, "sleep", side_effect=tick):
            self.assertEqual(self.post("alice", f"/api/holdem/games/{game.id}/start/").status_code, 200)
        game.refresh_from_db()
        state = HOLDEM.replay(operations.settings_of(game), game.actions)
        self.assertGreater(len(game.actions), game.actions.index("deal") + 1, "倒數結束後輪到 AI 的座位要馬上行動")
        self.assertIn(HOLDEM.to_move(state), game.human_seats())  # 行動到輪到真人為止，不會卡在 AI

    def test_the_ai_seats_never_act_before_the_start(self):
        from holdem.module import MODULE as HOLDEM
        game = self.full_room(HOLDEM, {"players": 4, "humans": [1, 2]})
        self.assertEqual(operations.run_ai_turns(HOLDEM, game, settings.OLLAMA_MODEL, 2, 1), [])
        self.assertEqual(Game.objects.get(pk=game.pk).actions, [])

    def test_the_host_and_the_friend_can_get_back_to_a_room_from_the_home_page(self):
        # 使用者回報（2026-11-13）：回到首頁後回不到自己開的房間。按開始前一步都沒下，原本的清單只列下過的對局
        game, _ = operations.start_game(GOMOKU, 1, {}, self.alice, "pvp")
        home = self.clients["alice"].get("/").content.decode()
        self.assertIn(f"/gomoku/?resume={game.id}", home)
        self.assertIn(f"等朋友加入（房間編號 {game.room_code}）", home)
        self.assertIn("回到房間", home)
        operations.join_game(game.room_code, self.bob)
        home = self.clients["bob"].get("/").content.decode()
        self.assertIn(f"/gomoku/?resume={game.id}", home)
        self.assertIn("等房主按「開始」", home)
        history = self.clients["alice"].get("/history/?result=unfinished").content.decode()
        self.assertIn(f"/gomoku/?resume={game.id}", history)
        # 開始以後就是一般的對局列：繼續下、手數
        game = start_now(game)
        operations.play_human_move(GOMOKU, game, "H8", 1)
        card = operations.home_context(self.alice)["resume"][0]
        self.assertEqual((card["id"], card["resume_label"], card["room"]), (game.id, "繼續下", ""))

    def test_every_page_links_back_to_an_unfinished_room(self):
        # 使用者要求（2026-11-13）：「在其他頁面也要確保能夠回去」，不只首頁
        game, _ = operations.start_game(GOMOKU, 1, {}, self.alice, "pvp")
        back = f'href="/gomoku/?resume={game.id}"'
        for url in ("/", "/history/", "/guide/", "/join/", "/weiqi/", "/gomoku/"):
            page = self.clients["alice"].get(url).content.decode()
            self.assertIn(back, page, url)
            self.assertNotIn("{#", page, url)  # 多行的模板註解要用 {% comment %}，否則原文會出現在畫面上
        carol = User.objects.create_user("carol")
        operations.watch_game(game.room_code, carol)
        watcher = Client()
        watcher.force_login(carol)
        self.assertNotIn(back, watcher.get("/guide/").content.decode())  # 觀戰的房間不算自己的
        operations.join_game(game.room_code, self.bob)
        game = start_now(game)
        operations.play_human_move(GOMOKU, game, "resign", 1)
        self.assertNotIn(back, self.clients["alice"].get("/guide/").content.decode())  # 結束了就不再提示

    def test_the_host_can_close_a_room_before_it_starts_and_it_disappears_everywhere(self):
        # 使用者要求（2026-11-13）：「房主要能夠關閉房間」。沒人來的房間否則會一直留在清單和頂端的「回到房間」
        from . import realtime
        game, _ = operations.start_game(GOMOKU, 1, {}, self.alice, "pvp")
        operations.join_game(game.room_code, self.bob)
        self.assertRefused(self.post("bob", f"/api/gomoku/games/{game.id}/close/"), "只有開房的人")
        with patch.object(realtime, "notify") as notify:
            r = self.post("alice", f"/api/gomoku/games/{game.id}/close/")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["game"]["closed"])
        notify.assert_called_once()  # 房間裡的朋友、觀戰的人馬上看到房間關了
        game.refresh_from_db()
        self.assertTrue(game.ended)
        back = f"/gomoku/?resume={game.id}"
        for who in ("alice", "bob"):
            for url in ("/", "/history/", "/guide/"):
                self.assertNotIn(back, self.clients[who].get(url).content.decode(), (who, url))
        carol = User.objects.create_user("carol")
        for join in (operations.join_game, operations.watch_game):
            with self.assertRaisesMessage(ValueError, "房主已經關閉這個房間"):
                join(game.room_code, carol)
        self.assertRefused(self.post("alice", f"/api/gomoku/games/{game.id}/start/"), "已經結束")

    def test_a_started_room_cannot_be_closed_so_the_losing_side_cannot_escape(self):
        # 開始後還能關的話，快輸的一方關掉房間就能讓輸棋不算；要結束請認輸
        game = start_now(self.full_room())
        self.assertRefused(self.post("alice", f"/api/gomoku/games/{game.id}/close/"), "認輸")
        game.refresh_from_db()
        self.assertFalse(game.ended)

    def test_the_home_list_offers_closing_only_to_the_host(self):
        game, _ = operations.start_game(GOMOKU, 1, {}, self.alice, "pvp")
        operations.join_game(game.room_code, self.bob)
        close = f"/games/{game.id}/close/"
        self.assertIn(close, self.clients["alice"].get("/").content.decode())
        self.assertNotIn(close, self.clients["bob"].get("/").content.decode())
        response = self.clients["alice"].post(close, {"next": "/history/"})
        self.assertRedirects(response, "/history/", fetch_redirect_response=False)
        self.assertTrue(Game.objects.get(pk=game.pk).ended)
        self.clients["bob"].post(close)  # 不是房主：不會改到任何東西（房間已經關了，也只是回到原頁）
        self.assertEqual(ModelRun.objects.filter(kind="req_close_room").count(), 2)  # 都經過 AI 助理

    def test_rooms_that_were_already_full_before_this_change_keep_going(self):
        # 原本最後一位加入就開始了；不當成已開始的話，進行到一半的對局會卡住、誰都不能下
        migration = importlib.import_module("arena.migrations.0009_room_start_chat")
        full = self.full_room()
        waiting, _ = operations.start_game(GOMOKU, 1, {}, self.alice, "pvp")
        migration.mark_old_rooms_started(apps, SimpleNamespace(connection=connection))
        full.refresh_from_db()
        waiting.refresh_from_db()
        self.assertTrue(full.started)
        self.assertIsNone(waiting.started_at)


class RoomChatTests(RefusalAssertions, TestCase):
    """房間聊天室（使用者要求，2026-11-13）：房主按開始前，玩家和觀戰的人在同一個聊天室；
    按了之後觀戰的人換到觀戰者專屬的聊天室，兩邊互相看不到（觀戰的人不能在聊天室替其中一方出主意）。
    每則訊息都經過 AI 助理（使用者選），但送出的一定是原文；對局結束後聊天紀錄留著、只能看。"""

    def setUp(self):
        self.users = {name: User.objects.create_user(name, password="x") for name in ("alice", "bob", "carol", "dave")}
        self.clients = {}
        for name, user in self.users.items():
            self.clients[name] = Client()
            self.clients[name].force_login(user)
        patcher = patch.object(orchestrator, "_chat", side_effect=OSError("測試不連模型"))
        self.model = patcher.start()
        self.addCleanup(patcher.stop)
        self.game, _ = operations.start_game(GOMOKU, 1, {}, self.users["alice"], "pvp")
        operations.join_game(self.game.room_code, self.users["bob"])
        operations.join_game(self.game.room_code, self.users["carol"])  # 座位坐滿：carol 改成觀戰
        self.url = f"/api/gomoku/games/{self.game.id}"

    def say(self, who, text):
        return self.clients[who].post(f"{self.url}/chat/", json.dumps({"text": text}), content_type="application/json")

    def heard(self, who):
        return [m["text"] for m in self.clients[who].get(f"{self.url}/").json()["chat"]]

    def test_before_the_start_players_and_spectators_share_one_room(self):
        for who in ("alice", "bob", "carol"):
            self.assertEqual(self.say(who, f"{who} 到了").status_code, 200)
        for who in ("alice", "bob", "carol"):
            self.assertEqual(self.heard(who), ["alice 到了", "bob 到了", "carol 到了"])

    def test_after_the_start_spectators_and_players_cannot_read_each_other(self):
        self.say("carol", "大家好")
        start_now(self.game)
        self.say("bob", "我要下天元")
        self.say("carol", "白棋應該擋左邊")  # 觀戰的人想出主意
        self.assertEqual(self.heard("alice"), ["大家好", "我要下天元"])
        self.assertEqual(self.heard("bob"), ["大家好", "我要下天元"])
        self.assertEqual(self.heard("carol"), ["大家好", "白棋應該擋左邊"])
        self.assertEqual(list(ChatMessage.objects.values_list("room", flat=True)), ["lobby", "players", "spectators"])

    def test_people_outside_the_room_can_neither_read_nor_post(self):
        self.assertEqual(self.say("dave", "我也要").status_code, 404)
        self.assertEqual(self.clients["dave"].get(f"{self.url}/").status_code, 404)
        self.assertFalse(ChatMessage.objects.exists())

    def test_messages_go_through_the_assistant_but_only_the_original_text_is_sent(self):
        # Agent 想改寫訊息：參數是鎖定的，改過的不送，最後照原文送出一次
        reworded = {"tool_calls": [{"function": {"name": "send_chat",
                                                 "arguments": {"game_id": self.game.id, "text": "改寫過的話"}}}]}
        self.model.side_effect = None
        self.model.return_value = reworded
        self.assertEqual(self.say("bob", "原本的話").status_code, 200)
        self.assertEqual(list(ChatMessage.objects.values_list("text", flat=True)), ["原本的話"])
        self.assertEqual(ModelRun.objects.get(kind="req_chat").outcome, "fallback")

    def test_empty_or_too_long_messages_are_refused_without_asking_the_model(self):
        with patch.object(orchestrator, "handle", side_effect=AssertionError("不必交給 AI 助理")):
            self.assertEqual(self.say("bob", "   ").status_code, 400)
            self.assertEqual(self.say("bob", "字" * (ChatMessage.MAX_LENGTH + 1)).status_code, 400)
        self.assertFalse(ChatMessage.objects.exists())

    def test_message_time_is_sent_with_its_time_zone_so_the_page_shows_local_time(self):
        # 2026-11-22 例行檢查：伺服器的 TIME_ZONE 是 UTC，原本送「14:30」這種只有時分的字串，
        # 台灣的玩家晚上 10:30 說的話顯示成 14:30。改送帶時區的 ISO 時間，由瀏覽器換成看的人所在的時區
        self.say("bob", "現在幾點")
        sent = ChatMessage.objects.get().to_dict(self.users["bob"])
        when = datetime.fromisoformat(sent["at"])
        self.assertIsNotNone(when.tzinfo)
        self.assertEqual(when, ChatMessage.objects.get().created_at)

    def test_after_the_game_the_chat_is_kept_but_read_only(self):
        self.say("bob", "請多指教")
        start_now(self.game)
        self.clients["alice"].post(f"{self.url}/move/", json.dumps({"action": "resign"}), content_type="application/json")
        self.assertRefused(self.say("bob", "再來一盤"), "只能看")
        self.assertEqual(self.heard("bob"), ["請多指教"])

    def test_new_messages_reach_each_connection_with_only_what_that_seat_may_read(self):
        from . import realtime
        start_now(self.game)
        self.say("bob", "玩家的話")
        self.say("carol", "觀戰的話")
        for who, seat, expected in (("alice", 1, "玩家的話"), ("carol", None, "觀戰的話")):
            sent = []
            consumer = realtime.GameConsumer()
            consumer.game_id, consumer.seat = self.game.id, seat
            consumer.scope = {"user": self.users[who]}
            consumer.send_json = sent.append
            with patch.object(orchestrator, "handle", side_effect=AssertionError("推送不經過 AI 助理")):
                consumer.chat_changed({"type": "chat.changed"})
            self.assertEqual([m["text"] for m in sent[0]["chat"]], [expected])


class RoomLobbyTests(RefusalAssertions, TestCase):
    """打完回到房間（使用者要求，2026-11-15：「完成遊戲後應該是要回到房間，而不是只有復盤然後遊戲結束」）。
    房間＝房間密碼相同的一串對局：每一盤打完，同樣的人回到房間大廳（下一盤，還沒按開始），房主再按開始打下一盤。
    每一盤各自是一筆對局（戰績、復盤一盤一盤算，到對局紀錄找）；聊天跨盤保留。
    在大廳裡（只有開始前）每個人能自己坐下或起身觀戰、離開房間；房主還能改設定、換遊戲、幫人換座位、請人離開（可以再回來）、
    轉讓房主。對局中這些都不行，否則快輸的一方換座位或離開就能逃掉。"""

    def setUp(self):
        self.users = {name: User.objects.create_user(name, password="x") for name in ("alice", "bob", "carol", "dave")}
        self.clients = {}
        for name, user in self.users.items():
            self.clients[name] = Client()
            self.clients[name].force_login(user)
        for patcher in (patch.object(orchestrator, "_chat", side_effect=OSError("測試不連模型")),
                        patch.object(agent, "_chat", side_effect=OSError("測試不連模型"))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def post(self, who, url, body=None):
        return self.clients[who].post(url, json.dumps(body or {}), content_type="application/json")

    def room(self, module=GOMOKU, settings=None, watcher="carol"):
        """alice 開房、bob 坐下、carol 觀戰（還沒按開始）。"""
        game, _ = operations.start_game(module, 1, settings or {}, self.users["alice"], "pvp")
        operations.join_game(game.room_code, self.users["bob"])
        if watcher:
            operations.watch_game(game.room_code, self.users[watcher])
        game.refresh_from_db()
        return game

    def finish(self, game):
        """按開始後 alice（黑棋先下）認輸，回傳認輸的回應。"""
        start_now(game)
        return self.post("alice", f"/api/gomoku/games/{game.id}/move/", {"action": "resign"})

    def seats(self, game):
        game.refresh_from_db()
        return {p.seat: p.user.username if p.user else None for p in game.players.all()}

    def heard(self, who, game_id):
        return [m["text"] for m in self.clients[who].get(f"/api/gomoku/games/{game_id}/").json()["chat"]]

    def test_after_a_game_everyone_is_back_in_the_room_for_the_next_one(self):
        game = self.room()
        r = self.finish(game)
        nxt = Game.objects.get(pk=r.json()["next_round"])
        game.refresh_from_db()
        self.assertEqual(game.winner, 2)
        self.assertEqual(game.summary()["next_round"], nxt.id)
        # 同一個房間：同一組密碼、房主、設定、座位、觀戰的人；還沒開始，由房主再按開始
        self.assertEqual((nxt.room_code, nxt.owner, nxt.game_type, nxt.actions),
                         (game.room_code, game.owner, "gomoku", []))
        self.assertEqual(self.seats(nxt), {1: "alice", 2: "bob"})
        self.assertEqual(nxt.summary()["spectators"], ["carol"])
        self.assertIsNone(nxt.started_at)
        self.assertEqual(nxt.summary()["room_code"], game.room_code)  # 座位坐滿了，別人仍可用密碼進來觀戰
        # 每一盤是各自的一筆：打完的那盤在對局紀錄可以復盤，下一盤是「回到房間」
        history = self.clients["alice"].get("/history/").content.decode()
        self.assertIn(f"/gomoku/?review={game.id}", history)
        self.assertIn(f"/gomoku/?resume={nxt.id}", history)
        self.assertEqual(operations._result(game, self.users["bob"]), "win")
        # 用房間密碼進來的人到的是現在這一盤（座位坐滿 → 觀戰）
        self.assertEqual(operations.join_game(game.room_code, self.users["dave"]), (nxt, None))
        # 第二盤打完再開第三盤，房間一直延續
        r = self.finish(nxt)
        self.assertEqual(Game.objects.get(pk=r.json()["next_round"]).room_code, game.room_code)

    def test_a_room_closed_by_the_host_does_not_come_back(self):
        game = self.room()
        operations.close_room(game, self.users["alice"])
        game.refresh_from_db()
        self.assertIsNone(operations._open_next_round(game))
        self.assertEqual(Game.objects.filter(room_code=game.room_code).count(), 1)

    def test_both_players_find_every_finished_game_of_the_room_and_review_their_own_side(self):
        # 2026-11-16 使用者要求：房間裡的「復盤」改成「對戰紀錄」，雙方都能復盤。
        # 對戰紀錄＝對局紀錄只列這個房間打完的每一盤；每一盤照舊只給坐過的人看，所以雙方都看得到、
        # 觀戰的人和外人看不到（不能拿別人房間的編號探出別人的對局）
        first = self.room()
        second = Game.objects.get(pk=self.finish(first).json()["next_round"])
        lobby = Game.objects.get(pk=self.finish(second).json()["next_round"])
        other, _ = operations.start_game(GOMOKU, 1, {}, self.users["alice"], "pvp")  # alice 的另一個房間
        for who, seat in (("alice", 1), ("bob", 2)):
            page = self.clients[who].get(f"/history/?room={lobby.id}").content.decode()
            self.assertIn("這個房間的對戰紀錄", page)
            for game in (first, second):
                self.assertIn(f"/gomoku/?review={game.id}", page)
                r = self.clients[who].get(f"/api/gomoku/games/{game.id}/review/")
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.json()["moments"], GOMOKU.review(game.settings, game.actions, seat))
            # 還沒打的這一盤、別的房間都不在這個房間的紀錄裡（頂端的「回到房間」不算，只看清單）
            listed = page[page.index('class="row-list"'):]
            self.assertNotIn(f"={lobby.id}\"", listed)
            self.assertNotIn(f"={other.id}\"", listed)
        for who in ("carol", "dave"):  # 觀戰的人、外人
            page = self.clients[who].get(f"/history/?room={lobby.id}").content.decode()
            self.assertIn("這個房間還沒有打完的對局", page)
            self.assertNotIn("?review=", page)

    def test_a_finished_game_points_back_to_the_room_s_current_game(self):
        # 2026-11-16 使用者回報 bug：AI 教練出現在房間裡。打完到回到房間之間重新整理（或按上一頁），
        # 網址還是打完的那一盤，網頁把它當成復盤打開。打完的那一盤要告訴網頁房間現在是哪一盤（換了遊戲也一樣），
        # 網頁才能回到房間；房間現在的那一盤沒有這個欄位，所以不會一直跳
        first = self.room()
        second = Game.objects.get(pk=self.finish(first).json()["next_round"])
        third = Game.objects.get(pk=self.finish(second).json()["next_round"])
        r = self.post("alice", f"/api/gomoku/games/{third.id}/room/", {"seat": 1, "game_type": "weiqi", "settings": {}})
        self.assertEqual(r.status_code, 200)
        for game in (first, second):
            detail = self.clients["bob"].get(f"/api/gomoku/games/{game.id}/").json()
            self.assertEqual(detail["game"]["current_round"], {"id": third.id, "game_type": "weiqi"})
        self.assertIsNone(self.clients["bob"].get(f"/api/weiqi/games/{third.id}/").json()["game"]["current_round"])

    def test_chat_carries_over_between_games_and_each_saved_game_keeps_its_own(self):
        game = self.room()
        url = f"/api/gomoku/games/{game.id}"
        self.post("alice", f"{url}/chat/", {"text": "開始前"})
        start_now(game)
        self.post("bob", f"{url}/chat/", {"text": "玩家說"})
        self.post("carol", f"{url}/chat/", {"text": "觀戰說"})
        nxt = Game.objects.get(pk=self.post("alice", f"{url}/move/", {"action": "resign"}).json()["next_round"])
        self.post("carol", f"/api/gomoku/games/{nxt.id}/chat/", {"text": "下一盤"})
        # 回到房間後，上一盤兩邊的聊天都看得到（打完了，不會再替誰出主意）
        for who in ("alice", "carol"):
            self.assertEqual(self.heard(who, nxt.id), ["開始前", "玩家說", "觀戰說", "下一盤"])
        # 從對局紀錄打開上一盤：看得到到那一盤為止的聊天
        self.assertEqual(self.heard("bob", game.id), ["開始前", "玩家說", "觀戰說"])
        # 下一盤開始後，兩邊又分開
        start_now(nxt)
        self.post("bob", f"/api/gomoku/games/{nxt.id}/chat/", {"text": "第二盤玩家"})
        self.assertNotIn("第二盤玩家", self.heard("carol", nxt.id))

    def test_people_sit_down_or_watch_by_themselves_only_in_the_room(self):
        game = self.room()
        url = f"/api/gomoku/games/{game.id}"
        self.assertEqual(self.post("bob", f"{url}/seat/", {"seat": 0}).status_code, 200)  # 起身觀戰
        self.assertEqual(self.seats(game), {1: "alice", 2: None})
        self.assertEqual(sorted(game.summary()["spectators"]), ["bob", "carol"])
        self.assertEqual(self.post("carol", f"{url}/seat/", {"seat": 2}).status_code, 200)  # 觀戰的人坐下
        self.assertEqual(self.seats(game), {1: "alice", 2: "carol"})
        self.assertEqual(game.summary()["spectators"], ["bob"])
        self.assertRefused(self.post("bob", f"{url}/seat/", {"seat": 2}), "已經有人")
        self.assertRefused(self.post("bob", f"{url}/seat/", {"seat": 1, "player": "carol"}), "只有房主")
        self.assertEqual(ModelRun.objects.filter(kind="req_room_seat").count(), 4)  # 每個操作都經過 AI 助理
        # 開始後不能換座位、起身（不然快輸的一方就能逃掉）
        start_now(game)
        self.assertRefused(self.post("carol", f"{url}/seat/", {"seat": 0}), "已經開始")
        self.assertEqual(self.seats(game), {1: "alice", 2: "carol"})

    def test_the_host_moves_people_kicks_and_hands_over_the_room(self):
        game = self.room()
        url = f"/api/gomoku/games/{game.id}"
        # 房主把 bob 換到自己的座位：兩人交換，房主的座位跟著換
        self.assertEqual(self.post("alice", f"{url}/seat/", {"player": "bob", "seat": 1}).status_code, 200)
        self.assertEqual(self.seats(game), {1: "bob", 2: "alice"})
        self.assertEqual(game.human_seat, 2)
        # 房主把觀戰的 carol 換進 bob 的座位：bob 改成觀戰
        self.post("alice", f"{url}/seat/", {"player": "carol", "seat": 1})
        self.assertEqual(self.seats(game), {1: "carol", 2: "alice"})
        self.assertEqual(game.summary()["spectators"], ["bob"])
        # 請人離開：只有房主能請；被請出去的人可以再用密碼回來（使用者決定）
        self.assertRefused(self.post("carol", f"{url}/kick/", {"player": "alice"}), "只有房主")
        self.assertEqual(self.post("alice", f"{url}/kick/", {"player": "carol"}).status_code, 200)
        self.assertEqual(self.seats(game), {1: None, 2: "alice"})
        self.assertEqual(self.clients["carol"].get(f"{url}/").status_code, 404)
        self.assertEqual(operations.join_game(game.room_code, self.users["carol"]), (game, 1))
        # 轉讓房主：交出去之後原房主就不能再管房間（交給觀戰的人見 test_the_host_can_watch_and_still_runs_the_room）
        self.assertEqual(self.post("alice", f"{url}/host/", {"player": "carol"}).status_code, 200)
        game.refresh_from_db()
        self.assertEqual((game.owner.username, game.human_seat), ("carol", 1))
        self.assertRefused(self.post("alice", f"{url}/kick/", {"player": "bob"}), "只有房主")
        self.assertTrue(self.clients["carol"].get(f"{url}/").json()["is_owner"])
        self.assertFalse(self.clients["alice"].get(f"{url}/").json()["is_owner"])

    def test_the_host_can_switch_games_and_whoever_does_not_fit_watches(self):
        # 換成撲克（2026-11-16 起是開放牌桌）：牌桌是空的，房間裡的人都到觀戰席，自己上桌
        from holdem.module import MODULE as HOLDEM
        game = self.room(watcher="")
        operations.join_game(game.room_code, self.users["carol"])  # 座位滿了：觀戰
        r = self.post("alice", f"/api/gomoku/games/{game.id}/room/",
                      {"game_type": "holdem", "seat": 1, "settings": {"players": 3}})
        self.assertEqual(r.status_code, 200)
        game.refresh_from_db()
        self.assertEqual(game.game_type, "holdem")
        self.assertEqual(r.json()["game"]["id"], game.id)
        self.assertEqual(HOLDEM.replay(operations.settings_of(game), game.actions).occupant, {})
        self.assertEqual(sorted(game.summary()["spectators"]), ["alice", "bob", "carol"])
        self.assertEqual(self.clients["bob"].get(f"/api/holdem/games/{game.id}/").status_code, 200)
        with patch("arena.operations.secrets.choice", side_effect=lambda seats: seats[-1]):
            for who in ("bob", "carol"):
                self.post(who, f"/api/holdem/games/{game.id}/table/", {"op": "sit"})  # bob 3 號位、carol 2 號位
        # 座位變少：坐不下的人（3 號位的 bob）改成觀戰，不會被移出房間
        self.post("alice", f"/api/holdem/games/{game.id}/room/", {"seat": 1, "settings": {"players": 2}})
        game.refresh_from_db()
        occupant = HOLDEM.replay(operations.settings_of(game), game.actions).occupant
        self.assertEqual(occupant, {2: HOLDEM.member_token(self.users["carol"])})
        self.assertIn("bob", game.summary()["spectators"])
        # 撲克的座位是隨機的，房主不能幫人換座位；房主可以交給觀戰席的人（開放牌桌的房主不一定在桌上）
        self.assertRefused(self.post("alice", f"/api/holdem/games/{game.id}/seat/", {"player": "carol", "seat": 1}),
                           "隨機")
        self.post("alice", f"/api/holdem/games/{game.id}/host/", {"player": "bob"})
        game.refresh_from_db()
        self.assertEqual(game.owner.username, "bob")

    def test_the_host_can_watch_and_still_runs_the_room(self):
        # 2026-11-18 使用者：「為什麼房主還是不能自己到觀戰去」「這是錯誤的規則」。房主改成觀戰後仍然是房主：
        # 改設定、開始、轉讓、請人離開照樣能做；但他和其他觀戰的人一樣沒有座位，這盤不算他的（不進戰績、不能復盤），
        # 否則觀戰的房主會被算成輸棋（_result 拿 None 比贏家），還能用 AI 教練分析別人下的棋
        game = self.room()
        url = f"/api/gomoku/games/{game.id}"
        self.assertEqual(self.post("alice", f"{url}/seat/", {"seat": 0}).status_code, 200)
        self.assertEqual(self.seats(game), {1: None, 2: "bob"})
        self.assertEqual(sorted(game.summary()["spectators"]), ["alice", "carol"])
        detail = self.clients["alice"].get(f"{url}/").json()
        self.assertEqual((detail["my_seat"], detail["is_owner"], detail["game"]["owner"]), (None, True, "alice"))
        # 觀戰中改設定：照樣觀戰，坐著的人照樣坐著
        r = self.post("alice", f"{url}/room/", {"seat": 1, "settings": {}})
        self.assertEqual((r.status_code, r.json()["my_seat"]), (200, None))
        self.assertEqual(self.seats(game), {1: "bob", 2: None})
        self.assertIn("alice", game.summary()["spectators"])
        # 觀戰中幫人換座位、把房主交給觀戰的人再拿回來
        self.assertEqual(self.post("alice", f"{url}/seat/", {"player": "carol", "seat": 2}).status_code, 200)
        self.assertEqual(self.seats(game), {1: "bob", 2: "carol"})
        self.assertEqual(self.post("alice", f"{url}/host/", {"player": "dave"}).status_code, 400)  # 不在房間裡
        operations.watch_game(game.room_code, self.users["dave"])
        self.assertEqual(self.post("alice", f"{url}/host/", {"player": "dave"}).status_code, 200)
        self.assertEqual(self.post("dave", f"{url}/host/", {"player": "alice"}).status_code, 200)
        # 觀戰中按開始
        with patch.object(operations.time, "sleep"), patch.object(operations, "START_COUNTDOWN", 0):
            self.assertEqual(self.post("alice", f"{url}/start/").status_code, 200)
        self.assertEqual(self.post("alice", f"{url}/move/", {"action": "H8"}).status_code, 404)  # 觀戰不能下
        r = self.post("bob", f"{url}/move/", {"action": "resign"})  # bob 執黑認輸，carol 贏
        nxt = Game.objects.get(pk=r.json()["next_round"])
        game.refresh_from_db()
        # 這盤不是房主的：不列在他的對局紀錄、不進戰績、不能復盤；下一盤他照樣是觀戰的房主
        self.assertNotIn(game, operations._played_games(self.users["alice"]))
        self.assertEqual(self.clients["alice"].get(f"{url}/review/").status_code, 404)
        self.assertEqual(self.clients["bob"].get(f"{url}/review/").status_code, 200)
        self.assertEqual((nxt.owner.username, self.seats(nxt)), ("alice", {1: "bob", 2: "carol"}))
        self.assertIn("alice", nxt.summary()["spectators"])
        # 頂端的「回到房間」照樣帶房主回去
        self.assertEqual([r["id"] for r in operations.active_rooms(self.users["alice"])], [nxt.id])

    def test_the_room_stays_while_anyone_is_in_it(self):
        # 2026-11-18 使用者：「就算對局桌沒有人，但只要房間內有人房間就能繼續存在」。
        # 房主離開時先交給坐著的人，沒有人坐著就交給觀戰的人；房間裡沒有別人才關閉
        game = self.room()
        url = f"/api/gomoku/games/{game.id}"
        self.post("bob", f"{url}/leave/")
        self.post("alice", f"{url}/seat/", {"seat": 0})
        self.assertEqual(self.seats(game), {1: None, 2: None})
        self.assertEqual(self.post("alice", f"{url}/leave/").status_code, 200)  # 觀戰中的房主離開
        game.refresh_from_db()
        self.assertFalse(operations.closed_room(game))
        self.assertEqual((game.owner.username, game.summary()["spectators"]), ("carol", ["carol"]))
        self.assertEqual(operations.join_game(game.room_code, self.users["bob"]), (game, 1))  # 房間密碼照樣能用
        # 坐著的人優先接手
        operations.watch_game(game.room_code, self.users["dave"])
        self.post("carol", f"{url}/leave/")
        game.refresh_from_db()
        self.assertEqual(game.owner.username, "bob")
        # 剩最後一個人離開才關閉
        self.post("dave", f"{url}/leave/")
        self.post("bob", f"{url}/leave/")
        game.refresh_from_db()
        self.assertTrue(operations.closed_room(game))

    def test_the_host_can_seat_an_ai_in_an_empty_seat(self):
        # 2026-11-18 使用者：「5個遊戲都可以讓ai進來」；做法由使用者決定：房主在空位請 AI 坐（撲克開放牌桌照舊「補 AI」）。
        # AI 坐的座位不算真人座位：不必等人、加入的人坐不進去，輪到時由 AI 下；房間的下一盤、改設定後照樣由 AI 坐
        game = self.room(watcher="")
        url = f"/api/gomoku/games/{game.id}"
        self.post("bob", f"{url}/seat/", {"seat": 0})  # bob 起身觀戰，白棋空出來
        self.assertRefused(self.post("bob", f"{url}/seat_ai/", {"seat": 2, "on": True}), "只有房主")
        self.assertRefused(self.post("alice", f"{url}/seat_ai/", {"seat": 1, "on": True}), "已經有人")
        self.assertEqual(self.post("alice", f"{url}/seat_ai/", {"seat": 2, "on": True}).status_code, 200)
        game.refresh_from_db()
        self.assertEqual((game.summary()["ai_seats"], game.waiting, game.human_seats()), ([2], False, {1}))
        # 加入的人坐不進 AI 的座位（改成觀戰），也不能換進去
        self.assertEqual(operations.join_game(game.room_code, self.users["dave"]), (game, None))
        self.assertRefused(self.post("dave", f"{url}/seat/", {"seat": 2}), "由 AI 坐")
        # 至少留一個座位給真人
        self.post("alice", f"{url}/seat/", {"seat": 0})
        self.assertRefused(self.post("alice", f"{url}/seat_ai/", {"seat": 1, "on": True}), "至少要留一個座位給真人")
        self.post("alice", f"{url}/seat/", {"seat": 1})
        # 改設定後照樣由 AI 坐
        self.post("alice", f"{url}/room/", {"seat": 1, "settings": {}})
        self.assertEqual(game.summary()["ai_seats"], [2])
        # 開始後 AI 接著真人下（模型連不上時由電腦下，照樣是 AI 的座位）
        start_now(game)
        r = self.post("alice", f"{url}/move/", {"action": "H8"})
        self.assertEqual([m["seat"] for m in r.json()["ai_moves"]], [2])
        game.refresh_from_db()
        self.assertEqual(len(game.actions), 2)
        self.assertRefused(self.post("alice", f"{url}/seat_ai/", {"seat": 2, "on": False}), "已經開始")
        # 打完回到房間：下一盤照樣由 AI 坐，房主可以請它離座
        nxt = Game.objects.get(pk=self.post("alice", f"{url}/move/", {"action": "resign"}).json()["next_round"])
        self.assertEqual(nxt.summary()["ai_seats"], [2])
        self.assertEqual(self.post("alice", f"/api/gomoku/games/{nxt.id}/seat_ai/", {"seat": 2, "on": False}).status_code, 200)
        self.assertEqual((nxt.summary()["ai_seats"], nxt.waiting), ([], True))

    def test_the_host_can_ban_the_poker_equity_table_in_a_room(self):
        # 2026-11-18 使用者：「德州撲克多一個設定 禁用概率列表 讓玩家不能看」；使用者決定只在真人房間由房主設定，
        # 和 AI 對局時不受影響（自己練習時看勝率表不影響別人）
        from holdem.module import MODULE as HOLDEM
        banned = HOLDEM.clean_settings({"equity_table": "hide"})
        room, _ = operations.start_game(HOLDEM, 1, banned, self.users["alice"], "pvp")
        self.assertEqual(room.settings["equity_table"], "hide")
        solo, _ = operations.start_game(HOLDEM, 1, banned, self.users["alice"])
        self.assertNotIn("equity_table", solo.settings)
        with self.assertRaises(ValueError):
            HOLDEM.clean_settings({"equity_table": "maybe"})
        # 房主在房間裡改回來
        r = self.post("alice", f"/api/holdem/games/{room.id}/room/", {"seat": 1, "settings": {"players": 6}})
        self.assertEqual(r.status_code, 200)
        room.refresh_from_db()
        self.assertNotIn("equity_table", room.settings)
        # 設定欄只在選真人對戰時出現（網頁依 data-pvp-only 切換）
        self.assertContains(self.clients["alice"].get("/holdem/"), 'data-pvp-only hidden>勝率表')

    def test_leaving_the_room(self):
        game = self.room()
        url = f"/api/gomoku/games/{game.id}"
        self.assertEqual(self.post("carol", f"{url}/leave/").status_code, 200)  # 觀戰的人離開
        self.assertEqual(game.summary()["spectators"], [])
        # 房主離開：房主交給還坐著的人
        self.assertEqual(self.post("alice", f"{url}/leave/").status_code, 200)
        game.refresh_from_db()
        self.assertEqual((game.owner.username, game.human_seat, self.seats(game)), ("bob", 2, {1: None, 2: "bob"}))
        self.assertNotIn(game, Game.visible_to(self.users["alice"]))
        # 最後一個人離開：房間關閉，清單上也不再列出
        self.post("bob", f"{url}/leave/")
        game.refresh_from_db()
        self.assertTrue(operations.closed_room(game))
        self.assertEqual(operations.active_rooms(self.users["bob"]), [])
        # 對局中不能離開（要認輸）
        other = self.room(watcher="")
        start_now(other)
        self.assertRefused(self.post("bob", f"/api/gomoku/games/{other.id}/leave/"), "認輸")

    def test_a_kicked_player_is_told_through_the_live_connection(self):
        from . import realtime
        game = self.room()
        sent = []
        consumer = realtime.GameConsumer()
        consumer.game_id, consumer.seat, consumer.scope = game.id, 2, {"user": self.users["bob"]}
        consumer.send_json = sent.append
        consumer.close = lambda *args, **kwargs: sent.append("closed")
        # 房主幫 bob 換到觀戰：推送依 bob 現在的身分（觀戰，座位 None）重算
        operations.move_player(GOMOKU, game, self.users["alice"], "bob", None)
        consumer.game_changed(None)
        self.assertIsNone(sent[-1]["my_seat"])
        operations.kick_player(game, self.users["alice"], "bob")
        consumer.game_changed(None)
        self.assertEqual(sent[-2:], [{"type": "left"}, "closed"])


class RoomListAndLockTests(TestCase):
    """房間列表與房間鎖（使用者要求，2026-11-18：「新增房間列表 房間預設公開 房主可自行設定房間是否上鎖 密碼可以自己設定或是隨機」）。
    房間預設公開：有人在線上的房間都列在房間列表，誰都能加入或觀戰。房主上鎖後照樣列出、標鎖頭（使用者決定），
    但不在房間裡的人加入、觀戰都要密碼（使用者決定觀戰也要），否則上鎖擋不住陌生人進來看。
    密碼只給房主看（要傳給朋友），也不寫進給模型的任務說明。"""

    def setUp(self):
        self.users = {name: User.objects.create_user(name, password="x") for name in ("alice", "bob", "carol")}
        self.clients = {}
        for name, user in self.users.items():
            self.clients[name] = Client()
            self.clients[name].force_login(user)
        self.prompts = []

        def no_model(messages, *args, **kwargs):
            self.prompts.append(json.dumps(messages, ensure_ascii=False))
            raise OSError("測試不連模型")
        patcher = patch.object(orchestrator, "_chat", side_effect=no_model)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.game, _ = operations.start_game(GOMOKU, 1, {}, self.users["alice"], "pvp")
        self.url = f"/api/gomoku/games/{self.game.id}"

    def post(self, who, url, body=None):
        return self.clients[who].post(url, json.dumps(body or {}), content_type="application/json")

    def enter(self, who, action="join", password=None):
        data = {"code": self.game.room_code, "action": action}
        if password is not None:
            data["password"] = password
        return self.clients[who].post("/join/", data)

    def test_rooms_are_public_by_default_and_listed_while_someone_is_there(self):
        # 房間裡沒有人在線上（沒人在網站上活動）：沒人管的舊房間不列出
        self.assertNotContains(self.clients["bob"].get("/join/"), self.game.room_code)
        self.clients["alice"].get("/")  # 房主在線上
        page = self.clients["bob"].get("/join/")
        self.assertContains(page, self.game.room_code)
        self.assertContains(page, "公開")
        self.assertContains(page, "座位 1／2 人")
        # 公開的房間不用密碼
        self.assertRedirects(self.enter("bob"), f"/gomoku/?resume={self.game.id}", fetch_redirect_response=False)
        self.assertContains(self.clients["bob"].get("/join/"), "你在這個房間")
        # 打完一盤回到房間：列表上是同一個房間（現在這一盤），不會一盤列一次
        start_now(self.game)
        nxt = self.post("alice", f"{self.url}/move/", {"action": "resign"}).json()["next_round"]
        page = self.clients["carol"].get("/join/").content.decode()
        self.assertEqual(page.count(f"房間編號 {self.game.room_code}"), 1)
        self.assertEqual(Game.objects.get(pk=nxt).room_code, self.game.room_code)
        # 房主關閉的房間不列出
        operations.close_room(Game.objects.get(pk=nxt), self.users["alice"])
        self.assertNotContains(self.clients["carol"].get("/join/"), self.game.room_code)

    def test_a_locked_room_needs_its_password_to_join_or_watch(self):
        operations.join_game(self.game.room_code, self.users["bob"])  # 上鎖前就在房間裡
        r = self.post("alice", f"{self.url}/lock/", {"lock": True, "password": "tea42"})
        self.assertEqual((r.status_code, r.json()["room_password"]), (200, "tea42"))
        # 密碼只給房主；別人只知道上鎖了
        self.assertEqual(self.clients["alice"].get(f"{self.url}/").json()["room_password"], "tea42")
        bob = self.clients["bob"].get(f"{self.url}/").json()
        self.assertNotIn("room_password", bob)
        self.assertTrue(bob["game"]["locked"])
        self.assertNotIn("tea42", json.dumps(self.game.summary(), ensure_ascii=False))
        # 不在房間裡的人：加入、觀戰都要密碼，錯的也不行
        self.assertContains(self.enter("carol"), "請輸入房間密碼")
        self.assertContains(self.enter("carol", "watch"), "請輸入房間密碼")
        self.assertContains(self.enter("carol", "watch", "TEA42"), "房間密碼不對")
        self.assertEqual(self.clients["carol"].get(f"{self.url}/").status_code, 404)
        self.assertRedirects(self.enter("carol", "watch", "tea42"), f"/gomoku/?watch={self.game.id}",
                             fetch_redirect_response=False)
        # 已經在房間裡的人不必再輸入（重新整理、從列表回到房間）
        self.assertEqual(operations.join_game(self.game.room_code, self.users["bob"]), (self.game, 2))
        self.assertEqual(operations.watch_game(self.game.room_code, self.users["carol"]), (self.game, None))
        # 房間列表照樣列出，標鎖頭
        self.clients["alice"].get("/")
        page = self.clients["carol"].get("/join/").content.decode()
        self.assertIn(self.game.room_code, page)
        self.assertIn("🔒 上鎖", page)
        # 下一盤（回到房間）沿用房間鎖；解鎖後誰都能進來
        start_now(self.game)
        nxt = Game.objects.get(pk=self.post("alice", f"{self.url}/move/", {"action": "resign"}).json()["next_round"])
        self.assertEqual(nxt.room_password, "tea42")
        self.assertEqual(self.post("alice", f"/api/gomoku/games/{nxt.id}/lock/", {"lock": False}).status_code, 200)
        nxt.refresh_from_db()
        self.assertEqual(nxt.room_password, "")
        # 密碼從沒寫進給模型的任務說明（上鎖、加入、觀戰都經過 AI 助理）
        self.assertTrue(self.prompts)
        self.assertFalse([p for p in self.prompts if "tea42" in p.lower()])

    def test_the_password_is_random_when_left_blank_and_only_the_owner_sets_it(self):
        r = self.post("alice", f"{self.url}/lock/", {"lock": True})
        password = r.json()["room_password"]
        self.assertEqual(len(password), 6)
        self.assertNotEqual(password, self.game.room_code)
        operations.join_game(self.game.room_code, self.users["bob"], password)
        self.assertEqual(self.post("bob", f"{self.url}/lock/", {"lock": False}).status_code, 400)
        self.assertEqual(self.post("alice", f"{self.url}/lock/", {"lock": True, "password": "x" * 13}).status_code, 400)
        self.assertEqual(self.post("alice", f"{self.url}/lock/", {"lock": True, "password": "a b"}).status_code, 400)
        self.game.refresh_from_db()
        self.assertEqual(self.game.room_password, password)

    def test_chinese_passwords_are_checked_instead_of_crashing_the_page(self):
        # 2026-11-20 審核 #20：secrets.compare_digest 遇到含中文的字串會丟出 TypeError，加入頁變成 500。
        # 玩家用中文輸入法打錯字、或房主設了中文密碼，都要照常回答「密碼不對」或讓人進來
        self.post("alice", f"{self.url}/lock/", {"lock": True, "password": "tea42"})
        self.assertContains(self.enter("carol", "join", "茶42"), "房間密碼不對")
        self.assertEqual(self.post("alice", f"{self.url}/lock/", {"lock": True, "password": "喝茶"}).status_code, 200)
        self.assertContains(self.enter("carol", "watch", "喝水"), "房間密碼不對")
        self.assertRedirects(self.enter("carol", "watch", "喝茶"), f"/gomoku/?watch={self.game.id}",
                             fetch_redirect_response=False)


class RoomAddressTests(TestCase):
    """直接把房間網址貼進瀏覽器（2026-11-20 使用者回報「對方輸入房間網址導致進入房間導致無法正確讀取」）。
    房間頁的網址是 /<遊戲>/?resume=<編號>（或 ?watch=），朋友常常直接把它傳給對方，而不是邀請連結。
    原本打開後網頁去讀 /api/<遊戲>/games/<編號>/，不在房間裡的人、網址上的遊戲和房間現在的遊戲不同時都是 404 的錯誤網頁，
    畫面只寫「伺服器發生錯誤（404），這一步沒有存檔」，棋盤空白。
    要的是：已經在房間裡的人照常進去；不在房間裡的人到加入／觀戰的確認頁（加入照舊要按按鈕送 POST，上鎖照舊要密碼，
    網址本身不能讓人坐進座位或繞過房間鎖）；看不到的對局說清楚原因。"""

    def setUp(self):
        self.users = {name: User.objects.create_user(name, password="x") for name in ("alice", "bob")}
        # 對方常常是遊客（沒有密碼的帳號，accounts.is_guest）
        self.users["guest"] = User.objects.create_user("遊客-ABC234")
        self.clients = {}
        for name, user in self.users.items():
            self.clients[name] = Client()
            self.clients[name].force_login(user)
        patcher = patch.object(orchestrator, "_chat", side_effect=OSError("測試不連模型"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.game, _ = operations.start_game(GOMOKU, 1, {}, self.users["alice"], "pvp")
        operations.join_game(self.game.room_code, self.users["bob"])
        self.join_url = f"/join/{self.game.room_code}/"

    def open(self, who, path):
        return self.clients[who].get(path)

    def test_someone_outside_the_room_is_asked_to_join_or_watch_instead_of_getting_a_broken_page(self):
        for query in ("resume", "watch"):
            r = self.open("guest", f"/gomoku/?{query}={self.game.id}")
            self.assertRedirects(r, self.join_url, fetch_redirect_response=False)
        page = self.clients["guest"].get(self.join_url)
        self.assertContains(page, "你還不在這個房間裡")
        self.assertContains(page, 'value="watch"')
        # 打開網址不會坐進座位、也不會變成觀戰：加入照舊要在確認頁按按鈕（POST）
        self.game.refresh_from_db()
        self.assertIsNone(self.game.seat_of(self.users["guest"]))
        self.assertFalse(self.game.in_room(self.users["guest"]))
        self.assertEqual(self.open("guest", f"/api/gomoku/games/{self.game.id}/").status_code, 404)

    def test_a_locked_room_s_address_still_needs_the_password(self):
        operations.set_room_lock(self.game, self.users["alice"], True, "tea42")
        self.assertRedirects(self.open("guest", f"/gomoku/?resume={self.game.id}"), self.join_url,
                             fetch_redirect_response=False)
        self.assertContains(self.clients["guest"].post("/join/", {"code": self.game.room_code}), "請輸入房間密碼")
        self.assertIsNone(Game.objects.get(pk=self.game.pk).seat_of(self.users["guest"]))
        self.assertEqual(self.open("guest", f"/api/gomoku/games/{self.game.id}/").status_code, 404)

    def test_people_in_the_room_get_in_even_after_the_host_switched_games(self):
        # 坐著的人與觀戰的人照常打開
        operations.watch_game(self.game.room_code, self.users["guest"])
        for who in ("bob", "guest"):
            self.assertEqual(self.open(who, f"/gomoku/?resume={self.game.id}").status_code, 200)
        # 房主在大廳把五子棋換成撲克：同一盤（編號不變）換了遊戲，朋友手上還是五子棋的網址
        from holdem.module import MODULE as HOLDEM
        operations.change_room(HOLDEM, self.game, self.users["alice"], 1, {"players": 4})
        for who, query in (("bob", "resume"), ("guest", "watch")):
            self.assertRedirects(self.open(who, f"/gomoku/?{query}={self.game.id}"), f"/holdem/?{query}={self.game.id}",
                                 fetch_redirect_response=False)
        self.assertEqual(self.open("bob", f"/holdem/?resume={self.game.id}").status_code, 200)

    def test_an_old_round_s_address_leads_to_the_room_s_current_game(self):
        start_now(self.game)
        operations.play_human_move(GOMOKU, self.game, "resign", 1)  # 打完一盤，房間開下一盤
        nxt = Game.objects.filter(room_code=self.game.room_code).order_by("-id").first()
        self.assertNotEqual(nxt.id, self.game.id)
        # 房間裡的人：舊的一盤照樣打開（網頁看 current_round 回到現在那一盤）
        self.assertEqual(self.open("bob", f"/gomoku/?resume={self.game.id}").status_code, 200)
        # 不在房間裡的人：到這個房間的加入確認頁
        self.assertRedirects(self.open("guest", f"/gomoku/?resume={self.game.id}"), self.join_url,
                             fetch_redirect_response=False)

    def test_addresses_of_games_you_cannot_see_say_why(self):
        # 別人和 AI 的對局、不存在的編號：一樣說找不到（不透露那盤存不存在）
        solo, _ = operations.start_game(GOMOKU, 1, {}, self.users["alice"], "ai")
        for game_id in (solo.id, 999999):
            r = self.open("guest", f"/gomoku/?resume={game_id}")
            self.assertRedirects(r, "/", fetch_redirect_response=False)
            self.assertContains(self.clients["guest"].get("/"), "找不到這盤對局")
        # 房主關掉的房間
        operations.close_room(self.game, self.users["alice"])
        r = self.open("guest", f"/gomoku/?resume={self.game.id}")
        self.assertRedirects(r, "/join/", fetch_redirect_response=False)
        self.assertContains(self.clients["guest"].get("/join/"), "這個房間已經關閉了")
        # 沒帶編號的網址照舊開新局，沒有被轉走
        self.assertEqual(self.open("guest", "/gomoku/").status_code, 200)
        self.assertEqual(self.open("guest", "/gomoku/?room=new").status_code, 200)

    def test_a_poker_player_who_left_the_room_rejoins_instead_of_opening_a_dead_page(self):
        # 撲克牌桌打過牌後離開房間的人（GamePlayer.left）還看得到那一盤（可以復盤），但已經不在房間裡：
        # 直接打開的話頁面讀得到、即時同步卻連不上（realtime.seat_for 看 in_room），所以要先回到加入確認頁
        table = poker_table(self.users["alice"], others=(self.users["bob"],), ai=("bot",))
        start_now(table)
        GamePlayer.objects.filter(game=table, user=self.users["bob"]).update(left=True)
        self.assertRedirects(self.open("bob", f"/holdem/?resume={table.id}"), f"/join/{table.room_code}/",
                             fetch_redirect_response=False)
        # 房主沒上桌（在觀戰席）也照常打開自己的房間
        self.assertEqual(self.open("alice", f"/holdem/?resume={table.id}").status_code, 200)
