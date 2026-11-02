# -*- coding: utf-8 -*-
"""保底成本：把「保证中某个奖级」换算成最少注数、花费与最坏回收率。

口径（前端文案必须与此一致）
------------------------------------------------------------------
1. 注数一律是**计数法下限**：
       ceil(全部开奖组合数 / 单注最多能覆盖的开奖组合数)
   它是数学下限。真实的覆盖设计（covering design）通常需要更多注，
   所以页面上的注数都带 "≥"。后区部分 j<=每注后区号数 时是精确最小解。
2. 「保底回收率」= 保底那一档的奖金 / 成本，是**最坏情况**。
3. 「期望回收率」恒等于 51%（官方返奖率）。由线性期望：
       N 注的期望回收 = N × (2 × 0.51) = 成本 × 0.51
   与怎么选号、怎么覆盖无关。所以保底只是把波动压到 0，期望一分不变。
   这就是「保底必然亏」的全部原因。
"""
import math

PRICE = 2                     # 每注 2 元
PAYBACK = 0.51                # 官方返奖率：49% 当期奖金 + 2% 调节基金
EV_PER_BET = PRICE * PAYBACK  # 每注期望回收，1.02 元

GAMES = {
    "ssq": {"name": "双色球", "N": 33, "n": 6, "B": 16, "pick": 1},
    "dlt": {"name": "大乐透", "N": 35, "n": 5, "B": 12, "pick": 2},
}

# 奖级：fixed 为固定奖金（元），None 表示浮动；ref 仅用于估算浮动奖的"最坏回收率"
RULES = {
    "ssq": [
        {"name": "一等奖", "cond": "6 红 + 蓝", "fixed": None, "ref": 5000000,
         "hits": [(6, 1)]},
        {"name": "二等奖", "cond": "6 红", "fixed": None, "ref": 200000,
         "hits": [(6, 0)]},
        {"name": "三等奖", "cond": "5 红 + 蓝", "fixed": 3000,
         "hits": [(5, 1)]},
        {"name": "四等奖", "cond": "5 红，或 4 红 + 蓝", "fixed": 200,
         "hits": [(5, 0), (4, 1)]},
        {"name": "五等奖", "cond": "4 红，或 3 红 + 蓝", "fixed": 10,
         "hits": [(4, 0), (3, 1)]},
        {"name": "六等奖", "cond": "命中蓝球（2+1 / 1+1 / 0+1）", "fixed": 5,
         "hits": [(2, 1), (1, 1), (0, 1)]},
    ],
    "dlt": [
        {"name": "一等奖", "cond": "5 前区 + 2 后区", "fixed": None, "ref": 8000000,
         "hits": [(5, 2)]},
        {"name": "二等奖", "cond": "5 前区 + 1 后区", "fixed": None, "ref": 150000,
         "hits": [(5, 1)]},
        {"name": "三等奖", "cond": "5 前区", "fixed": 10000, "hits": [(5, 0)]},
        {"name": "四等奖", "cond": "4 前区 + 2 后区", "fixed": 3000, "hits": [(4, 2)]},
        {"name": "五等奖", "cond": "4 前区 + 1 后区", "fixed": 300, "hits": [(4, 1)]},
        {"name": "六等奖", "cond": "3 前区 + 2 后区", "fixed": 200, "hits": [(3, 2)]},
        {"name": "七等奖", "cond": "4 前区", "fixed": 100, "hits": [(4, 0)]},
        {"name": "八等奖", "cond": "3+1，或 2+2", "fixed": 15,
         "hits": [(3, 1), (2, 2)]},
        {"name": "九等奖", "cond": "3+0、1+2、2+1、0+2", "fixed": 5,
         "hits": [(3, 0), (1, 2), (2, 1), (0, 2)]},
    ],
}

# 保底路径：(前区保中 f 个, 后区保中 j 个)。同一奖级可有多条路径，取注数最少者。
# 每条路径都已人工验证「前区≥f 且 后区≥j ⟹ 至少中该奖级」。
PATHS = {
    "ssq": {
        "六等奖": [(0, 1)],
        "五等奖": [(4, 0), (3, 1)],
        "四等奖": [(5, 0), (4, 1)],
        "三等奖": [(5, 1)],
        "二等奖": [(6, 0)],
        "一等奖": [(6, 1)],
    },
    "dlt": {
        "九等奖": [(3, 0), (2, 1), (1, 2), (0, 2)],
        "八等奖": [(3, 1), (2, 2)],
        "七等奖": [(4, 0)],
        "六等奖": [(3, 2)],
        "五等奖": [(4, 1)],
        "四等奖": [(4, 2)],
        "三等奖": [(5, 0)],
        "二等奖": [(5, 1)],
        "一等奖": [(5, 2)],
    },
}

FRONT_LABEL = {"ssq": ("红球", "蓝球"), "dlt": ("前区", "后区")}


# --------------------------------------------------------------------------
def hit_at_least(N, n, k):
    """单注命中开奖号码里「至少 k 个」的开奖组合数。"""
    if k <= 0:
        return math.comb(N, n)
    total = 0
    for i in range(k, n + 1):
        total += math.comb(n, i) * math.comb(N - n, n - i)
    return total


def front_need(N, n, k):
    """保证前区至少命中 k 个所需注数（计数法下限）。"""
    if k <= 0:
        return 1
    return math.ceil(math.comb(N, n) / hit_at_least(N, n, k))


def back_need(B, pick, j):
    """保证后区至少命中 j 个所需注数。

    j <= pick 时是精确最小解：每注覆盖 C(pick,j) 种 j-子集，共 C(B,j) 种。
    """
    if j <= 0:
        return 1
    return math.ceil(math.comb(B, j) / math.comb(pick, j))


def back_cover(B, pick, j):
    """一注后区能覆盖多少种开奖后区组合（与它至少共享 j 个号）。"""
    if j <= 0:
        return math.comb(B, pick)
    return sum(math.comb(pick, i) * math.comb(B - pick, pick - i)
               for i in range(j, pick + 1))


def tier_counts(g):
    """(命中前区数, 命中后区数) -> 组合数。"""
    N, n, B, pick = g["N"], g["n"], g["B"], g["pick"]
    out = {}
    for r in range(n + 1):
        cf = math.comb(n, r) * math.comb(N - n, n - r)
        for b in range(pick + 1):
            cb = math.comb(pick, b) * math.comb(B - pick, pick - b)
            out[(r, b)] = cf * cb
    return out


def total_combos(g):
    return math.comb(g["N"], g["n"]) * math.comb(g["B"], g["pick"])


def run():
    """返回注入到前端的 guard 数据。"""
    out = {}
    for key, g in GAMES.items():
        N, n, B, pick = g["N"], g["n"], g["B"], g["pick"]
        fname, bname = FRONT_LABEL[key]
        counts = tier_counts(g)
        total = total_combos(g)

        # 单注各奖级命中概率
        for t in RULES[key]:
            cnt = sum(counts.get(h, 0) for h in t["hits"])
            t["count"] = cnt
            t["p"] = cnt / total
            t["odds"] = total / cnt if cnt else None
        win_cnt = sum(t["count"] for t in RULES[key])

        tiers = []
        for t in RULES[key]:
            best = None
            for (f, j) in PATHS[key][t["name"]]:
                fn = front_need(N, n, f)
                bn = back_need(B, pick, j)
                need = fn * bn
                if best is None or need < best["need"]:
                    best = {"need": need, "f": f, "j": j,
                            "fn": fn, "bn": bn}
            need = best["need"]
            cost = need * PRICE
            # 自检：这条路径买下去，确实覆盖了整个组合空间
            covered = hit_at_least(N, n, best["f"]) * back_cover(B, pick, best["j"]) * need
            assert covered >= total, (key, t["name"], best, covered, total)
            if t["fixed"] is not None:
                prize, floor_rate, basis = t["fixed"], t["fixed"] / cost, "固定奖金"
                prize_text = _money(t["fixed"])
                prize_cell, prize_sub = _money(t["fixed"]), ""
            else:
                prize, floor_rate, basis = t["ref"], t["ref"] / cost, "按参考奖金估算"
                prize_text = "浮动（参考 %s）" % _money(t["ref"])
                prize_cell, prize_sub = "浮动", "≈ " + _money(t["ref"])
            tiers.append({
                "name": t["name"],
                "cond": t["cond"],
                "need": need,
                "cost": cost,
                "costText": _money(cost),
                "prize": prize,
                "fixed": t["fixed"],
                "prizeText": prize_text,
                "prizeCell": prize_cell,
                "prizeSub": prize_sub,
                "floorPct": round(floor_rate * 100, 1),
                "basis": basis,
                "path": _path_text(fname, bname, best, pick),
                "pPct": round(t["p"] * 100, 6),
                "odds": _odds(t["odds"]),
                # 只保后区、或前区整区全包时，是精确最小解；其余是计数法下限
                "exact": best["f"] == 0 or best["f"] == n,
            })

        cheapest = min(tiers, key=lambda x: x["cost"])
        # 期望回收恒等式
        expect_ret = round(PRICE * PAYBACK, 2)

        out[key] = {
            "game": g["name"],
            "total": total,
            "frontLabel": fname,
            "backLabel": bname,
            "tiers": tiers,
            "cheapest": {"name": cheapest["name"], "cost": cheapest["cost"],
                         "prize": cheapest["prize"],
                         "costText": _money(cheapest["cost"]),
                         "prizeText": cheapest["prizeText"],
                         "floorPct": cheapest["floorPct"],
                         "need": cheapest["need"]},
            "single": {
                "cost": PRICE,
                "anyWinPct": round(win_cnt / total * 100, 4),
                "anyWinOdds": _odds(total / win_cnt if win_cnt else None),
                "expectReturn": expect_ret,
                "expectLoss": round(PRICE - expect_ret, 2),
                "jackpotOdds": _odds(total),
            },
            "paybackPct": round(PAYBACK * 100),
            "evPerBet": expect_ret,
            "tiersTitle": "%s 保底价目表" % g["name"],
        }

        # 自检：各奖级组合数必须覆盖全部中奖组合，且不超过总组合
        assert 0 < win_cnt < total, (key, win_cnt, total)
        assert sum(counts.values()) == total, (key, "组合数合计不等于总数")
        # 注数必须随奖级升高而增加（奖级从高到低排列，成本应单调递减）
        costs = [t["cost"] for t in tiers]
        assert costs == sorted(costs, reverse=True), (key, "保底成本非单调", costs)

    out["_meta"] = {
        "price": PRICE,
        "paybackPct": round(PAYBACK * 100),
        "evPerBet": round(PRICE * PAYBACK, 2),
        "note": ("注数为计数法下限（真实覆盖设计通常更多），故一律记作「≥」。"
                 "保底回收率是最坏情况；任何 N 注的期望回收恒为成本的 51%。"),
    }
    return out


def _money(v):
    """金额的人话写法：5 元 / 3,000 元 / 221.5 万元。"""
    v = int(round(v))
    if v < 100000:
        return "{:,} 元".format(v)
    w = v / 10000.0
    if abs(w - round(w)) < 1e-9:
        return "{:,} 万元".format(int(round(w)))
    return "{:.1f} 万元".format(w)


def _odds(v):
    """组合数倒数的可读写法。"""
    if not v:
        return "—"
    if v >= 100:
        return "1/{:,}".format(int(round(v)))
    return "1/%.1f" % v


def _path_text(fname, bname, best, pick):
    """把路径写成一句人话。"""
    f, j, fn, bn = best["f"], best["j"], best["fn"], best["bn"]
    if f == 0:
        return "%s %d 个号全部覆盖，每注 %d 个，共 %d 注" % (
            bname, GAMES["ssq" if fname == "红球" else "dlt"]["B"], pick, bn)
    if j == 0:
        return "%s保证中 %d 个（%d 注）× %s 1 注" % (fname, f, fn, bname)
    return "%s保证中 %d 个（%d 注）× %s保证中 %d 个（%d 注）" % (
        fname, f, fn, bname, j, bn)


if __name__ == "__main__":
    d = run()
    for k in ("ssq", "dlt"):
        g = d[k]
        print("=" * 78)
        print("%s  总组合 %s  单注中奖 %.4f%%（%s）" % (
            g["game"], "{:,}".format(g["total"]),
            g["single"]["anyWinPct"], g["single"]["anyWinOdds"]))
        print("%-7s %-20s %12s %14s %-16s %7s %s" % (
            "奖级", "条件", "最少注数", "花费", "保底拿回", "最坏回收", "构造"))
        for t in g["tiers"]:
            print("%-7s %-20s %s%12s %14s %-16s %6.1f%% %s" % (
                t["name"], t["cond"][:18], "" if t["exact"] else "≥",
                "{:,}".format(t["need"]), _money(t["cost"]),
                t["prizeText"], t["floorPct"], t["path"]))
        print("最便宜保底：%s %s → 拿回 %s" % (
            g["cheapest"]["name"], g["cheapest"]["costText"],
            g["cheapest"]["prizeText"]))
        print("单注期望回收 %.2f 元（净 %.2f 元），一等奖 %s" % (
            g["single"]["expectReturn"], g["single"]["expectLoss"],
            g["single"]["jackpotOdds"]))
