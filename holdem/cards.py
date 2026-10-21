"""撲克牌、牌型判斷、起手牌強度與勝率模擬（純程式，不依賴外部套件）。

牌的內部記法是兩個字元：點數 23456789TJQKA ＋ 花色 s h d c，例如 "As"、"Td"。
說明文字與網頁標籤用顯示記法「A♠」「10♦」（show）。
"""
import math
from collections import Counter

RANKS = "23456789TJQKA"
SUITS = "shdc"
SUIT_SYMBOLS = {"s": "♠", "h": "♥", "d": "♦", "c": "♣"}
DECK = [r + s for r in RANKS for s in SUITS]
RANK_NAMES = {14: "A", 13: "K", 12: "Q", 11: "J", 10: "10"}


def value(card):
    return RANKS.index(card[0]) + 2


def rank_name(v):
    return RANK_NAMES.get(v, str(v))


def show(card):
    return rank_name(value(card)) + SUIT_SYMBOLS[card[1]]


def show_all(cards):
    return " ".join(show(c) for c in cards)


def _straight_high(values):
    """由大到小的點數中最大的順子頂張；A 也可以當 1（A2345）。沒有順子回傳 0。"""
    vals = sorted(set(values), reverse=True)
    if 14 in vals:
        vals.append(1)
    run = 1
    for i in range(1, len(vals)):
        run = run + 1 if vals[i] == vals[i - 1] - 1 else 1
        if run >= 5:
            return vals[i] + 4
    return 0


def evaluate(cards):
    """最好的 5 張（最多 7 張），回傳可以直接比大小的 tuple：(牌型類別, 決勝點數...)。

    也用來看公共牌本身的牌型（3–4 張），所以踢腳可能不存在，這時記為 0。
    """
    values = sorted((value(c) for c in cards), reverse=True)
    counts = Counter(values)
    suit_counts = Counter(c[1] for c in cards)
    flush = next((s for s, n in suit_counts.items() if n >= 5), None)
    if flush:
        flush_values = sorted((value(c) for c in cards if c[1] == flush), reverse=True)
        high = _straight_high(flush_values)
        if high:
            return (8, high)
    quads = [v for v, n in counts.items() if n == 4]
    trips = sorted((v for v, n in counts.items() if n == 3), reverse=True)
    pairs = sorted((v for v, n in counts.items() if n == 2), reverse=True)
    if quads:
        return (7, quads[0], max((v for v in values if v != quads[0]), default=0))
    if trips and (len(trips) > 1 or pairs):
        return (6, trips[0], max(trips[1:] + pairs))
    if flush:
        return (5, *flush_values[:5])
    high = _straight_high(values)
    if high:
        return (4, high)
    if trips:
        return (3, trips[0], *[v for v in values if v != trips[0]][:2])
    if len(pairs) >= 2:
        return (2, pairs[0], pairs[1], max((v for v in values if v not in pairs[:2]), default=0))
    if pairs:
        return (1, pairs[0], *[v for v in values if v != pairs[0]][:3])
    return (0, *values[:5])


def describe(rank):
    """牌型的中文說明，例如「一對 K」「葫蘆（Q 帶 7）」。"""
    cat, a = rank[0], rank_name(rank[1])
    if cat == 2:
        return f"兩對（{a} 和 {rank_name(rank[2])}）"
    if cat == 6:
        return f"葫蘆（{a} 帶 {rank_name(rank[2])}）"
    return {8: f"同花順（{a} 大）", 7: f"四條 {a}", 5: f"同花（{a} 大）", 4: f"順子（{a} 大）",
            3: f"三條 {a}", 1: f"一對 {a}", 0: f"高牌 {a}"}[cat]


# ---- 起手牌強度：陳氏公式（Chen formula），用來排序 1326 種起手牌 ----

def chen(c1, c2):
    hi, lo = sorted((value(c1), value(c2)), reverse=True)
    base = {14: 10, 13: 8, 12: 7, 11: 6}.get(hi, hi / 2)
    if hi == lo:
        return max(5, base * 2)
    score = base + (2 if c1[1] == c2[1] else 0)
    gap = hi - lo - 1
    score -= (0, 1, 2, 4)[gap] if gap < 4 else 5
    if gap <= 1 and hi < 12:
        score += 1
    return math.ceil(score)


def _top_table():
    """每種起手牌（兩張牌的 frozenset）→ 它在所有起手牌中的前百分之幾（同分算在一起，取上緣）。"""
    hands = [frozenset((a, b)) for i, a in enumerate(DECK) for b in DECK[i + 1:]]
    scores = {h: chen(*h) for h in hands}
    by_score = Counter(scores.values())
    above, top = 0, {}
    for score in sorted(by_score, reverse=True):
        above += by_score[score]
        top[score] = above / len(hands)
    return {h: top[s] for h, s in scores.items()}


TOP = _top_table()


def top_fraction(c1, c2):
    """這手起手牌在 1326 種起手牌中的前百分之幾（0–1，越小越強）。"""
    return TOP[frozenset((c1, c2))]


# ---- 勝率模擬 ----

def weighted_range(known, weight):
    """對手可能的起手牌：所有不含 known（自己看得到的牌）的組合，各帶權重 weight(a, b)（0–1）。

    回傳 (組合清單, 累積權重)，給 equity_vs 抽樣用；沒有任何組合時回傳 None。
    """
    combos, cum, total = [], [], 0.0
    for i, a in enumerate(DECK):
        if a in known:
            continue
        for b in DECK[i + 1:]:
            if b in known:
                continue
            w = weight(a, b)
            if w > 0:
                total += w
                combos.append([a, b])
                cum.append(total)
    return (combos, cum) if combos else None


def limit_range(known, limit):
    """起手牌前百分之 limit 的範圍（陳氏公式排序），每個組合權重相同。"""
    return weighted_range(known, lambda a, b: 1.0 if TOP[frozenset((a, b))] <= limit else 0.0)


def equity_vs(hole, board, ranges, sims, rng, mixture=False):
    """蒙地卡羅模擬：hole 對上各對手（ranges 是 weighted_range 的結果）打到攤牌的勝率，平手按人數平分。

    mixture=True 時每次只隨機挑一位對手單挑，用來估計「加注後只有一個人跟」的勝率。
    """
    known = set(hole) | set(board)
    deck = [c for c in DECK if c not in known]
    need = 5 - len(board)
    total = 0.0
    for _ in range(sims):
        chosen = [rng.choice(ranges)] if mixture else ranges
        used, opponents = set(), []
        for combos, cum in chosen:
            for _ in range(20):
                pair = rng.choices(combos, cum_weights=cum)[0]
                if pair[0] not in used and pair[1] not in used:
                    break
            else:  # 範圍裡的牌都被別人拿走了，改抽剩下的牌
                pair = rng.sample([c for c in deck if c not in used], 2)
            used.update(pair)
            opponents.append(pair)
        full_board = board + rng.sample([c for c in deck if c not in used], need)
        mine = evaluate(hole + full_board)
        theirs = [evaluate(o + full_board) for o in opponents]
        best = max(theirs, default=(-1,))
        if mine > best:
            total += 1
        elif mine == best:
            total += 1 / (1 + theirs.count(mine))
    return total / sims


def equity(hole, board, limits, sims, rng):
    """hole 對上各對手（limits 是每位對手起手牌前百分之幾）打到攤牌的勝率。"""
    known = set(hole) | set(board)
    return equity_vs(hole, board, [limit_range(known, limit) for limit in limits], sims, rng)


def has_draw(hole, board):
    """還差一張就能成同花或順子（只看翻牌、轉牌；比 outs 快很多，推估範圍時用）。"""
    if len(board) not in (3, 4):
        return False
    cards = hole + board
    if max(sum(c[1] == s for c in cards) for s in SUITS) == 4:
        return True
    values = {value(c) for c in cards}
    if 14 in values:
        values.add(1)
    return any(len(values & set(range(low, low + 5))) == 4 for low in range(1, 11))


def starting_hands():
    """169 種起手牌：[(代號, 代表的兩張牌)]，代號像 "AA"、"AKs"（同花）、"AKo"（不同花）。"""
    hands = []
    for i, hi in enumerate(reversed(RANKS)):
        for lo in reversed(RANKS[:len(RANKS) - i]):
            if hi == lo:
                hands.append((hi + lo, [hi + "s", lo + "h"]))
            else:
                hands.append((hi + lo + "s", [hi + "s", lo + "s"]))
                hands.append((hi + lo + "o", [hi + "s", lo + "h"]))
    return hands


def hand_label(key):
    """起手牌代號的顯示文字：「A A」「A K 同花」「K 10 不同花」。"""
    ranks = " ".join(rank_name(RANKS.index(r) + 2) for r in key[:2])
    return ranks + {"s": " 同花", "o": " 不同花"}.get(key[2:], "")


def win_tie_vs_random(hole, sims, rng):
    """hole 對上一手隨機起手牌、打到攤牌：(贏的比例, 平手的比例)。"""
    deck = [c for c in DECK if c not in hole]
    win = tie = 0
    for _ in range(sims):
        drawn = rng.sample(deck, 7)
        mine, theirs = evaluate(hole + drawn[2:]), evaluate(drawn[:2] + drawn[2:])
        if mine > theirs:
            win += 1
        elif mine == theirs:
            tie += 1
    return win / sims, tie / sims


def outs(hole, board):
    """再來一張牌就能成順子或同花（目前還沒有）的補牌，回傳 {"順子": [...], "同花": [...]}；河牌後沒有補牌。"""
    if len(board) not in (3, 4):
        return {}
    now = evaluate(hole + board)[0]
    result = {"順子": [], "同花": []}
    known = set(hole) | set(board)
    for card in DECK:
        if card in known:
            continue
        cat = evaluate(hole + board + [card])[0]
        if cat in (5, 8) and now < 5:
            result["同花"].append(card)
        elif cat == 4 and now < 4:
            result["順子"].append(card)
    return result
