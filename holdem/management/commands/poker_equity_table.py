import json
import random

from django.core.management.base import BaseCommand

from holdem import cards
from holdem.module import EQUITY_FILE


class Command(BaseCommand):
    help = "計算 169 種起手牌對上一手隨機牌、打到攤牌的勝率與平手率，寫入 holdem/preflop_equity.json（撲克的勝率表）"

    def add_arguments(self, parser):
        parser.add_argument("--sims", type=int, default=20000, help="每種起手牌模擬幾次（預設 20000，誤差約 ±0.4%）")

    def handle(self, *args, sims, **options):
        rng = random.Random(2026)  # 固定種子，重跑結果一樣
        rows = []
        for key, hole in cards.starting_hands():
            win, tie = cards.win_tie_vs_random(hole, sims, rng)
            rows.append({"hand": key, "win": round(win, 4), "tie": round(tie, 4)})
            self.stdout.write(f"{key}: 勝率 {win:.1%}，平手 {tie:.1%}")
        EQUITY_FILE.write_text(json.dumps({"sims": sims, "rows": rows}, ensure_ascii=False, indent=1) + "\n",
                               encoding="utf-8")
        self.stdout.write(f"已寫入 {EQUITY_FILE}（{len(rows)} 種起手牌）")
