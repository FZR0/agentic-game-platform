import json
import statistics
import time
import urllib.request
from datetime import datetime

from django.conf import settings
from django.core.management.base import BaseCommand

from arena import agent, games, narrator
from arena.models import ModelRun


def _unload(name):
    """把指定模型從 Ollama 記憶體卸載，成功時回傳 True。伺服器是共用的，只動要測的模型，不卸載別人載入的模型。
    伺服器忙著沒回應時回傳 False，讓測速繼續，不要整批中斷。"""
    try:
        with urllib.request.urlopen(f"{settings.OLLAMA_URL}/api/ps", timeout=10) as resp:
            loaded = [m["name"] for m in json.load(resp)["models"]]
        if name in loaded:
            body = json.dumps({"model": name, "keep_alive": 0,
                               "options": {"num_ctx": settings.OLLAMA_NUM_CTX}}).encode("utf-8")
            req = urllib.request.Request(f"{settings.OLLAMA_URL}/api/generate", data=body,
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=60).close()
        return True
    except (OSError, ValueError, KeyError):
        return False


def _unreachable(result):
    """這一手是不是根本呼叫不到模型（逾時、連線失敗）。這不是模型的表現，不能當成測量結果。"""
    return result["fallback"] and agent.UNREACHABLE in result["facts"]["points"][0]["text"]


class Command(BaseCommand):
    help = "測量每個模型在某個遊戲擔任 Agent 時下一手所需的時間與成功率，結果寫入目前 Ollama 伺服器的測速檔"

    def add_arguments(self, parser):
        parser.add_argument("--game", default="gomoku", choices=sorted(games.GAMES), help="要測的遊戲（預設 gomoku）")
        parser.add_argument("--rounds", type=int, default=2, help="每個局面重複幾次（預設 2）")
        parser.add_argument("--model", action="append", help="只測指定模型，可重複指定")
        parser.add_argument("--steps", type=int, help="Agent 每手查證次數（預設用該遊戲的預設值）")

    def handle(self, *args, game, rounds, model, steps, **options):
        module = games.get(game)
        installed = narrator.installed_models()
        if installed is None:
            self.stderr.write("Ollama 沒有啟動，無法測速。")
            return
        defaults = module.clean_settings({})
        samples = [module.replay(defaults, actions) for actions in module.benchmark_samples]
        engine_moves = [module.engine_move(s)["action"] for s in samples]
        results = narrator.load_benchmarks()
        game_results = results.setdefault(game, {})
        path = narrator.benchmark_file()
        for config in settings.OLLAMA_MODELS:
            name = config["name"]
            if model and name not in model:
                continue
            if not config.get("agent", True):
                self.stdout.write(f"略過 {name}：不適合擔任 Agent")
                continue
            if name not in installed:
                self.stdout.write(f"略過 {name}：尚未安裝")
                continue
            if not _unload(name):  # 第一次呼叫才是真正的冷啟動
                self.stdout.write(f"    無法確認 {name} 已卸載，冷啟動秒數可能偏低")
            start = time.perf_counter()
            r = agent.agent_move(module, samples[0], module.benchmark_samples[0], name, steps)
            cold = time.perf_counter() - start
            if _unreachable(r):
                return self._stop(name, r)

            times, ok, agree, reasons, problems = [], 0, 0, 0, []
            for _ in range(rounds):
                for sample, actions, expected in zip(samples, module.benchmark_samples, engine_moves):
                    start = time.perf_counter()
                    r = agent.agent_move(module, sample, actions, name, steps)
                    times.append(time.perf_counter() - start)
                    if r["run"]:  # 每一手都存進 ModelRun：測速檔只留最新一次，紀錄表保留每次重測
                        ModelRun.record("benchmark", game, r["run"])
                    if _unreachable(r):
                        return self._stop(name, r)
                    agree += r["action"] == expected
                    if r["fallback"]:
                        problems.append(r["facts"]["points"][0]["text"])
                    else:
                        ok += 1
                        reasons += bool(r["narration"] and r["narration"]["key"])
            if not _unload(name):  # 測完就卸載，不佔用共用伺服器的記憶體
                self.stdout.write(f"    無法卸載 {name}，它會在閒置 30 分鐘後自動卸載")
            game_results[name] = {
                "seconds": round(statistics.median(times), 1),
                "max_seconds": round(max(times), 1),
                "cold_seconds": round(cold, 1),
                "agent_success_rate": round(ok / len(times), 2),
                "agree_with_engine_rate": round(agree / len(times), 2),
                "reason_pass_rate": round(reasons / ok, 2) if ok else 0,
                "steps": steps or module.default_steps,
                "runs": len(times),
                "measured_at": datetime.now().isoformat(timespec="seconds"),
            }
            # 每測完一個模型就存檔：整批要跑很久，中途出錯也不會丟掉已經測完的結果
            path.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            r = game_results[name]
            self.stdout.write(f"{game} / {name}: 每手約 {r['seconds']} 秒（最慢 {r['max_seconds']}），冷啟動 "
                              f"{r['cold_seconds']} 秒，成功 {ok}/{len(times)}，與引擎相同 {agree}/{len(times)}，"
                              f"理由通過檢查 {reasons}/{ok}")
            for problem in sorted(set(problems)):
                self.stdout.write(f"    代下原因：{problem}")
        self.stdout.write(f"已寫入 {path}")

    def _stop(self, name, result):
        # 2026-10-11 伺服器卡住後每次呼叫都等滿逾時，曾把「每手 120 秒、成功 0%」存成 4 個模型的結果
        self.stderr.write(f"停止測速：{name} 呼叫不到模型（{result['facts']['points'][0]['text']}）。"
                          f"伺服器可能卡住了，或這個模型一次回應超過 {settings.OLLAMA_TIMEOUT} 秒。"
                          "這個模型的結果不存；前面測完的模型都已存檔，之後可以用 --model 單獨重測。")
