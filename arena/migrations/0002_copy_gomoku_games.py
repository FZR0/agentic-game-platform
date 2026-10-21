"""把五子棋原本的對局與 AI 思路搬進共用的 arena 資料表，保留編號與時間。"""
from django.db import migrations

COLS = "ABCDEFGHIJKLMNO"


def copy_games(apps, schema_editor):
    OldGame = apps.get_model("gomoku", "Game")
    OldNote = apps.get_model("gomoku", "AiNote")
    Game = apps.get_model("arena", "Game")
    AiNote = apps.get_model("arena", "AiNote")
    # 有兩個資料庫（SQL Server 和搬資料用的舊 db.sqlite3，2026-10-19）：只讀寫正在更新的那一個，不能讀到預設的資料庫
    db = schema_editor.connection.alias
    for old in OldGame.objects.using(db).all():
        Game.objects.using(db).create(id=old.id, game_type="gomoku", human_seat=old.human_color, settings={},
                                      actions=[f"{COLS[x]}{y + 1}" for x, y in old.moves],
                                      winner=old.winner, draw=old.draw)
        # auto_now 欄位在建立時會被改成現在時間，用 update 補回原本的時間
        Game.objects.using(db).filter(id=old.id).update(created_at=old.created_at, updated_at=old.updated_at)
    for note in OldNote.objects.using(db).all():
        AiNote.objects.using(db).create(game_id=note.game_id, index=note.index, seat=1 if note.index % 2 == 0 else 2,
                                        facts=note.facts, narration=note.narration)


class Migration(migrations.Migration):

    dependencies = [
        ("arena", "0001_initial"),
        ("gomoku", "0004_remove_game_level"),
    ]

    operations = [migrations.RunPython(copy_games, migrations.RunPython.noop)]
