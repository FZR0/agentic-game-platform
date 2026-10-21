"""不能公開的設定（2026-10-21：專案放上公開的 GitHub 儲存庫，使用者決定機密不進公開的儲存庫）。

值依序從這些地方找，找到就用：
1. 環境變數（例如本機測試模式設的 OLLAMA_URL）
2. private/settings.json：私人儲存庫（只有受邀成員拿得到），全組共用的值，例如研究用伺服器的位址
3. config/local.json：這台電腦自己的值，不進任何儲存庫，例如 Django 的 SECRET_KEY（第一次執行時自動產生）
私人儲存庫排在本機前面：成員還沒受邀時在本機輸入過位址，受邀後要改用全組共用的值。

git worktree 的資料夾裡沒有不進儲存庫的檔案（private/、config/local.json、遊戲引擎、SQL Server 的選擇），
所以這些一律到主要的專案資料夾找（MAIN_DIR）。
"""
import json
import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main_checkout(root):
    """主要的專案資料夾。git worktree 裡 .git 是一個檔案，內容是「gitdir: <主要資料夾>/.git/worktrees/<名稱>」。"""
    git = root / ".git"
    if git.is_file():
        gitdir = root / git.read_text(encoding="utf-8").split(":", 1)[1].strip()
        return gitdir.resolve().parent.parent.parent
    return root


MAIN_DIR = main_checkout(ROOT)
PRIVATE_FILE = MAIN_DIR / "private" / "settings.json"
LOCAL_FILE = MAIN_DIR / "config" / "local.json"


def read(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def get(name, default=None):
    if name in os.environ:
        return os.environ[name]
    for path in (PRIVATE_FILE, LOCAL_FILE):
        value = read(path).get(name)
        if value:
            return value
    return default


def save(name, value):
    """存進這台電腦的 config/local.json。"""
    data = read(LOCAL_FILE)
    data[name] = value
    LOCAL_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def secret_key():
    """每台電腦自己的 SECRET_KEY：第一次執行時產生並存起來，之後一直用同一個。"""
    key = get("SECRET_KEY")
    if not key:
        key = secrets.token_urlsafe(50)
        save("SECRET_KEY", key)
    return key
