"""把改用 SQL Server 之前存在 db.sqlite3 的資料搬進 SQL Server（啟動遊戲平台.bat 每次啟動時呼叫；2026-10-19 使用者決定）。

搬 arena 的每一張表（對局、AI 思路、模型使用紀錄；以後加的表也會跟著搬），保留編號與時間：
復盤靠編號找對局，AiNote、ModelRun 靠編號對到 Game。只在 SQL Server 那邊 arena 的表全都是空的時候搬，
所以只會搬一次；已經有資料時，同一個編號會蓋掉原本那一筆，兩台電腦的資料也會混在一起，所以不搬。
db.sqlite3 的資料不刪（只會先補上新版的資料表），留著當備份。
"""
from django.apps import apps
from django.core import serializers
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import transaction

SOURCE = "sqlite"  # settings.DATABASES 裡舊資料庫的名稱


class Command(BaseCommand):
    help = "SQL Server 還沒有任何資料時，把 db.sqlite3 的對局與紀錄搬過去（保留編號與時間）"

    def handle(self, *args, **options):
        models = list(apps.get_app_config("arena").get_models())  # 定義順序：被參照的 Game 在前面
        if any(model.objects.exists() for model in models):
            return
        call_command("migrate", database=SOURCE, verbosity=0)  # 舊檔案的資料表可能比程式舊
        rows = [row for model in models for row in model.objects.using(SOURCE).all()]
        if not rows:
            return
        with transaction.atomic():
            for obj in serializers.deserialize("json", serializers.serialize("json", rows)):
                obj.save()  # 和 loaddata 一樣不經過 auto_now：編號與時間照舊
        games = sum(1 for row in rows if row._meta.model_name == "game")
        self.stdout.write(f"已把 db.sqlite3 的 {games} 盤對局（共 {len(rows)} 筆資料）搬進 SQL Server；db.sqlite3 留著當備份。")
