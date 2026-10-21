import csv
import statistics
from collections import Counter, defaultdict
from datetime import date, datetime, time
from itertools import groupby

from django.conf import settings
from django.core.management.base import BaseCommand

from arena import games
from arena.models import ModelRun

# (欄位, 標題, 畫面上要不要顯示)；CSV 全部都寫
COLUMNS = [
    ("kind", "用途", False), ("game", "遊戲", False), ("model", "模型", True), ("where", "伺服器（電腦）", True),
    ("count", "次數", True), ("ok", "成功", False), ("fallback", "沒完成", False), ("error", "呼叫失敗", True),
    ("rate", "成功率（%）", True), ("median", "秒數中位數", True), ("max", "最慢秒數", True),
    ("model_median", "等模型秒數中位數", True), ("calls", "平均呼叫次數", True), ("rejected", "平均被退回次數", True),
    ("ratio", "秒數倍數（基準＝1）", True), ("rate_diff", "成功率差（百分點）", True),
    ("first", "第一筆", False), ("last", "最後一筆", False),
]
NOTES = [
    "成功率＝成功 ÷（成功＋沒完成）。呼叫失敗（連不上、逾時、伺服器暫停呼叫）是伺服器的問題，不算進成功率，也不算進秒數。",
    "Agent 的秒數含引擎工具的時間（在「電腦」那台算）；「等模型」只算等 Ollama 回應的時間。",
    "伺服器「不明」的是加紀錄表（2026-10-19）以前從 AI 思路匯入的舊紀錄：只有 Agent 自己下成功的手，"
    "沒有代下的手，所以不算成功率，秒數也偏快；不要和新紀錄直接比。",
    "測速用固定局面，實戰的局面不同，所以分開列。",
    "倍數與成功率差只和同一用途、同一遊戲、同一伺服器與電腦裡的基準模型比；那一組沒有基準模型時留空。",
]


def _median(values):
    values = [v for v in values if v is not None]
    return round(statistics.median(values), 1) if values else None


def _mean(values):
    values = [v for v in values if v is not None]
    return round(statistics.mean(values), 1) if values else None


def _local(moment):
    return moment.astimezone().strftime("%Y-%m-%d %H:%M")  # 資料庫存 UTC，顯示成這台電腦的時間


def summarize(runs, baseline):
    """依（用途、遊戲、模型、伺服器、電腦）分組統計，再和同一組條件下的基準模型比較。"""
    groups = defaultdict(list)
    for r in runs:
        groups[(r.kind, r.game_type, r.model, r.server, r.machine)].append(r)
    model_order = [c["name"] for c in settings.OLLAMA_MODELS]  # 依設定的順序（智慧程度由高到低）
    rows = []
    for (kind, game_type, model, server, machine), rs in groups.items():
        counts = Counter(r.outcome for r in rs)
        answered = [r for r in rs if r.outcome != "error"]
        decided = counts["ok"] + counts["fallback"]
        rows.append({
            "kind": kind, "game_type": game_type, "model": model, "server": server, "machine": machine,
            "count": len(rs), "ok": counts["ok"], "fallback": counts["fallback"], "error": counts["error"],
            # 舊紀錄只有成功的手，算出來一定是 100%，不能當成功率
            "rate": round(100 * counts["ok"] / decided) if decided and server else None,
            "median": _median(r.seconds for r in answered),
            "max": round(max(r.seconds for r in answered), 1) if answered else None,
            "model_median": _median(r.model_seconds for r in answered),
            "calls": _mean(r.calls for r in answered),
            "rejected": _mean(r.rejected for r in answered),
            "first": min(r.created_at for r in rs), "last": max(r.created_at for r in rs),
            "sort": (list(ModelRun.KINDS).index(kind) if kind in ModelRun.KINDS else len(ModelRun.KINDS), game_type,
                     model_order.index(model) if model in model_order else len(model_order), model, server, machine),
        })
    rows.sort(key=lambda row: row["sort"])
    base = {(b["kind"], b["game_type"], b["server"], b["machine"]): b for b in rows if b["model"] == baseline}
    for row in rows:
        b = base.get((row["kind"], row["game_type"], row["server"], row["machine"]))
        row["ratio"] = round(row["median"] / b["median"], 1) if b and row["median"] and b["median"] else None
        row["rate_diff"] = row["rate"] - b["rate"] if b and row["rate"] is not None and b["rate"] is not None else None
    return rows


def _cells(row):
    """一列的顯示內容（給畫面與 CSV）。"""
    try:
        game = games.get(row["game_type"]).name
    except KeyError:
        game = row["game_type"]
    where = f"{row['server']}（{row['machine']}）" if row["server"] else "不明（舊紀錄）"
    return {**row, "kind": ModelRun.KINDS.get(row["kind"], row["kind"]), "game": game, "where": where,
            "first": _local(row["first"]), "last": _local(row["last"])}


class Command(BaseCommand):
    help = "整理模型使用紀錄（ModelRun）：依用途、遊戲、模型、伺服器分組，比較秒數與成功率"

    def add_arguments(self, parser):
        parser.add_argument("--game", choices=sorted(games.GAMES), help="只看某個遊戲")
        parser.add_argument("--kind", choices=list(ModelRun.KINDS), help="只看某種用途（agent 是實戰下棋）")
        parser.add_argument("--since", type=date.fromisoformat, help="只看這一天（YYYY-MM-DD，這台電腦的時間）以後的紀錄")
        parser.add_argument("--baseline", default=settings.OLLAMA_MODEL, help="比較的基準模型（預設是目前的預設模型）")
        parser.add_argument("--csv", dest="csv_path", help="另外把整理結果寫成 CSV（UTF-8 含 BOM，Excel 可以直接開）")

    def handle(self, *args, game, kind, since, baseline, csv_path, **options):
        runs = ModelRun.objects.all()
        if game:
            runs = runs.filter(game_type=game)
        if kind:
            runs = runs.filter(kind=kind)
        if since:
            runs = runs.filter(created_at__gte=datetime.combine(since, time.min).astimezone())
        runs = list(runs)
        if not runs:
            self.stdout.write("沒有符合條件的模型使用紀錄。")
            return
        rows = [_cells(row) for row in summarize(runs, baseline)]
        self.stdout.write(f"模型使用紀錄：{len(runs)} 筆，基準模型 {baseline}")
        shown = [(key, title) for key, title, show in COLUMNS if show]
        for (kind_name, game_name), section in groupby(rows, key=lambda row: (row["kind"], row["game"])):
            self.stdout.write(f"\n{kind_name}／{game_name}")
            self.stdout.write("| " + " | ".join(title for _, title in shown) + " |")
            self.stdout.write("|" + "---|" * len(shown))
            for row in section:
                self.stdout.write("| " + " | ".join("—" if row[key] is None else str(row[key]) for key, _ in shown) + " |")
        self.stdout.write("")
        for note in NOTES:
            self.stdout.write(f"※ {note}")
        if csv_path:
            self._write_csv(csv_path, rows)
            self.stdout.write(f"已寫入 {csv_path}")

    def _write_csv(self, path, rows):
        with open(path, "w", encoding="utf-8-sig", newline="") as f:  # 沒有 BOM 時 Excel 會把中文讀成亂碼
            writer = csv.writer(f)
            writer.writerow(title for _, title, _ in COLUMNS)
            for row in rows:
                writer.writerow("" if row[key] is None else row[key] for key, _, _ in COLUMNS)
