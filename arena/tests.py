import codecs
import csv
import importlib
import io
import json
import platform
import tempfile
import urllib.error
import zipfile
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.apps import apps
from django.conf import settings
from django.core.management import call_command
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings

from gomoku.module import MODULE as GOMOKU

from . import agent, narrator
from .base import ESTIMATE
from .management.commands.model_report import summarize
from .models import AiNote, Game, ModelRun


class NarrationCheckTests(SimpleTestCase):
    FACTS = {"kind": "ai_move", "points": [{"evidence": ESTIMATE, "text": "依引擎估計，H9 的作用：形成活三（橫向）。"}]}

    def test_rejects_coordinates_not_in_facts(self):
        # 語言模型自己編出的棋步不能送到玩家面前。
        self.assertIsNotNone(narrator.check_text("我下在 H9，之後可以接 J11。", self.FACTS, GOMOKU.coord_re))

    def test_rejects_certainty_when_only_estimated(self):
        self.assertIsNotNone(narrator.check_text("H9 一定是最好的一手。", self.FACTS, GOMOKU.coord_re))

    def test_accepts_faithful_text(self):
        self.assertIsNone(narrator.check_text("依引擎估計，我下在 H9 是為了形成橫向活三。", self.FACTS, GOMOKU.coord_re))

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
    def test_benchmark_unload_request_carries_num_ctx(self):
        from .management.commands import benchmark_models
        sent = self.sent_bodies(lambda: benchmark_models._unload("qwen3:8b"))
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["options"]["num_ctx"], 4096)

    def test_default_is_8192(self):
        self.assertEqual(settings.OLLAMA_NUM_CTX, 8192)  # 沒設環境變數 OLLAMA_NUM_CTX 時


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
    # 使用者要比較各模型的速度與成功率（2026-10-19）。以前只有 Agent 自己下成功的手記了模型與秒數：
    # 改由引擎代下的手、說明、提問都沒記，算不出成功率，秒數也只算到順利的手而偏快。
    OPEN_THREE = ["H8", "A15", "I8", "O15", "J8"]  # 黑 H8-J8 活三，輪到白（Agent）
    FACTS = {"kind": "review", "points": [{"evidence": ESTIMATE, "text": "白棋下 H9。"}]}

    def post(self, url, body):
        return self.client.post(url, json.dumps(body), content_type="application/json")

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
        run = ModelRun.objects.get()
        self.assertEqual((run.kind, run.game_id, run.index, run.model, run.outcome),
                         ("agent", game_id, 1, "qwen3:8b", "ok"))
        # 同名模型在不同伺服器、不同電腦上速度不同，比較時要分得開
        self.assertEqual((run.server, run.machine), ("127.0.0.1", platform.node()))

    def test_moves_that_need_no_model_are_not_recorded(self):
        # AI 執黑的第一手由程式直接下；記進去會把模型的秒數拉低
        self.post("/api/gomoku/games/", {"seat": 2})
        self.assertFalse(ModelRun.objects.exists())

    def test_explanation_failures_are_told_apart(self):
        # 說明沒通過檢查是模型寫錯；呼叫不到是伺服器的問題，要分開算
        for reply in ("重點：擋住。\n說明：先擋。", "重點：下 N2。\n說明：N2 比較好。", OSError("timed out")):
            with patch("arena.narrator._ask_ollama", side_effect=[reply]):
                self.post("/api/gomoku/narrate/", {"facts": self.FACTS, "model": "qwen3:8b"})
        self.assertEqual([(r.kind, r.model, r.outcome) for r in ModelRun.objects.order_by("id")],
                         [("review", "qwen3:8b", "ok"), ("review", "qwen3:8b", "fallback"),
                          ("review", "qwen3:8b", "error")])

    def test_a_saved_explanation_is_not_counted_again(self):
        game = Game.objects.create(game_type="gomoku", human_seat=1, actions=["H8", "H9"])
        AiNote.objects.create(game=game, index=1, seat=2, facts={**self.FACTS, "kind": "ai_move"})
        with patch("arena.narrator._ask_ollama", return_value="重點：跟著下。\n說明：靠近黑棋。"):
            for _ in range(2):
                self.post(f"/api/gomoku/games/{game.id}/notes/1/narrate/", {"model": "qwen3:8b"})
            self.post(f"/api/gomoku/games/{game.id}/ask/", {"question": "這手好嗎？", "at": 1, "model": "qwen3:8b"})
        # 第二次直接回傳存好的說明，沒有用到模型
        self.assertEqual([(r.kind, r.game, r.index) for r in ModelRun.objects.order_by("id")],
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


class RulesPanelTests(TestCase):
    def test_every_game_page_explains_its_rules(self):
        # 使用者要求每個遊戲都要有規則說明；新增遊戲時忘了寫規則，這個測試要擋下來
        from arena import games
        for key, module in games.GAMES.items():
            self.assertTrue(module.rules, key)
            html = self.client.get(f"/{key}/").content.decode()
            for section in module.rules:
                self.assertIn(f"<h3>{section['title']}</h3>", html, key)


class HomePageTests(TestCase):
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
        # 私人儲存庫只管這些，不能把程式、套件或引擎裡的說明檔收進去
        import shutil
        import subprocess
        from tools import git_menu
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("CLAUDE.md", "docs/交接.md", ".claude/skills/x/SKILL.md", "private/settings.json",
                         "arena/views.py", ".venv/Lib/site-packages/pkg/README.md", "tools/stockfish/README.md"):
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

            self.assertEqual(untracked(), [".gitignore", "arena/views.py"])
            self.assertEqual(untracked(f"--git-dir={root / 'private' / '.git'}", f"--work-tree={root}"),
                             [".claude/skills/x/SKILL.md", "CLAUDE.md", "docs/交接.md", "private/settings.json"])

    def test_a_commit_that_adds_documents_to_the_public_repository_is_refused(self):
        # 排除清單還沒寫好的電腦、或用 git add -f 硬加的，存檔前也要擋下
        from tools import git_menu
        self.assertEqual(git_menu.private_files(["docs/交接.md", "arena/views.py", "README.MD", "private", "private/x.json"]),
                         ["docs/交接.md", "README.MD", "private", "private/x.json"])


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
        for name in ("tools/git_menu.py", "config/local.py", "config/__init__.py"):
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
