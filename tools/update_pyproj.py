"""依專案裡實際的檔案，重新產生 test1.pyproj 的檔案清單（Visual Studio 只顯示清單裡的檔案）。

用法：.venv/Scripts/python.exe tools/update_pyproj.py
新增或刪除檔案後執行一次即可。不列入：.venv、tools、.claude、private、快取、資料庫，
以及不進儲存庫的每台電腦自己的檔案（其他電腦沒有，Visual Studio 會顯示找不到）。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJ = ROOT / "test1.pyproj"
SKIP_DIRS = {".venv", "tools", ".claude", "__pycache__", "obj", ".vs", "KataGoData", "private", ".git"}
CONTENT_SUFFIXES = {".md", ".json", ".js", ".css", ".html", ".bat", ".txt", ".svg"}
SKIP_FILES = {"db.sqlite3", "local.json", "sqlserver.json", "工作紀錄.md"}


def project_files():
    for path in sorted(ROOT.rglob("*")):
        rel = path.relative_to(ROOT)
        if path.is_dir() or set(rel.parts) & SKIP_DIRS or path.name in SKIP_FILES or rel.parts[0].endswith(".pyproj"):
            continue
        yield rel


def item_group(tag, paths):
    lines = "\n".join(f'    <{tag} Include="{p}" />' for p in paths)
    return f"<ItemGroup>\n{lines}\n  </ItemGroup>"


def main():
    files = list(project_files())
    compile_ = [str(p).replace("/", "\\") for p in files if p.suffix == ".py"]
    content = [str(p).replace("/", "\\") for p in files if p.suffix in CONTENT_SUFFIXES]
    folders = sorted({str(parent).replace("/", "\\") + "\\" for p in files for parent in p.parents if str(parent) != "."})
    text = PYPROJ.read_text(encoding="utf-8")
    for tag, paths in (("Compile", compile_), ("Content", content), ("Folder", folders)):
        pattern = re.compile(rf"<ItemGroup>\s*<{tag} [^\n]*\n(?:\s*<{tag} [^\n]*\n)*\s*</ItemGroup>")
        if not pattern.search(text):
            raise SystemExit(f"找不到 {tag} 的 ItemGroup，請手動檢查 test1.pyproj")
        block = item_group(tag, paths)
        text = pattern.sub(lambda _: block, text, count=1)  # 用函式代入，路徑裡的反斜線才不會被當成跳脫字元
    PYPROJ.write_text(text, encoding="utf-8")
    print(f"已更新 test1.pyproj：{len(compile_)} 個 .py、{len(content)} 個內容檔、{len(folders)} 個資料夾")


if __name__ == "__main__":
    main()
