"""平台的操作：開局、下棋、復盤、提問、各頁面的資料。

這些是 AI 助理（arena/orchestrator.py）的工具，也是它沒完成時的退回做法；views 只負責解析 HTTP 與輸出。
從 views.py 搬過來時行為不變（2026-10-28）；唯一的差別是開局與玩家下棋拆成「存檔」和「AI 行動」兩步，
讓 AI 助理一步一步呼叫，所以會多存一次檔。
"""
import re
import secrets
import time
from collections import Counter
from datetime import timedelta
from pathlib import Path

from django.contrib.staticfiles import finders
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q
from django.urls import reverse
from django.utils import timezone

from . import agent, games, narrator
from .base import ESTIMATE, RULE
from .models import AiNote, ChatMessage, Game, GamePlayer, GameSpectator, ModelRun

MOVE_REF_RE = re.compile(r"第\s*(\d{1,3})\s*[手步](?!牌)")  # 撲克的「第 3 手牌」不是第 3 步


def settings_of(game, seat=None):
    """交給遊戲模組的設定：存檔的設定加上只在伺服器的 secret（撲克的洗牌種子）。
    seat 是從哪個座位看（viewer）：撲克的提問只用這個座位看得到的牌（真人對戰時不一定是 1 號位）。"""
    settings = {**game.settings, "secret": game.secret}
    if seat is not None:
        settings["viewer"] = seat
    if game.settings.get("table") and game.pk:  # 撲克開放牌桌：畫面上的真人寫帳號名稱（成員代號 → 名稱）
        settings["names"] = {p.seat: p.user.username for p in game.players.select_related("user") if p.user}
    return settings


def _public(module, state, moves):
    """AI 思路送到網頁前先過濾：撲克這手牌還沒結束時遮住含底牌的思路。"""
    return module.public_notes(state, moves)


def _set_outcome(module, game, state):
    result = module.outcome(state)
    if result:
        game.winner, game.draw = result


def _ai_turns(module, game, state, model, steps, seat):
    """輪到 AI 的座位時一直由 Agent 行動，直到輪到任何一位真人或對局結束。回傳 (局面, AI 行動清單)；
    畫面（frame）是 seat 看到的樣子。"""
    moves = []
    humans = game.human_seats()
    while not module.outcome(state) and module.to_move(state) not in humans:
        actor = module.to_move(state)
        if actor is None:  # 撲克開放牌桌一手結束：等 table_advance 補人、發牌
            break  # 不能叫 seat：seat 是看畫面的真人，畫面要用他的座位（否則撲克會露出 AI 的底牌）
        result = agent.agent_move(module, state, game.actions, model, steps)
        state = module.apply(state, result["action"])
        game.actions.append(result["action"])
        moves.append({"index": len(game.actions) - 1, "seat": actor, "action": result["action"],
                      "facts": result["facts"], "narration": result["narration"], "fallback": result["fallback"],
                      "run": result["run"], "frame": module.frame(state, seat)})
    _set_outcome(module, game, state)
    return state, moves


def _save_and_hide(module, game, state, moves):
    """存檔後回傳可以送到網頁的 AI 行動（思路已過濾）。"""
    game.save()
    for m in moves:
        AiNote.objects.create(game=game, index=m["index"], seat=m["seat"], facts=m["facts"], narration=m["narration"])
        run = m.pop("run")
        if run:  # 不需要模型的步驟（例如第一手）沒有紀錄
            ModelRun.record("agent", module.key, run, game, m["index"])
    return _public(module, state, moves)


def narrate(module, facts, model, game=None, index=None):
    """請語言模型說明，同時記下這次用模型的秒數與結果（ModelRun）。復盤的說明不管 model 是什麼，一律用最聰明的模型。"""
    if facts["kind"] in narrator.REVIEW_KINDS:
        model = narrator.review_model()
    start = time.perf_counter()
    result = narrator.narrate(facts, model, module)
    seconds = round(time.perf_counter() - start, 2)
    if result["source"] == "llm":
        outcome = "ok"
    else:
        outcome = "error" if result["note"].startswith(narrator.UNAVAILABLE) else "fallback"
    ModelRun.record(facts["kind"], module.key, {"model": model, "seconds": seconds, "model_seconds": seconds,
                                                "calls": 1, "outcome": outcome, "problem": result.get("note", "")},
                    game, index)
    return result


def _frames(module, game, seat):
    state = module.new_state(settings_of(game))
    frames = [module.frame(state, seat)]
    for action in game.actions:
        state = module.apply(state, action)
        frames.append(module.frame(state, seat))
    return frames


# ---- 頁面 ----

def _asset_version(*paths):
    """靜態檔最後修改時間，加在網址後面（?v=）。改了 JS 或 CSS 之後，瀏覽器一定會拿新版，不會沿用快取的舊檔。"""
    return int(max(Path(finders.find(p)).stat().st_mtime for p in paths))


RESULTS = {"win": "你贏", "loss": "對手贏", "draw": "和局", "ended": "不計勝負", "unfinished": "未完成"}
HOME_RESUME = 3  # 首頁「繼續上次的對局」最多幾盤
HOME_RECENT = 5  # 首頁「最近對局」最多幾盤
HISTORY_PAGE_SIZE = 20


def _site_context(nav):
    """首頁、對局紀錄、怎麼玩共用：上方導覽列目前在哪一頁，以及靜態檔版本號。"""
    icons = [g.icon for g in games.GAMES.values()]
    return {"nav": nav, "games": games.GAMES.values(), "asset_version": _asset_version("arena/style.css", *icons)}


def _result(game, user):
    if game.ended:
        return "ended"
    if game.draw:
        return "draw"
    if game.winner:
        return "win" if game.winner == game.seat_of(user) else "loss"
    return "unfinished"


def _played_games(user, **filters):
    """這位使用者看得到、而且玩家至少下過一步的對局，最近下的在前（首頁與對局紀錄都用這個）。

    打開對局頁時如果 AI 先行動（例如撲克），對局一建立就存檔；玩家一步都沒下就離開的不列出，
    否則每打開一次撲克頁就多一盤「未完成」。AI 的每一步都有一筆 AiNote，所以行動數多於 AiNote 數就是玩家下過。
    真人對戰的房間一律列出（2026-11-13）：房間是特地開的或加入的，按「開始」前一步都沒下，
    不列出的話房主或朋友一離開房間頁就回不去了（使用者回報）。房主在開始前關掉的房間不列出（closed_room）。
    """
    qs = Game.visible_to(user).filter(game_type__in=list(games.GAMES), **filters).prefetch_related("players__user")
    ai_counts = dict(AiNote.objects.filter(game__in=qs).values("game").annotate(n=Count("id")).values_list("game", "n"))
    return [g for g in qs if (g.pvp and not closed_room(g)) or (not g.pvp and len(g.actions) > ai_counts.get(g.id, 0))]


def active_rooms(user):
    """這位使用者坐在座位上、還沒結束的真人對戰房間，最近有動靜的在前（2026-11-13 使用者要求：
    「在其他頁面也要確保能夠回去」，每一頁頂端的「回到房間」用這個）。觀戰中的房間不算自己的。"""
    if not user.is_authenticated:
        return []
    mine = Q(players__user=user, players__left=False) | Q(owner=user)  # 撲克開放牌桌的房主可能沒上桌（2026-11-16）
    rooms = (Game.objects.filter(mine, mode="pvp", game_type__in=list(games.GAMES), winner__isnull=True,
                                 draw=False, ended=False).distinct().order_by("-updated_at").prefetch_related("players"))
    return [{"id": g.id, "name": games.get(g.game_type).name, "status": _room_status(g) or "對戰中",
             "url": f"{reverse('arena:play', args=[g.game_type])}?resume={g.id}"} for g in rooms]


def _room_status(game):
    """還沒開始的真人對戰房間在清單上的說明；已經開始或不是房間時是空字串。"""
    if not game.pvp or game.finished or game.started:
        return ""
    if game.waiting:
        return f"等朋友加入（房間密碼 {game.room_code}）"
    return "等房主按「開始」" if game.started_at is None else "倒數中"


def _card(game, user):
    """對局清單的一列：遊戲、時間、玩家執哪一方、對手、結果、手數，以及復盤／接著下的連結。"""
    module = games.get(game.game_type)
    result = _result(game, user)
    url = reverse("arena:play", args=[module.key])
    seat = game.seat_of(user)
    opponents = [p.user.username for p in game.players.all() if p.user and p.user_id != user.id] if game.pvp else []
    room = _room_status(game)
    return {"id": game.id, "module": module, "when": game.updated_at, "steps": len(game.actions),
            # 撲克玩家固定 1 號位，寫「執 1 號位」沒有意義
            "seat": module.seat_names.get(seat, "") if len(module.seat_choices) > 1 else "",
            "pvp": game.pvp, "opponents": "、".join(opponents), "room": room,
            "result": result, "result_text": RESULTS[result], "review_url": f"{url}?review={game.id}",
            "resume_url": f"{url}?resume={game.id}" if result == "unfinished" else "",
            "resume_label": "回到房間" if room else "繼續下",
            # 房主可以關閉還沒按開始的房間（2026-11-13）
            "close_url": reverse("arena:close_room", args=[game.id])
            if room and game.owner_id == user.id and game.started_at is None else "",
            # 真人對戰不能單方面「不計勝負」，否則快輸的一方按了就能讓輸棋不算；要結束就認輸
            "end_url": reverse("arena:end_game", args=[game.id]) if result == "unfinished" and not game.pvp else ""}


def home_context(user):
    played = _played_games(user)
    unfinished = [g for g in played if not g.finished]
    stats = []
    for module in games.GAMES.values():
        count = Counter(_result(g, user) for g in played if g.game_type == module.key)
        finished = count["win"] + count["loss"] + count["draw"]
        stats.append({"module": module, "win": count["win"], "loss": count["loss"], "draw": count["draw"],
                      "finished": finished, "rate": round(100 * count["win"] / finished) if finished else None})
    return {**_site_context("home"),
            "resume": [_card(g, user) for g in unfinished[:HOME_RESUME]],
            "more_unfinished": max(0, len(unfinished) - HOME_RESUME),
            "recent": [_card(g, user) for g in played if g.finished][:HOME_RECENT],
            "stats": stats, "any_finished": any(s["finished"] for s in stats)}


def history_context(user, game_key, result, page, room=0):
    """room：房間裡某一盤的編號（房間頁的「對戰紀錄」，2026-11-16 使用者要求「改成對戰紀錄按鈕，雙方都能復盤」），
    只列出同一個房間打完的每一盤；每一盤照舊用 visible_to，所以雙方各自看得到、復盤自己坐的那一盤，
    沒坐過的人（觀戰、外人）一盤也看不到，也不能用編號探別人的房間。"""
    played = _played_games(user, **({"game_type": game_key} if game_key else {}))
    anchor = Game.watchable_by(user).filter(pk=room, mode="pvp").exclude(room_code="").first() if room else None
    if room:
        played = [g for g in played if anchor and g.room_code == anchor.room_code and g.finished]
    if result:
        played = [g for g in played if _result(g, user) == result]
    page = Paginator(played, HISTORY_PAGE_SIZE).get_page(page)
    return {**_site_context("history"), "page": page, "cards": [_card(g, user) for g in page],
            "game_key": game_key, "result": result, "results": RESULTS, "room": room}


def guide_context():
    return _site_context("guide")


def join_context():
    return _site_context("join")


def play_context(module):
    return {"module": module,
            "asset_version": _asset_version("arena/style.css", "arena/app.js", module.board_script),
            "config": {"key": module.key, "name": module.name, "seatNames": module.seat_names,
                       "coordPattern": module.coord_pattern, "defaultSteps": module.default_steps,
                       "minSteps": agent.MIN_STEPS, "maxSteps": agent.MAX_STEPS, "stepWord": module.step_word,
                       "pickHumanSeats": module.pvp_pick_seats,
                       # 房主在房間大廳換遊戲用（2026-11-15）
                       "games": [{"key": m.key, "name": m.name, "pickSeats": m.pvp_pick_seats}
                                 for m in games.GAMES.values()]}}


# ---- 對局 ----

def list_games(module, user):
    return {"games": [g.summary() for g in Game.visible_to(user).filter(game_type=module.key)[:30]]}


def start_game(module, seat, settings, user, mode="ai"):
    """建立並存下新對局（開局的人是 user），回傳 (對局, 開局畫面)。AI 先行動的部分由 run_ai_turns 處理。
    真人對戰（mode="pvp"）：其他真人座位先空著，產生房間密碼等對手加入（join_game）。"""
    settings = _table_settings(module, mode, settings)
    game = Game(game_type=module.key, owner=user, human_seat=seat, settings=settings, secret=module.new_secret(),
                mode=mode)
    if mode == "pvp":
        seats = [] if open_table(game) else _pvp_seats(module, seat, settings)
        game.room_code = Game.new_room_code()
    state = module.new_state(settings_of(game))
    game.save()
    if open_table(game):  # 牌桌一開始是空的（2026-11-16），開房的人先在觀戰席，自己按「上桌」
        GameSpectator.objects.create(game=game, user=user)
        return game, module.frame(state, None)
    if mode == "pvp":
        _seat_players(game, seats, seat, user)
    return game, module.frame(state, seat)


def _pvp_seats(module, seat, settings):
    seats = module.pvp_seats(settings)
    if seat not in seats or len(seats) < 2:
        raise ValueError("真人對戰至少要有兩個真人座位，而且要包含你的座位")
    return seats


def _seat_players(game, seats, seat, user):
    """真人對戰的座位：開房的人坐 seat，其他真人座位空著等人加入。"""
    GamePlayer.objects.bulk_create([GamePlayer(game=game, seat=s, user=user if s == seat else None,
                                               joined_at=timezone.now() if s == seat else None) for s in seats])


def change_room(module, game, user, seat, settings):
    """房主在房間裡改設定或換遊戲（2026-11-08 先開房、等人時能改；2026-11-15 使用者要求「房主可以自由選擇」：
    朋友坐下後也能改，也能換別的遊戲）。module 是改完的遊戲（換遊戲時和 game.game_type 不同）。
    同一個房間（編號、房間密碼不變），設定、座位、撲克的洗牌種子換成新的，回傳 (對局, 開局畫面)。
    座位照新的設定重排：房主坐 seat，其他已經坐下的人依原本的座位順序坐進剩下的真人座位，坐不下的改成觀戰。
    只在按「開始」之前（_lobby）。"""
    if not game.pvp or game.owner_id != user.id:
        raise ValueError("只有房主能改房間設定")
    settings = _table_settings(module, "pvp", settings)
    table = bool(settings.get("table"))
    seats = [] if table else _pvp_seats(module, seat, settings)
    with transaction.atomic():
        game = Game.objects.select_for_update().get(pk=game.pk)
        _lobby(game)
        if table:  # 撲克開放牌桌（2026-11-16）：同樣是撲克時留下還坐得下的人，換成撲克時牌桌是空的
            game = _change_table(module, game, settings)
            seat = game.seat_of(user)  # 房主的成員代號（坐在桌上）或 None（在觀戰席）
            return game, module.frame(module.replay(settings_of(game), game.actions), seat)
        friends = [p.user for p in game.players.exclude(user=user).filter(user__isnull=False).select_related("user")]
        game.players.all().delete()
        GameSpectator.objects.filter(game=game, user=user).delete()  # 從撲克的觀戰席換過來的房主
        game.game_type, game.human_seat, game.settings, game.secret = module.key, seat, settings, module.new_secret()
        game.actions, game.table = [], {}  # 撲克牌桌的入座紀錄不帶到別的遊戲
        game.save()
        _seat_players(game, seats, seat, user)
        free = list(game.players.filter(user__isnull=True).order_by("seat"))
        for place, friend in zip(free, friends):
            place.user, place.joined_at = friend, timezone.now()
            place.save()
        for friend in friends[len(free):]:
            GameSpectator.objects.get_or_create(game=game, user=friend)
    return game, module.frame(module.new_state(settings_of(game)), seat)


def _change_table(module, game, settings):
    """房間換成撲克開放牌桌或改牌桌設定（在 change_room 的鎖裡）：原本就是牌桌時，座位號碼還在新人數內的照樣坐著
    （真人、AI 都是），其他人改成觀戰；從別的遊戲換過來時牌桌是空的，房間裡的人都到觀戰席。"""
    keep = []
    if game.settings.get("table") and game.game_type == module.key:
        old = module.replay(settings_of(game), game.actions)
        keep = [f"sit {s} {w}" for s, w in sorted(old.occupant.items()) if s <= settings["players"]]
    seated = {int(a.split()[2]) for a in keep if a.split()[2].isdigit()}
    for p in game.players.all():
        if p.user_id and p.seat not in seated:
            GameSpectator.objects.get_or_create(game=game, user_id=p.user_id)
    game.players.exclude(seat__in=seated).delete()
    game.game_type, game.settings, game.secret = module.key, settings, module.new_secret()
    game.actions, game.table = keep, {}
    game.save()
    return game


# ---- 房間大廳：打完回到房間、換座位、請人離開、轉讓房主（使用者要求，2026-11-15）----
# 房間＝房間密碼相同的一串對局。每一盤打完（有勝負或和局）就用同一組密碼、同樣的座位與觀戰者開下一盤，
# 還沒按「開始」的那一盤就是房間大廳：可以換座位、改設定、換遊戲、請人離開、轉讓房主、離開房間。
# 每一盤各自是一筆對局，戰績、復盤照舊一盤一盤算；聊天跨盤保留（chat_messages）。

def _latest_round(code):
    """這組房間密碼目前的那一盤（最新開的）。"""
    code = code.strip().upper()
    return Game.objects.filter(mode="pvp", room_code=code).order_by("-id").first() if code else None


def _open_next_round(game):
    """一盤真人對戰打完時開下一盤（回到房間）：同一組密碼、同一位房主、同樣的設定與座位、觀戰的人照樣觀戰，
    撲克換新的洗牌種子。房主在開始前關掉的房間（ended）不開；已經開過就不重開。回傳下一盤或 None。"""
    table = open_table(game) and game.ended and game.started_at is not None
    if not game.pvp or not (game.winner or game.draw or table) or game.next_round_id():
        return None
    module = games.get(game.game_type)
    if table:
        return _next_table(module, game)
    with transaction.atomic():
        nxt = Game.objects.create(game_type=game.game_type, owner=game.owner, human_seat=game.human_seat, mode="pvp",
                                  settings=game.settings, secret=module.new_secret(), room_code=game.room_code)
        GamePlayer.objects.bulk_create([GamePlayer(game=nxt, seat=p.seat, user=p.user, joined_at=p.joined_at)
                                        for p in GamePlayer.objects.filter(game=game)])
        GameSpectator.objects.bulk_create([GameSpectator(game=nxt, user_id=s.user_id)
                                           for s in GameSpectator.objects.filter(game=game)])
    return nxt


def _next_table(module, game):
    """撲克開放牌桌結束後的下一盤（回到房間大廳）：坐在桌上的人照樣坐著（離場、輸光的人除外），
    房間裡沒上桌的人觀戰，離開房間的人不帶過去。"""
    state, seated, others = _table_people(module, game)
    keep = [f"sit {s} {w}" for s, w in sorted(state.occupant.items()) if s not in state.gone and state.stacks[s] > 0]
    with transaction.atomic():
        nxt = Game.objects.create(game_type=game.game_type, owner=game.owner, human_seat=game.human_seat, mode="pvp",
                                  settings=game.settings, secret=module.new_secret(), room_code=game.room_code,
                                  actions=keep)
        tokens = {int(a.split()[2]) for a in keep if a.split()[2].isdigit()}
        GamePlayer.objects.bulk_create([GamePlayer(game=nxt, seat=t, user_id=t - module.member_token_base,
                                                   joined_at=timezone.now()) for t in sorted(tokens)])
        sitting = {t - module.member_token_base for t in tokens}
        GameSpectator.objects.bulk_create([GameSpectator(game=nxt, user_id=u)
                                           for u in list(seated.values()) + others if u not in sitting])
    return nxt


def _lobby(game):
    """房間大廳的操作只能在按「開始」之前；對局中只能下棋、聊天、認輸。"""
    if not game.pvp:
        raise ValueError("只有真人對戰的房間能這樣做")
    if closed_room(game):
        raise ValueError("房主已經關閉這個房間了")
    if game.finished or game.started_at is not None:
        raise ValueError("這一盤已經開始了，要等打完回到房間才能換座位、改設定")


def _member(game, name):
    """房間裡（坐在座位上或觀戰中）叫這個名稱的帳號；不在房間裡時丟出 ValueError。"""
    player = game.players.filter(user__username=name).select_related("user").first()
    if player:
        return player.user
    spectator = game.spectators.filter(user__username=name).select_related("user").first()
    if spectator:
        return spectator.user
    raise ValueError(f"{name} 不在這個房間裡")


def _fixed_host_seat(module):
    """撲克的房主固定坐 1 號位（只有他能開下一手、離場），換座位不能動那個位子，轉讓房主時新房主換過去。"""
    return module.pvp_pick_seats


def move_player(module, game, actor, name, seat):
    """換座位（2026-11-15）：seat 是要坐的座位，None 是改成觀戰。
    每個人都能自己坐到空的座位或起身觀戰；房主還能替別人換：坐到有人的座位時兩人交換
    （被換的人坐到對方原本的位置，對方原本在觀戰就改成觀戰）。房主要一直坐在座位上。回傳對局。"""
    _lobby(game)
    if open_table(game):
        raise ValueError("德州撲克的座位是隨機的，請用「上桌」「下桌」")
    target = _member(game, name)
    host = game.owner_id == actor.id
    if target.id != actor.id and not host:
        raise ValueError("只有房主能幫別人換座位")
    with transaction.atomic():
        game = Game.objects.select_for_update().get(pk=game.pk)
        _lobby(game)
        places = {p.seat: p for p in game.players.select_for_update()}
        current = next((s for s, p in places.items() if p.user_id == target.id), None)
        if seat == current:
            return game
        if seat is not None and seat not in places:
            raise ValueError("沒有這個真人座位")
        occupant = places[seat].user_id if seat is not None else None
        if occupant and not host:
            raise ValueError("這個座位已經有人了")
        if _fixed_host_seat(module) and game.human_seat in (seat, current):
            raise ValueError("房主固定坐 1 號位（開下一手、離場都由房主決定）；要換房主請用「轉讓房主」")
        # 換完後誰坐哪裡：目標坐 seat；原本坐 seat 的人換到目標原本的位置（目標原本在觀戰就改成觀戰）
        moved = {target.id: seat}
        if occupant:
            moved[occupant] = current
        if game.owner_id in moved and moved[game.owner_id] is None:
            raise ValueError("房主要坐在座位上；想觀戰請先把房主轉讓給別人")
        if current is not None:
            places[current].user_id, places[current].joined_at = occupant, timezone.now() if occupant else None
            places[current].save()
        if seat is not None:
            places[seat].user_id, places[seat].joined_at = target.id, timezone.now()
            places[seat].save()
        for user_id, place in moved.items():
            if place is None:
                GameSpectator.objects.get_or_create(game=game, user_id=user_id)
            else:
                GameSpectator.objects.filter(game=game, user_id=user_id).delete()
        if game.owner_id in moved:
            game.human_seat = moved[game.owner_id]
        game.save()
    return game


def kick_player(game, actor, name):
    """房主請人離開房間（2026-11-15）：空出他的座位或拿掉觀戰。使用者決定被請出去的人可以再用房間密碼回來。"""
    _lobby(game)
    if game.owner_id != actor.id:
        raise ValueError("只有房主能請人離開房間")
    target = _member(game, name)
    if target.id == actor.id:
        raise ValueError("不能請自己離開；要離開請按「離開房間」")
    _remove_from_room(game, target)
    game.save(update_fields=["updated_at"])
    return game


def _remove_from_room(game, user):
    if open_table(game):  # 撲克開放牌桌（只在開始前）：坐在桌上就先下桌，排隊的也拿掉
        module = games.get(game.game_type)
        token = module.member_token(user)
        state = module.replay(settings_of(game), game.actions)
        seat = state.seat_of_token(token)
        if seat is not None:
            _table_apply(module, game, state, f"stand {seat}")
        game.table = {**game.table, "queue": [e for e in game.table.get("queue", []) if e.get("token") != token]}
        game.save()
        GamePlayer.objects.filter(game=game, user=user).delete()
        GameSpectator.objects.filter(game=game, user=user).delete()
        return
    GamePlayer.objects.filter(game=game, user=user).update(user=None, joined_at=None)
    GameSpectator.objects.filter(game=game, user=user).delete()


def transfer_host(module, game, actor, name):
    """房主把房主交給房間裡坐在座位上的另一個人（2026-11-15）。撲克的新房主換到 1 號位，原房主坐到他原本的位置。"""
    _lobby(game)
    if game.owner_id != actor.id:
        raise ValueError("只有房主能轉讓房主")
    target = _member(game, name)
    if target.id == actor.id:
        raise ValueError("你已經是房主了")
    seat = game.seat_of(target)
    if seat is None and not open_table(game):  # 撲克開放牌桌的房主不一定坐在桌上（2026-11-16）
        raise ValueError(f"{name} 在觀戰；要先請他坐下，才能把房主交給他")
    with transaction.atomic():
        game = Game.objects.select_for_update().get(pk=game.pk)
        _lobby(game)
        if _fixed_host_seat(module):
            GamePlayer.objects.filter(game=game, seat=seat).update(user=actor, joined_at=timezone.now())
            GamePlayer.objects.filter(game=game, seat=game.human_seat).update(user=target, joined_at=timezone.now())
        elif not open_table(game):
            game.human_seat = seat
        game.owner = target
        game.save()
    return game


def leave_room(module, game, user):
    """離開房間（2026-11-15）：觀戰的人隨時可以走；坐在座位上的人只能在開始前離開（對局中要認輸）。
    房主離開時，房主交給坐在座位上、座位號碼最小的人（撲克換到 1 號位）；沒有別人坐著就關閉房間。回傳對局。"""
    if not game.pvp:
        raise ValueError("只有真人對戰的房間能離開")
    if open_table(game):
        return _leave_table_room(module, game, user)
    seat = game.seat_of(user)
    if seat is None:
        GameSpectator.objects.filter(game=game, user=user).delete()
        return game
    if game.finished:
        return game
    if game.started_at is not None:
        raise ValueError("對局進行中不能離開房間；要結束請認輸")
    _lobby(game)
    with transaction.atomic():
        game = Game.objects.select_for_update().get(pk=game.pk)
        _lobby(game)
        if game.owner_id == user.id:
            heirs = list(game.players.exclude(user=user).filter(user__isnull=False).order_by("seat"))
            if not heirs:
                game.ended = True  # 和房主在開始前關閉房間一樣（closed_room）
                game.save()
                return game
            heir = heirs[0]
            game.owner_id = heir.user_id
            if _fixed_host_seat(module):
                GamePlayer.objects.filter(game=game, seat=game.human_seat).update(user=heir.user_id,
                                                                                    joined_at=heir.joined_at)
                heir.user, heir.joined_at = None, None
                heir.save()
            else:
                game.human_seat = heir.seat
                _remove_from_room(game, user)
        else:
            _remove_from_room(game, user)
        game.save()
    return game


def _leave_table_room(module, game, user):
    """撲克開放牌桌離開房間（2026-11-16）：坐在桌上要先下桌（開始前直接幫他下桌）；排隊的拿掉。
    打過牌的人留著成員紀錄（left）給他復盤。房主離開時交給房間裡的下一個人，沒有人了：開始前關閉房間，開始後結束牌局。"""
    with transaction.atomic():
        game = Game.objects.select_for_update().get(pk=game.pk)
        if game.finished:
            GameSpectator.objects.filter(game=game, user=user).delete()
            return game
        token = module.member_token(user)
        state = module.replay(settings_of(game), game.actions)
        seat = state.seat_of_token(token)
        if seat is not None:
            if game.started_at is not None:
                raise ValueError("你還在牌桌上：先按「觀眾」或「離場」下桌，才能離開房間")
            _table_apply(module, game, state, f"stand {seat}")
        game.table = {**game.table, "queue": [e for e in game.table.get("queue", []) if e.get("token") != token]}
        GameSpectator.objects.filter(game=game, user=user).delete()
        if game.started_at is None:
            GamePlayer.objects.filter(game=game, user=user).delete()
        else:
            GamePlayer.objects.filter(game=game, user=user).update(left=True)
        if game.owner_id == user.id:
            _, seated, others = _table_people(module, game)
            heirs = [u for u in list(seated.values()) + others if u != user.id]
            if heirs:
                game.owner_id = heirs[0]
            else:
                game.ended = True  # 沒有人了：開始前等於關閉房間，開始後等於結束牌局
        game.save()
    return game


def watch_game(code, user):
    """用房間密碼觀戰（2026-11-08）：記成觀戰者，回傳 (對局, 座位)。本來就坐在這盤的人照樣用自己的座位，
    座位是 None 表示觀戰。等人中、對戰中、已結束都能看。"""
    game = _latest_round(code)  # 房間打過好幾盤時是目前那一盤（2026-11-15）
    if game is None:
        raise ValueError("找不到這個房間密碼，請確認有沒有打錯")
    if closed_room(game):
        raise ValueError("房主已經關閉這個房間了")
    if open_table(game):
        return _enter_table_room(game, user)
    seat = game.seat_of(user)
    if seat is None:
        GameSpectator.objects.get_or_create(game=game, user=user)
    return game, seat


def _enter_table_room(game, user):
    """進入撲克開放牌桌的房間（加入或觀戰都一樣）：上過桌的人回到房間（取消 left），其他人到觀戰席。回傳 (對局, 座位)。"""
    player = game.players.filter(user=user).first()
    if player:
        if player.left:
            player.left = False
            player.save(update_fields=["left"])
        return game, player.seat
    GameSpectator.objects.get_or_create(game=game, user=user)
    game.save(update_fields=["updated_at"])
    return game, None


START_COUNTDOWN = 3  # 房主按「開始」後倒數幾秒才能下（使用者要求，2026-11-13）


def start_room(game, user):
    """房主按「開始」（使用者要求，2026-11-13：建立房間後由房主按開始，而且要等三秒）。
    座位都坐滿才能按；開始時間記成現在＋倒數，到了那個時間 play_human_move、run_ai_turns 才接受。回傳更新後的對局。"""
    if not game.pvp or game.owner_id != user.id:
        raise ValueError("只有開房的人能按「開始」")
    if game.finished:
        raise ValueError("這盤已經結束了")
    if game.started_at is not None:
        raise ValueError("已經按過開始了")
    if game.waiting:
        raise ValueError("還有座位空著，朋友都加入後才能開始")
    if open_table(game):
        module = games.get(game.game_type)
        if len(module.replay(settings_of(game), game.actions).occupant) < 2:
            raise ValueError("牌桌上要有兩個人以上（可以補 AI）才能開始")
    # 條件式更新：同時按兩次（例如兩個分頁）只有一次算數，倒數不會被往後延
    if not Game.objects.filter(pk=game.pk, started_at__isnull=True, ended=False).update(
            started_at=timezone.now() + timedelta(seconds=START_COUNTDOWN)):
        raise ValueError("已經按過開始了")
    game.refresh_from_db()
    return game


def closed_room(game):
    """房主在開始前關掉的房間（使用者要求，2026-11-13）：記成「結束（不計勝負）」而且從沒開始過。"""
    return game.pvp and game.ended and game.started_at is None


def close_room(game, user):
    """房主關閉還沒開始的房間（2026-11-13 使用者要求「房主要能夠關閉房間」）：記成結束（不計勝負），
    不進戰績，房間密碼與邀請連結都不能再加入或觀戰，清單與頂端的「回到房間」也不再列出。
    開始以後不能關：快輸的一方關掉就能讓輸棋不算（和真人對戰不能「不計勝負」同一個理由），要結束請認輸。"""
    if not game.pvp or game.owner_id != user.id:
        raise ValueError("只有開房的人能關閉房間")
    if game.finished:
        raise ValueError("這個房間已經結束了")
    # 條件式更新：和按「開始」同時發生時只有一個算數
    if not Game.objects.filter(pk=game.pk, started_at__isnull=True, ended=False).update(ended=True,
                                                                                       updated_at=timezone.now()):
        raise ValueError("已經開始了，不能關閉房間；要結束請認輸")
    game.refresh_from_db()
    return game


def wait_for_start(game):
    """等到倒數結束（AI 的座位要等倒數完才行動，和真人一樣）。
    原本睡 starts_in() 的秒數，那是給網頁看的、四捨五入到小數兩位的值，約四成會早醒幾毫秒，
    接著 run_ai_turns 看到還沒開始就不行動，撲克由 AI 先說話的牌桌就整個卡住（2026-11-15 修正）。
    所以用沒有四捨五入的剩餘時間，再多等一點，醒來時一定已經開始。"""
    if game.started_at is None:
        return
    left = (game.started_at - timezone.now()).total_seconds()
    if left > 0:
        time.sleep(left + 0.05)


def join_game(code, user):
    """用房間密碼加入真人對戰：坐進第一個空的真人座位。回傳 (對局, 座位)。已經在這盤裡就直接回傳原本的座位。
    座位都坐滿時改成觀戰（使用者要求，2026-11-13），座位是 None。"""
    game = _latest_round(code)  # 房間打過好幾盤時是目前那一盤（2026-11-15）
    if game is None:
        raise ValueError("找不到這個房間密碼，請確認有沒有打錯")
    if closed_room(game):
        raise ValueError("房主已經關閉這個房間了")
    if open_table(game):
        return _enter_table_room(game, user)
    seat = game.seat_of(user)
    if seat is not None:
        return game, seat
    if game.finished:
        raise ValueError("這盤已經結束了")
    # 只有座位還空著時才寫入（條件式更新）：兩個人同時按「加入」時，先讀再存的話後存的會蓋掉先存的，
    # 被蓋掉的人以為加入了其實沒有座位（2026-11-05 審核 #10 時發現）。沒搶到就換下一個空位
    for empty in game.players.filter(user__isnull=True).order_by("seat"):
        if GamePlayer.objects.filter(pk=empty.pk, user__isnull=True).update(user=user, joined_at=timezone.now()):
            game.save(update_fields=["updated_at"])
            return game, empty.seat
    GameSpectator.objects.get_or_create(game=game, user=user)
    return game, None


# ---- 撲克開放牌桌（使用者要求「補入式」，2026-11-16；docs/交接.md 4.10 第 14 項）----
# 牌桌一開始是空的：開始前每個人自己上桌（座位隨機）、房主補 AI；兩個以上就能開始。
# 開始後不能直接上桌，要排隊（queue），一手牌結束時才補進去；桌上的人按「觀眾」，這手結束後下桌（leaving）。
# 一手結束後由房間裡的網頁等 TABLE_DELAY 秒送 deal（table_advance）：先讓要下桌、籌碼輸光、離場的人下桌，
# 再把排隊的人補進隨機空位，有兩個以上有籌碼的人就發下一手。不計勝負，房主按「結束牌局」才結束（table_end）。
# 誰坐哪裡記在 actions 的 "sit N 誰"／"stand N"（重播得出來）；真人在 GamePlayer 的 seat 是成員代號（module.member_token），
# 不是座位號碼，所以換位子不用改 GamePlayer，上過桌的人一直看得到這一盤、能復盤。

TABLE_DELAY = 5  # 一手牌結束後幾秒補人、發下一手（使用者同意，2026-11-16）


def open_table(game):
    return game.pvp and bool(game.settings.get("table"))


def _table_settings(module, mode, settings):
    """真人對戰的撲克改用開放牌桌：設定加上 table，拿掉舊的固定真人座位。"""
    if mode == "pvp" and getattr(module, "open_table", False):
        settings = {k: v for k, v in settings.items() if k != "humans"}
        settings["table"] = True
    return settings


def _random_empty(state):
    empty = [s for s in state.seats if s not in state.occupant]
    return secrets.choice(empty) if empty else None  # 座位隨機（使用者要求）


def _make_member(game, user_id, token):
    """上過桌的人記成這一盤的成員（GamePlayer 的 seat 是成員代號）：之後下桌了也看得到這一盤、能復盤。"""
    GamePlayer.objects.update_or_create(game=game, user_id=user_id,
                                        defaults={"seat": token, "left": False, "joined_at": timezone.now()})
    GameSpectator.objects.filter(game=game, user_id=user_id).delete()


def _to_spectator(game, user_id):
    """開始前下桌：還沒打過牌，不留成員紀錄，改成觀戰。"""
    GamePlayer.objects.filter(game=game, user_id=user_id).delete()
    GameSpectator.objects.get_or_create(game=game, user_id=user_id)


def _table_apply(module, game, state, action):
    state = module.apply(state, action)  # 不合法會丟出 ValueError
    game.actions.append(action)
    return state


def table_action(module, game, user, op, kind="", seat=0):
    """開放牌桌的操作（除了 deal、end）。回傳 (對局, 要不要接著補人發牌)：開始後牌桌在等人時排進來的人馬上補進去。
    op：sit（上桌；開始後是排隊）、unqueue（離開隊列；房主帶 seat＝隊列第幾個，拿掉排隊的 AI）、
    stand（下桌；開始後是「觀眾」，這手結束才下桌，再按一次取消）、add_ai（房主補 AI，kind 是 agent 或 bot）、
    remove_ai（房主請 seat 號位的 AI 下桌，開始後同樣這手結束才下桌）。"""
    if not open_table(game):
        raise ValueError("只有真人對戰的德州撲克牌桌能這樣做")
    host = game.owner_id == user.id
    with transaction.atomic():
        game = Game.objects.select_for_update().get(pk=game.pk)
        if closed_room(game):
            raise ValueError("房主已經關閉這個房間了")
        if game.finished:
            raise ValueError("這一盤已經結束了")
        if not game.in_room(user):
            raise ValueError("你不在這個房間裡")
        started = game.started_at is not None
        state = module.replay(settings_of(game), game.actions)
        token = module.member_token(user)
        mine = state.seat_of_token(token)
        queue, leaving = list(game.table.get("queue", [])), list(game.table.get("leaving", []))
        if op in ("add_ai", "remove_ai") or (op == "unqueue" and seat):
            if not host:
                raise ValueError("只有房主能補 AI 或請 AI 下桌")
        if op == "sit":
            if mine is not None:
                raise ValueError("你已經在牌桌上了")
            if not started:
                place = _random_empty(state)
                if place is None:
                    raise ValueError("牌桌已經坐滿了")
                _table_apply(module, game, state, f"sit {place} {token}")
                _make_member(game, user.id, token)
            elif any(e.get("token") == token for e in queue):
                raise ValueError("你已經在隊列裡了")
            else:
                queue.append({"token": token, "name": user.username})
        elif op == "unqueue":
            if seat:
                if not 1 <= seat <= len(queue):
                    raise ValueError("隊列裡沒有這一位")
                queue.pop(seat - 1)
            elif any(e.get("token") == token for e in queue):
                queue = [e for e in queue if e.get("token") != token]
            else:
                raise ValueError("你不在隊列裡")
        elif op == "add_ai":
            if kind not in ("agent", "bot"):
                raise ValueError("只能補 Agent AI 或機器人")
            if not started:
                place = _random_empty(state)
                if place is None:
                    raise ValueError("牌桌已經坐滿了")
                _table_apply(module, game, state,
                             f"sit {place} {'agent' if kind == 'agent' else module.table_bot_role(state)}")
            else:
                queue.append({"ai": kind})
        elif op in ("stand", "remove_ai"):
            target = mine if op == "stand" else seat
            if op == "stand" and target is None:
                raise ValueError("你不在牌桌上")
            if op == "remove_ai" and (target not in state.occupant or isinstance(state.occupant[target], int)):
                raise ValueError("這個座位不是 AI")
            if target in leaving:  # 再按一次：取消，留在牌桌上
                leaving.remove(target)
            elif not started or state.hand_over:
                _table_apply(module, game, state, f"stand {target}")
                if op == "stand" and not started:
                    _to_spectator(game, user.id)
            else:
                leaving.append(target)
        else:
            raise ValueError(f"看不懂的牌桌操作：{op}")
        game.table = {"queue": queue, "leaving": leaving}
        game.save()
        waiting = started and game.started and state.hand_over and len(state.live()) < 2 and bool(queue)
    return game, waiting


def table_advance(module, game, hand, model, steps, seat):
    """一手牌結束後（或剛按完開始）補人、發下一手（網頁等 TABLE_DELAY 秒後送來，hand 是它看到的那一手）。
    已經發過了（別人先送到）、這手還沒結束、還沒開始時什麼都不做。發牌後 AI 的座位行動到輪到真人。回傳 AI 行動。"""
    if not open_table(game):
        raise ValueError("只有真人對戰的德州撲克牌桌能這樣做")
    with transaction.atomic():
        game = Game.objects.select_for_update().get(pk=game.pk)
        if game.finished or not game.started:
            return []
        state = module.replay(settings_of(game), game.actions)
        if not state.hand_over or (hand is not None and state.hand_no != hand):
            return []
        leaving = set(game.table.get("leaving", []))
        for s in sorted(state.occupant):  # 按了「觀眾」、房主請下桌的 AI、離場或籌碼輸光的人下桌
            if s in leaving or s in state.gone or state.stacks[s] == 0:
                state = _table_apply(module, game, state, f"stand {s}")
        rest = []
        for entry in game.table.get("queue", []):
            place = _random_empty(state)
            token = entry.get("token")
            if place is None:
                rest.append(entry)  # 坐滿了：繼續排隊
                continue
            if token is not None:
                if state.seat_of_token(token) is not None:
                    continue
                state = _table_apply(module, game, state, f"sit {place} {token}")
                _make_member(game, token - module.member_token_base, token)
            else:
                who = "agent" if entry.get("ai") == "agent" else module.table_bot_role(state)
                state = _table_apply(module, game, state, f"sit {place} {who}")
        game.table = {"queue": rest, "leaving": []}
        if len(state.live()) >= 2:
            state = _table_apply(module, game, state, "deal")
        game.save()
    return run_ai_turns(module, game, model, steps, seat)


def table_end(game, user):
    """房主結束牌局（不計勝負）：記成結束，所有人回到房間大廳（下一盤照樣坐著）。回傳結束的這一盤。"""
    if not open_table(game) or game.owner_id != user.id:
        raise ValueError("只有房主能結束牌局")
    if game.started_at is None:
        raise ValueError("還沒開始；不玩了可以「關閉房間」")
    if not Game.objects.filter(pk=game.pk, ended=False, winner__isnull=True).update(ended=True,
                                                                                   updated_at=timezone.now()):
        raise ValueError("這一盤已經結束了")
    game.refresh_from_db()
    _open_next_round(game)
    return game


def _table_people(module, game):
    """開放牌桌房間裡的人：(坐在桌上的 {座位: 帳號編號}, 在房間裡沒上桌的帳號編號)。"""
    state = module.replay(settings_of(game), game.actions)
    seated = {s: w - module.member_token_base for s, w in state.occupant.items() if isinstance(w, int)}
    members = [p.user_id for p in game.players.all() if not p.left and p.user_id not in seated.values()]
    watching = [s.user_id for s in game.spectators.all()]
    return state, seated, list(dict.fromkeys(members + watching))


def play_human_move(module, game, action, seat):
    """seat 的真人行動：伺服器驗證後存檔，回傳這一步之後的畫面。不合法時丟出 ValueError。"""
    if game.finished:
        raise ValueError("這盤棋已經結束")
    if game.waiting:
        raise ValueError("還在等對手加入，對手加入後才能開始")
    if not game.started:
        raise ValueError("房主還沒按「開始」" if game.started_at is None else "倒數還沒結束，請稍等")
    state = module.replay(settings_of(game), game.actions)
    if not module.may_act(state, action, seat):
        raise ValueError("現在不是你的回合")
    state = module.apply(state, action)  # 不合法會丟出 ValueError
    game.actions.append(action)
    _set_outcome(module, game, state)
    game.save()
    _open_next_round(game)  # 真人對戰打完了：回到房間（2026-11-15）
    return module.frame(state, seat)


def run_ai_turns(module, game, model, steps, seat):
    """由 AI 的座位行動，直到又輪到真人或對局結束；存檔後回傳可以送到網頁的 AI 行動（seat 看到的畫面）。
    真人對戰在房主按「開始」、倒數結束之前不行動：撲克的 AI 要等所有真人坐好、一起開始。"""
    if game.waiting or not game.started:
        return []
    state = module.replay(settings_of(game), game.actions)
    state, moves = _ai_turns(module, game, state, model, steps, seat)
    moves = _save_and_hide(module, game, state, moves)
    _open_next_round(game)  # 撲克可能由 AI 的行動結束整場
    return moves


def game_detail(module, game, seat, user=None):
    """打開一盤棋：seat 看到的每一步畫面與 AI 思路。my_seat 告訴網頁這位使用者坐哪裡（真人對戰時不一定是開局的座位）。
    is_owner：user 是不是房主（房主可以轉讓，2026-11-15，所以網頁不能自己猜）。"""
    notes = _public(module, module.replay(settings_of(game), game.actions),
                    [n.to_dict() for n in game.ai_notes.all()])
    detail = {"game": game.summary(), "my_seat": seat, "actions": game.actions, "frames": _frames(module, game, seat),
              "notes": notes, "is_owner": user is not None and game.owner_id == user.id}
    if open_table(game) and user is not None:  # 撲克開放牌桌：觀戰中的人也要知道自己在不在隊列裡
        detail["my_token"] = module.member_token(user)
    return detail


def coach_allowed(game):
    """真人對戰還沒結束時不能用 AI 教練（復盤、提問、AI 說明），否則就是幫其中一方作弊（使用者決定，2026-11-05）。"""
    if game.pvp and not game.finished:
        raise ValueError("真人對戰進行中不能使用 AI 教練，對局結束後就可以復盤")


# ---- 房間聊天室（使用者要求，2026-11-13）----

def _chat_room(game, seat):
    """這位使用者現在發言進哪個聊天室：房主按開始前大家同一個；按了之後玩家與觀戰的人分開。"""
    if game.started_at is None:
        return "lobby"
    return "players" if seat is not None else "spectators"


def clean_chat_text(text):
    text = " ".join(str(text).split())  # 換行、連續空白縮成一個空白
    if not text:
        raise ValueError("訊息是空的")
    if len(text) > ChatMessage.MAX_LENGTH:
        raise ValueError(f"訊息最多 {ChatMessage.MAX_LENGTH} 個字")
    return text


def chat_messages(game, seat, user):
    """這位使用者看得到的聊天：開始前的訊息大家都看得到，開始後只看得到自己那一邊（玩家或觀戰者）的。
    跨盤保留（使用者決定，2026-11-15）：同一個房間到這一盤為止的每一盤訊息都列出；已經結束的那幾盤，
    玩家與觀戰者兩邊的訊息都公開（打完了，不會再替其中一方出主意）。從對局紀錄打開某一盤，看得到到那一盤為止的聊天。"""
    if not game.pvp:
        return []
    side = "players" if seat is not None else "spectators"
    messages = ChatMessage.objects.filter(game__mode="pvp", game__room_code=game.room_code,
                                          game_id__lte=game.id).select_related("game")
    return [m.to_dict(user) for m in messages if m.room in ("lobby", side) or m.game.finished]


def send_chat(game, user, seat, text):
    """在房間的聊天室發言：坐在座位上的人與觀戰的人才能說話；對局結束後只能看。回傳這則訊息。"""
    if not game.pvp:
        raise ValueError("只有真人對戰的房間有聊天室")
    if seat is None and not game.spectators.filter(user=user).exists():
        raise ValueError("你不在這個房間裡")
    if game.finished:
        raise ValueError("對局已經結束，聊天室只能看、不能再發言")
    message = ChatMessage.objects.create(game=game, user=user, name=user.username, room=_chat_room(game, seat),
                                         text=clean_chat_text(text))
    return message.to_dict(user)


def end_game(game):
    """「結束（不計勝負）」：對局停在這裡，不算輸贏、不進戰績、不能再下，仍可復盤（使用者要求，2026-10-21）。
    真人對戰不行：快輸的一方按了就能讓輸棋不算，要結束請認輸（撲克由開桌的人離場）。"""
    if game.finished:
        raise ValueError("這盤棋已經結束")
    if game.pvp:
        raise ValueError("真人對戰不能「不計勝負」，要結束請認輸")
    game.ended = True
    game.save(update_fields=["ended", "updated_at"])


def narrate_note(module, note, model):
    """AI 某一步的說明。模型成功改寫後存起來，之後（例如復盤時）直接回傳同一段說明。"""
    coach_allowed(note.game)
    state = module.replay(settings_of(note.game), note.game.actions)
    if _public(module, state, [note.to_dict()])[0].get("hidden"):
        raise ValueError("這一步的思路含底牌，這手牌結束後才能看")
    if note.narration:
        return note.narration
    result = narrate(module, note.facts, model, note.game, note.index)
    if result["source"] == "llm":  # 範本說明不存，Ollama 恢復後還能再試
        note.narration = result
        note.save(update_fields=["narration"])
    return result


LEVEL_NAMES = {"key": "關鍵時刻", "minor": "小失誤", "suggest": "建議", "good": "做得好的地方"}


def _main_evidence(moment):
    """一則問題主要的證據種類：規則確認以外最常見的那種（例如撲克是條件式計算）。"""
    kinds = Counter(p["evidence"] for p in moment["points"] if p["evidence"] != RULE)
    return kinds.most_common(1)[0][0] if kinds else RULE


def _overall(module, game, moments, seat):
    """整體評論的事實（使用者要求，2026-10-11）：結果、各級別的數量、最常出現的問題、做得好的地方，加上遊戲自己的統計。
    語言模型只根據這些事實寫評論，跟其他說明一樣經過 narrator.check_text。"""
    state = module.replay(settings_of(game), game.actions)
    result = module.outcome(state)
    if result:
        winner, draw = result
        who = "真人" if winner in game.human_seats() else "AI"
        # 寫明玩家是哪一方、是贏是輸：只寫「白棋（AI）贏了」時，模型回答「我是贏還是輸」只說得出白棋獲勝（2026-11-16 情境測試）
        mine = f"你（{module.seat_names[seat]}）" if seat in module.seat_names else "你"
        verdict = ("和局" if draw else f"{mine}贏了" if winner == seat
                   else f"{mine}輸了，{module.seat_names[winner]}（{who}）贏了")
        detail = module.frame(state, seat).get("result")
        if detail and detail.startswith(verdict):  # 「和局：逼和…」本身就說了和局，不要寫成「和局（和局：逼和…）」
            verdict, detail = detail, None
        points = [(RULE, f"結果：{verdict}" + (f"（{detail}）" if detail else "") + "。")]
    elif game.ended:
        # 真人對戰由房主結束（撲克開放牌桌），別人看到「是你選擇結束」就寫錯了（2026-11-16 情境測試）
        owner_seat = game.seat_of(game.owner) if game.pvp and game.owner else None
        by = "你" if not game.pvp or owner_seat == seat else "房主"
        points = [(RULE, f"這場開放牌桌由{by}結束；開放牌桌不計勝負。" if open_table(game)
                   else f"這盤沒有下完，是{by}選擇結束、不計勝負。")]
    else:
        points = [(RULE, "這盤還沒有下完。")]
    points.append((RULE, f"這盤一共 {len(game.actions)} {module.step_word}。"))
    counts = Counter(m["level"] for m in moments)
    found = "、".join(f"{name} {counts[level]} 個" for level, name in LEVEL_NAMES.items() if counts[level])
    points.append((RULE, f"問題檢查的結果：{found or '沒有找到任何問題'}。"))
    for levels, label, top in ((("key", "minor"), "最常出現的問題", 3), (("good",), "做得好的地方", 2)):
        group = [m for m in moments if m["level"] in levels]
        for pattern, n in Counter(m.get("pattern", m["title"]) for m in group).most_common(top):
            same = [m for m in group if m.get("pattern", m["title"]) == pattern]
            steps = "、".join(f"第 {m['index'] + 1} {module.step_word}" for m in same[:3])
            points.append((_main_evidence(same[0]), f"{label}：「{pattern}」{n} 次（{steps}）。"))
    points += module.overall_points(settings_of(game), game.actions, seat)
    return {"kind": "overall", "points": [{"evidence": e, "text": t} for e, t in points]}


def _steps_text(module, indexes):
    steps = [str(i + 1) for i in indexes]
    first = f"第一次在第 {steps[0]} {module.step_word}"
    return first + (f"，之後在第 {'、'.join(steps[1:6])} {module.step_word}" + ("等" if len(steps) > 6 else "")
                    if len(steps) > 1 else "")


def opponents(module, game, seat):
    """對手觀察（使用者要求，2026-11-05）：真人對戰結束後，用同一套復盤檢查每位對手（其他真人）的每一步，
    整理成「對手的傾向、在第幾步出現跡象、知道後可以怎麼應對」。

    傾向來自 module.review 對對手座位找到的問題（關鍵時刻與小失誤，證據種類照原本的），依類型分組；
    應對建議來自 module.opponent_tips（程式寫好的經驗做法，標成電腦估計）；撲克另有打法統計（module.opponent_points）。
    和 AI 的對局不做：AI 的每一步就是引擎的選擇，復盤標準一致時本來就找不到問題。"""
    if not game.pvp:
        return []
    names = {p.seat: p.user.username for p in game.players.all() if p.user}
    result = []
    for opp in sorted(game.human_seats() - {seat}):
        # 撲克開放牌桌的真人換過座位（opp 是成員代號，2026-11-16），只寫名稱
        who = f"{names.get(opp, '對手')}（{module.seat_names[opp]}）" if opp in module.seat_names else names.get(opp, "對手")
        found = [m for m in module.review(settings_of(game), game.actions, opp) if m["level"] in ("key", "minor")]
        groups = {}
        for m in found:
            groups.setdefault(m.get("pattern", m["title"]), []).append(m)
        points = []
        # 關鍵時刻那類的習慣先講，再依次數多寡
        severity = lambda kv: (all(m["level"] != "key" for m in kv[1]), -len(kv[1]), kv[1][0]["index"])  # noqa: E731
        for pattern, items in sorted(groups.items(), key=severity):
            points.append((_main_evidence(items[0]), f"{who}出現「{pattern}」{len(items)} 次："
                                                      f"{_steps_text(module, [m['index'] for m in items])}。"))
            tip = module.opponent_tips.get(pattern)
            if tip:
                points.append((ESTIMATE, f"應對建議（經驗做法，不是這盤算出來的）：{tip}"))
        points += module.opponent_points(settings_of(game), game.actions, opp, who)
        if not points:
            points = [(RULE, f"電腦檢查了{who}的每一步，沒有找到明顯的失誤或固定的習慣。")]
        result.append({"seat": opp, "name": names.get(opp, ""),
                       "facts": {"kind": "opponent", "points": [{"evidence": e, "text": t} for e, t in points]},
                       "signs": [{"index": m["index"], "title": m["title"], "level": m["level"],
                                  "played": m["played"], "best": m["best"]} for m in found]})
    return result


def review(module, game, seat):
    """seat 這位真人的復盤（真人對戰時每個人從自己的座位看），真人對戰另有對手觀察。"""
    coach_allowed(game)
    moments = module.review(settings_of(game), game.actions, seat)
    overall = _overall(module, game, moments, seat)
    rivals = opponents(module, game, seat)
    for m in moments:
        m["facts"] = {"kind": "review", "points": m.pop("points")}
    return {"moments": moments, "overall": overall, "opponents": rivals}


# ---- 提問 ----

def position_points(module, game, at, focus, seat):
    """第 at 步之後的局面事實（seat 看得到的）；focus 是 module.question_focus 找到的座標或行動。"""
    coach_allowed(game)
    state = module.replay(settings_of(game, seat), game.actions[:at])
    over = module.outcome(state)
    if over or (game.ended and at == len(game.actions)):
        # 終局（2026-11-16 情境測試）：原本只有「這時對局已經結束（白棋獲勝）」，模型答不出「我是贏還是輸」
        # 「我為什麼輸」「輸了幾目」。改用整體評論的事實：玩家是哪一方、輸贏與終局說明（圍棋的目數），
        # 以及問題檢查找到的問題在第幾步；都是引擎與規則的結果，和復盤的「問題檢查」同一份。
        # 「結束（不計勝負）」的最後局面也一樣，否則問「誰贏了」時事實裡沒有說這盤不計勝負。
        moments = module.review(settings_of(game), game.actions, seat)
        points = _overall(module, game, moments, seat)["points"]
        if not over:  # 不計勝負：棋停在一半，接著附上停下來時的局面
            return points + module.explain_position(state, focus, None)
        if game.actions[-1] == "resign":  # 認輸（四種棋都叫 resign）：附上認輸前的局面，問「認輸時真的輸定了嗎」才有依據
            points += module.explain_action(settings_of(game, seat), game.actions, len(game.actions))
        return points
    next_action = game.actions[at] if at < len(game.actions) else None
    return module.explain_position(state, focus, next_action)


def action_points(module, game, n, seat):
    """第 n 步（從 1 起算）的事實（seat 看得到的）。"""
    coach_allowed(game)
    if not 1 <= n <= len(game.actions):
        raise ValueError(f"這盤只有 {len(game.actions)} {module.step_word}")
    return module.explain_action(settings_of(game, seat), game.actions, n)


def ask_points_by_rules(module, game, question, at, seat):
    """原本的做法：用正規表示式從問題找出座標與「第 N 手」，再查那些事實。"""
    points = position_points(module, game, at, module.question_focus(question), seat)
    if module.outcome(module.replay(settings_of(game), game.actions[:at])):
        return points
    # 玩家常用「第 N 手」（撲克「第 N 步」）指稱；第 at+1 步就是上面的「實際下一步」，不重複
    refs = []
    for s in MOVE_REF_RE.findall(question):
        n = int(s)
        if 1 <= n <= len(game.actions) and n != at + 1 and n not in refs:
            refs.append(n)
    for n in refs[:2]:
        points += action_points(module, game, n, seat)
    return points


def answer(module, game, question, at, points, model):
    """依查到的事實請語言模型回答提問。"""
    facts = {"kind": "ask", "question": question, "points": points}
    return {**narrate(module, facts, model, game, at), "facts": facts}
