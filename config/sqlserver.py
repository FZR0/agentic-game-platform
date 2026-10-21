"""每台電腦自己的 SQL Server 設定（2026-10-19 使用者決定：資料庫改用每台電腦本機的 SQL Server）。

專案資料夾會在不同電腦之間搬，所以設定依電腦名稱（platform.node()）分開存在 config/sqlserver.json：
    {"電腦名稱": {"mode": "auto" 或 "manual", "host": "localhost\\SQLEXPRESS"}}
mode 由管理者在那台電腦第一次啟動時選（tools/setup_sqlserver.py）：auto 每次啟動重新挑第一個連得上的執行個體並寫回 host，
manual 一律用管理者輸入的 host。用 Windows 驗證登入，所以檔案裡沒有帳號密碼。
"""
import json
import os
import platform
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

from config import local

# 不進儲存庫（每台電腦自己的選擇），git worktree 裡沒有，所以放在主要的專案資料夾
CONFIG_FILE = local.MAIN_DIR / "config" / "sqlserver.json"
DATABASE = "game_platform"  # 每台電腦的 SQL Server 上，這個專案用的資料庫名稱
MACHINE = platform.node()
# ODBC Driver 18 預設要求加密，本機 SQL Server 的自簽憑證不被信任就會連不上；這裡只連本機，所以信任它
CONNECTION_OPTIONS = "TrustServerCertificate=yes"


def load():
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def database():
    """settings.DATABASES["default"]。這台電腦還沒設定時直接停下來，不改用別的資料庫（使用者 2026-10-19 決定）。"""
    entry = load().get(MACHINE)
    if not entry or not entry.get("host"):
        raise ImproperlyConfigured(f"這台電腦（{MACHINE}）還沒設定 SQL Server：請先執行 啟動遊戲平台.bat，"
                                   "或 .venv/Scripts/python.exe tools/setup_sqlserver.py。")
    # 測試資料庫名稱帶程序編號：同一個資料夾常有好幾個工作階段，同時跑測試時共用一個測試資料庫會互相刪掉
    return {"ENGINE": "mssql", "NAME": DATABASE, "HOST": entry["host"],
            "OPTIONS": {"extra_params": CONNECTION_OPTIONS}, "TEST": {"NAME": f"test_{DATABASE}_{os.getpid()}"}}
