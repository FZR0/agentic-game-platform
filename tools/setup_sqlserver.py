"""設定這台電腦用哪一個 SQL Server，並建立遊戲平台的資料庫（啟動遊戲平台.bat 呼叫；2026-10-19 使用者決定）。

一台電腦第一次執行時，請管理者選：
  自動：每次啟動依序用第一個連得上的執行個體（預設執行個體 → SQLEXPRESS → 其他具名執行個體 → LocalDB）；
  手動：輸入伺服器名稱，之後一律用它。
選擇依電腦名稱存在 config/sqlserver.json（見 config/sqlserver.py）；要重選就刪掉這台電腦那一筆，下次啟動會再問。
沒有 ODBC 驅動、找不到 SQL Server 或連不上時停下來說明，不改用別的資料庫（使用者決定）。用 Windows 驗證登入。
ODBC 的錯誤訊息在中文 Windows 上會變成亂碼，所以只印 SQLSTATE 代碼，說明用自己的文字。
"""
import json
import sys
import winreg
from pathlib import Path

import pyodbc

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import sqlserver  # noqa: E402

DRIVERS = ("ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server")  # mssql-django 沒指定驅動時也是這個順序
INSTANCES_KEY = r"SOFTWARE\Microsoft\Microsoft SQL Server\Instance Names\SQL"
LOCALDB_KEY = r"SOFTWARE\Microsoft\Microsoft SQL Server Local DB\Installed Versions"
LOCALDB = r"(localdb)\MSSQLLocalDB"


def installed_instances():
    """登錄檔裡這台電腦裝了的 SQL Server 執行個體名稱（預設執行個體叫 MSSQLSERVER）。"""
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, INSTANCES_KEY) as key:
            return [winreg.EnumValue(key, i)[0] for i in range(winreg.QueryInfoKey(key)[1])]
    except OSError:
        return []


def has_localdb():
    try:
        winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, LOCALDB_KEY).Close()
        return True
    except OSError:
        return False


def candidates(instances, localdb):
    """自動模式依序嘗試的伺服器：預設執行個體、SQLEXPRESS、其他具名執行個體（依名稱）、LocalDB。"""
    hosts = ["localhost"] if "MSSQLSERVER" in instances else []
    if "SQLEXPRESS" in instances:
        hosts.append(r"localhost\SQLEXPRESS")
    hosts += [rf"localhost\{name}" for name in sorted(instances) if name not in ("MSSQLSERVER", "SQLEXPRESS")]
    if localdb:
        hosts.append(LOCALDB)
    return hosts


def ask(prompt):
    try:
        return input(prompt).strip()
    except EOFError:  # 沒有人可以回答（例如輸入被導向空檔案）時當成沒有選
        return ""


def choose(hosts):
    print(f"這台電腦（{sqlserver.MACHINE}）第一次使用，請選擇遊戲平台要用哪一個 SQL Server：")
    print("  找到的 SQL Server：" + ("、".join(hosts) if hosts else "沒有"))
    print("  1. 自動：每次啟動依上面的順序，用第一個連得上的")
    print("  2. 手動：輸入伺服器名稱，之後一律用它")
    answer = ask("請輸入 1 或 2：")
    if answer == "1":
        return {"mode": "auto"}
    if answer == "2":
        host = ask(r"伺服器名稱（例如 localhost\SQLEXPRESS）：")
        if host:
            return {"mode": "manual", "host": host}
    return None


def connect(driver, host):
    return pyodbc.connect(f"DRIVER={{{driver}}};SERVER={host};DATABASE=master;Trusted_Connection=yes;"
                          f"{sqlserver.CONNECTION_OPTIONS}", timeout=10, autocommit=True)


def save(entry):
    data = sqlserver.load()
    if data.get(sqlserver.MACHINE) != entry:
        data[sqlserver.MACHINE] = entry
        sqlserver.CONFIG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    driver = next((d for d in DRIVERS if d in pyodbc.drivers()), None)
    if not driver:
        print("這台電腦沒有 SQL Server 的 ODBC 驅動程式。請先安裝 Microsoft ODBC Driver 18 for SQL Server"
              "（https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server），再執行一次啟動檔。")
        return 1
    entry = sqlserver.load().get(sqlserver.MACHINE)
    if not entry:
        entry = choose(candidates(installed_instances(), has_localdb()))
        if not entry:
            print("沒有選擇，遊戲平台沒有啟動。再執行一次啟動檔就會再問。")
            return 1
    hosts = candidates(installed_instances(), has_localdb()) if entry["mode"] == "auto" else [entry["host"]]
    if not hosts:
        print("這台電腦找不到 SQL Server。請先安裝 SQL Server（Express 或 LocalDB 都可以），再執行一次啟動檔。")
        return 1
    for host in hosts:
        try:
            conn = connect(driver, host)
            break
        except pyodbc.Error as exc:
            print(f"連不上 SQL Server {host}（{exc.args[0]}）。")
    else:
        print("請確認 SQL Server 的服務有在執行、伺服器名稱沒有打錯，而且目前的 Windows 帳號可以登入。"
              "要改選別的伺服器，刪掉 config/sqlserver.json 裡這台電腦那一筆，再執行一次啟動檔。")
        return 1
    try:
        conn.execute(f"IF DB_ID(N'{sqlserver.DATABASE}') IS NULL CREATE DATABASE [{sqlserver.DATABASE}]")
    except pyodbc.Error as exc:
        print(f"在 {host} 建立資料庫 {sqlserver.DATABASE} 失敗（{exc.args[0]}）：目前的 Windows 帳號需要建立資料庫的權限。")
        return 1
    finally:
        conn.close()
    save(dict(entry, host=host))
    print(f"資料庫：SQL Server {host}（{sqlserver.DATABASE}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
