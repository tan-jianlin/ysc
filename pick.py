# -*- coding: utf-8 -*-
"""选号与胆拖建议。

选号原则：受历史分布约束的随机抽样，**不是预测**。历史频次只用来调整
抽样权重（向均衡回归），不构成任何提高中奖概率的依据。

随机源可复现：seed = int(YYYYMMDD) * 10 + 彩种序号（SSQ=1, DLT=2）。
同一天同一彩种重复运行结果完全一致，跨天必然不同。
"""
import datetime
import math
import random
from bisect import bisect_right
from itertools import accumulate

# 彩种序号，参与 seed 计算
GAME_INDEX = {"ssq": 1, "dlt": 2}
# 排除上期时给 seed 加的偏移，使两种号码可复现地不同
EXCL_OFFSET = 7777

# 开奖日（0=周一）
DRAW_DAYS = {"ssq": (1, 3, 6), "dlt": (0, 2, 5)}
DRAW_HINT = {"ssq": "每周二 / 周四 / 周日 21:15 开奖",
             "dlt": "每周一 / 周三 / 周六 开奖"}
DAY_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

STRATEGY = "均衡回归 · 结构约束"
MAX_RETRY = 200

# 胆拖模板：胆码个数、拖码个数、后区取球数
DANTUO_TPL = {
    "ssq": {"dan": 2, "tuo": 6, "back": 1},
    "dlt": {"dan": 2, "tuo": 5, "back": 2},
}


# --------------------------------------------------------------------------
# 组合与日期工具
# --------------------------------------------------------------------------
def comb(n, k):
    """组合数 C(n,k)，非法输入返回 0。"""
    if k < 0 or n < 0 or k > n:
        return 0
    k = min(k, n - k)
    out = 1
    for i in range(1, k + 1):
        out = out * (n - k + i) // i
    return out


def make_seed(date_str, game_key):
    """seed = int(YYYYMMDD) * 10 + 彩种序号。"""
    return int(date_str.replace("-", "")) * 10 + GAME_INDEX[game_key]


def next_issue(latest_issue):
    """在最新期号上顺延一期（只处理序号 +1，跨年重置不在本项目范围内）。"""
    s = str(latest_issue)
    idx = len(s)
    while idx > 0 and s[idx - 1].isdigit():
        idx -= 1
    head, tail = s[:idx], s[idx:]
    if not tail:
        return str(int(s) + 1)
    return head + str(int(tail) + 1).zfill(len(tail))


def next_draw_date(today, game_key, last_date=None):
    """推算下一期开奖日。

    以「今天」与「上期开奖日 + 1 天」中较晚者为起点，避免把已经开过的
    当天重复算作下一期。
    """
    days = DRAW_DAYS[game_key]
    start = today
    if last_date:
        start = max(today, last_date + datetime.timedelta(days=1))
    for offset in range(8):
        d = start + datetime.timedelta(days=offset)
        if d.weekday() in days:
            return d
    return start


# --------------------------------------------------------------------------
# 加权抽样
# --------------------------------------------------------------------------
def build_weights(freq, n):
    """w[i] = 1 / (1 + z^2)，z 为频次的标准分。

    越偏离均值的号码权重越低，等价于向均衡回归；权重差异有限，
    随机性不会被消除。
    """
    vals = freq[1:n + 1]
    mean = sum(vals) / float(n)
    var = sum((v - mean) ** 2 for v in vals) / float(n)
    std = math.sqrt(var)
    out = {}
    for i, v in enumerate(vals):
        z = (v - mean) / (std + 1e-9)
        out[i + 1] = 1.0 / (1.0 + z * z)
    return out, mean


def sample_weighted(rng, weights, k):
    """按权重无放回抽取 k 个号码，返回升序列表。"""
    nums = sorted(weights.keys())
    wts = [weights[v] for v in nums]
    out = []
    for _ in range(k):
        if not nums:
            break
        cum = list(accumulate(wts))
        total = cum[-1]
        idx = bisect_right(cum, rng.random() * total)
        if idx >= len(nums):
            idx = len(nums) - 1
        out.append(nums.pop(idx))
        wts.pop(idx)
    return sorted(out)


def structure_ok(nums, cfg, p5, p95):
    """结构约束复核：非全奇全偶、三区非空、和值落在 5%~95% 分位内。"""
    odd = sum(1 for v in nums if v % 2 == 1)
    if odd == 0 or odd == len(nums):
        return False
    for lo, hi in cfg["zones"]:
        if not any(lo <= v <= hi for v in nums):
            return False
    s = sum(nums)
    if p5 is not None and s < p5:
        return False
    if p95 is not None and s > p95:
        return False
    return True


def zone_profile(nums, cfg):
    """返回 (奇数个数, 三区计数列表, 和值)。"""
    odd = sum(1 for v in nums if v % 2 == 1)
    zones = [sum(1 for v in nums if lo <= v <= hi) for lo, hi in cfg["zones"]]
    return odd, zones, sum(nums)


# --------------------------------------------------------------------------
# 单彩种选号
# --------------------------------------------------------------------------
def pick_game(game_key, stats, today=None, exclude_last=False):
    """生成一个彩种的今日号码、依据、胆拖建议。

    Args:
      game_key: 'ssq' 或 'dlt'
      stats: analyze.analyze_game 的返回值
      today: datetime.date，默认取系统当天
      exclude_last: 若为 True，生成时剔除上一期开出的全部号码
        （双色球 6+1、大乐透 5+2，各 7 个）。仅改变候选号码外观，
        不提高中奖概率——开奖独立，上期号码不影响本期。
    """
    today = today or datetime.date.today()
    date_str = today.strftime("%Y%m%d")
    # 排除上期时换一个偏移 seed，保证与常规号码可复现地不同
    seed = make_seed(date_str, game_key) + (EXCL_OFFSET if exclude_last else 0)
    rng = random.Random(seed)

    front = stats["front"]
    back = stats["back"]
    cfg = {
        "n": front["n"],
        "pick": front["pick"],
        "zones": [(z["lo"], z["hi"]) for z in front["zones"]],
    }

    fw, mean_freq = build_weights(front["freq"], front["n"])
    bw, _ = build_weights(back["freq"], back["n"])
    if exclude_last:
        # 杀号：剔除上一期开出的全部 7 个号码（红球/前区 + 蓝球/后区）
        kill = set(int(v) for v in stats["latest"]["front"]) | \
               set(int(v) for v in stats["latest"]["back"])
        fw = {k: w for k, w in fw.items() if k not in kill}
        bw = {k: w for k, w in bw.items() if k not in kill}
    p5 = front["sumDist"]["p5"]
    p95 = front["sumDist"]["p95"]

    picked, tries = None, 0
    for _ in range(MAX_RETRY):
        tries += 1
        cand = sample_weighted(rng, fw, cfg["pick"])
        if structure_ok(cand, cfg, p5, p95):
            picked = cand
            break
    if picked is None:  # 超限则接受最后一次抽样
        picked = sample_weighted(rng, fw, cfg["pick"])

    back_nums = sample_weighted(rng, bw, back["pick"])

    odd, zones, ssum = zone_profile(picked, cfg)
    high = [v for v in picked if front["freq"][v] >= mean_freq * 1.1]
    low = [v for v in picked if front["freq"][v] < mean_freq]

    reasons = [
        "%d 个号码历史出现次数不低于均值 %.1f 的 1.1 倍（高频），"
        "%d 个低于均值，权重按 1/(1+z²) 向均衡回归。"
        % (len(high), mean_freq, len(low)),
        "结构复核通过：奇偶 %d:%d，三区 %s，和值 %d 落在历史 5%%~95%% 区间 %d~%d。"
        % (odd, cfg["pick"] - odd, "-".join(str(z) for z in zones), ssum,
           round(p5 or ssum), round(p95 or ssum)),
        "随机源 seed=%d（日期×10+彩种序号），同日可复现；抽样与开奖结果无关，"
        "不含预测成分。" % seed,
    ]

    dantuo = build_dantuo(game_key, fw, back_nums, cfg, stats)

    issue = stats["latest"]["issue"]
    try:
        last_date = datetime.datetime.strptime(stats["latest"]["date"], "%Y-%m-%d").date()
    except (KeyError, ValueError):
        last_date = None
    nxt = next_draw_date(today, game_key, last_date)
    return {
        "key": game_key,
        "game": stats["game"],
        "date": today.strftime("%Y-%m-%d"),
        "seed": seed,
        "front": picked,
        "back": back_nums,
        "strategy": STRATEGY,
        "retries": tries - 1,
        "profile": {"odd": odd, "even": cfg["pick"] - odd,
                    "zones": zones, "sum": ssum},
        "reasons": reasons,
        "nextIssue": next_issue(issue),
        "lastIssue": issue,
        "lastDraw": last_draw_block(game_key, stats, picked, back_nums, today),
        "drawHint": DRAW_HINT[game_key],
        "nextDrawDate": nxt.strftime("%Y-%m-%d"),
        "nextDrawWeekday": DAY_CN[nxt.weekday()],
        "dantuo": dantuo,
    }


def build_dantuo(game_key, weights, back_nums, cfg, stats):
    """胆拖建议：胆码取权重最高者，拖码取次高者。"""
    tpl = DANTUO_TPL[game_key]
    ranked = sorted(weights.keys(), key=lambda v: (-weights[v], v))
    dan = sorted(ranked[:tpl["dan"]])
    tuo = sorted(ranked[tpl["dan"]:tpl["dan"] + tpl["tuo"]])
    need = cfg["pick"] - len(dan)
    bets = comb(len(tuo), need)
    back_pick = stats["back"]["pick"]
    back_sel = sorted(back_nums[:tpl["back"]])
    if back_pick > 1:
        bets *= comb(len(back_sel), back_pick)
    return {
        "dan": dan,
        "tuo": tuo,
        "back": back_sel,
        "betLines": comb(len(tuo), need),
        "bets": bets,
        "cost": bets * 2,
        "formula": "C(%d,%d)=%d" % (len(tuo), need, comb(len(tuo), need)),
        "note": ("胆码必选，拖码中任选 %d 个与胆码组合；全部组合覆盖，"
                 "不提高单注中奖概率。" % need),
    }


# --------------------------------------------------------------------------
# 蒙特卡洛：随机选号 vs 热号加权，比较命中的前区号码个数分布
# --------------------------------------------------------------------------
SIM_SEED = 20261002  # 展示用固定种子，保证同一天多次构建结果一致


def simulate(stats, trials=20000, seed=SIM_SEED):
    """模拟两种选号策略命中个数的分布。

    每次试验抽一期"开奖"，再用两种策略各抽一张票，统计命中的前区号码个数。
    开奖结果在同一次试验里被两种策略共用（配对比较），降低抽样噪声。
    两种策略的命中数分布理论上完全相同，观察到的差异只来自随机波动。
    """
    front = stats["front"]
    n, pick = front["n"], front["pick"]
    pool = list(range(1, n + 1))
    rng = random.Random(seed)
    fw, _ = build_weights(front["freq"], n)
    hot_nums = sorted(fw.keys())
    hot_wts = [fw[v] for v in hot_nums]

    dist_random = [0] * (pick + 1)
    dist_hot = [0] * (pick + 1)

    for _ in range(trials):
        win = set(rng.sample(pool, pick))
        ticket = rng.sample(pool, pick)
        dist_random[len(win & set(ticket))] += 1

        nums = list(hot_nums)
        wts = list(hot_wts)
        ticket_hot = []
        for _ in range(pick):
            cum = list(accumulate(wts))
            idx = bisect_right(cum, rng.random() * cum[-1])
            if idx >= len(nums):
                idx = len(nums) - 1
            ticket_hot.append(nums.pop(idx))
            wts.pop(idx)
        dist_hot[len(win & set(ticket_hot))] += 1

    def _pct(arr):
        return [round(100.0 * c / trials, 2) for c in arr]

    pct_random = _pct(dist_random)
    pct_hot = _pct(dist_hot)
    gap = round(max(abs(a - b) for a, b in zip(pct_random, pct_hot)), 2)
    # 期望是否相同是理论结论（任一票的中奖个数服从同一超几何分布），
    # 不随本次抽样的噪声大小而改变；gap 只作为噪声量级展示。
    if gap < 1.0:
        conclusion = ("两种策略的分布差异 %.2f 个百分点，小于 1 个百分点："
                      "任何选号策略的期望值相同。" % gap)
    else:
        conclusion = ("两种策略的分布差异 %.2f 个百分点，仍在抽样噪声范围内"
                      "（%d 次试验的抖动约 %s 个百分点）：任何选号策略的期望值相同。"
                      % (gap, trials, _noise(trials)))
    return {
        "trials": trials,
        "pick": pick,
        "labels": ["命中 %d 个" % i for i in range(pick + 1)],
        "random": pct_random,
        "hot": pct_hot,
        "countsRandom": dist_random,
        "countsHot": dist_hot,
        "maxGap": gap,
        "sameExpectation": True,
        "conclusion": conclusion,
    }


def _noise(trials):
    """单个百分比分箱的抽样标准误上限（p=0.5 时最坏），用于给差异值一个参照。"""
    return "%.2f" % (100.0 * (0.25 / trials) ** 0.5)


# --------------------------------------------------------------------------
# 官方奖级规则（来源：中国福彩网 cwl.gov.cn / 中国体彩网 lottery.gov.cn）
# 奖金为税前金额；浮动奖按当期销量与实际中奖注数分配。
# --------------------------------------------------------------------------
PRIZE_RULES = {
    "ssq": {
        "levels": 6,
        "drawHint": "每周二 / 周四 / 周日 21:15 开奖",
        "tiers": [
            {"name": "一等奖", "cond": "6 个红球 + 蓝球", "amount": "浮动",
             "note": "单注封顶 500 万元"},
            {"name": "二等奖", "cond": "6 个红球", "amount": "浮动", "note": ""},
            {"name": "三等奖", "cond": "5 个红球 + 蓝球", "amount": "3,000 元", "note": ""},
            {"name": "四等奖", "cond": "5 个红球，或 4 红 + 蓝球", "amount": "200 元", "note": ""},
            {"name": "五等奖", "cond": "4 个红球，或 3 红 + 蓝球", "amount": "10 元", "note": ""},
            {"name": "六等奖", "cond": "命中蓝球（2+1 / 1+1 / 0+1）", "amount": "5 元", "note": ""},
        ],
        "dantuo": "胆码 1-5 个，且 胆码 + 拖码 ≥ 7",
    },
    "dlt": {
        "levels": 9,
        "drawHint": "每周一 / 周三 / 周六 开奖",
        "tiers": [
            {"name": "一等奖", "cond": "5 个前区 + 2 个后区", "amount": "浮动", "note": ""},
            {"name": "二等奖", "cond": "5 个前区 + 1 个后区", "amount": "浮动", "note": ""},
            {"name": "三等奖", "cond": "5 个前区", "amount": "10,000 元", "note": ""},
            {"name": "四等奖", "cond": "4 个前区 + 2 个后区", "amount": "3,000 元", "note": ""},
            {"name": "五等奖", "cond": "4 个前区 + 1 个后区", "amount": "300 元", "note": ""},
            {"name": "六等奖", "cond": "3 个前区 + 2 个后区", "amount": "200 元", "note": ""},
            {"name": "七等奖", "cond": "4 个前区", "amount": "100 元", "note": ""},
            {"name": "八等奖", "cond": "3 个前区 + 1 个后区，或 2 前区 + 2 后区",
             "amount": "15 元", "note": ""},
            {"name": "九等奖", "cond": "3 个前区 / 1 前区 + 2 后区 / 2 前区 + 1 后区 / 0 前区 + 2 后区",
             "amount": "5 元", "note": ""},
        ],
        "dantuo": "前区胆码 1-4 个、胆+拖 ≥ 6；后区胆码 0-1 个、胆+拖 ≥ 2",
    },
}

# 命中数 → 奖级的结构化判定表：(前区命中, 后区命中, 奖级名, 固定奖金/None=浮动)
# 顺序即判定优先级；每个 (前区, 后区) 组合只对应一个奖级。
PRIZE_HIT = {
    "ssq": (
        (6, 1, "一等奖", None), (6, 0, "二等奖", None),
        (5, 1, "三等奖", 3000),
        (5, 0, "四等奖", 200), (4, 1, "四等奖", 200),
        (4, 0, "五等奖", 10), (3, 1, "五等奖", 10),
        (2, 1, "六等奖", 5), (1, 1, "六等奖", 5), (0, 1, "六等奖", 5),
    ),
    "dlt": (
        (5, 2, "一等奖", None), (5, 1, "二等奖", None), (5, 0, "三等奖", 10000),
        (4, 2, "四等奖", 3000), (4, 1, "五等奖", 300), (3, 2, "六等奖", 200),
        (4, 0, "七等奖", 100), (3, 1, "八等奖", 15), (2, 2, "八等奖", 15),
        (3, 0, "九等奖", 5), (2, 1, "九等奖", 5), (1, 2, "九等奖", 5),
        (0, 2, "九等奖", 5),
    ),
}

# 奖金分配比例（两个彩种相同）
PAYBACK = 0.51          # 返奖率 51% = 49% 当期奖金 + 2% 调节基金
PAYBACK_POOL = 0.49
PAYBACK_ADJUST = 0.02
ISSUE_FEE = 0.13        # 发行费
WELFARE = 0.36          # 公益金


def prize_rules(game_key):
    """返回该彩种的奖级规则（供前端表格渲染）。"""
    cfg = PRIZE_RULES[game_key]
    return {
        "levels": cfg["levels"],
        "drawHint": cfg["drawHint"],
        "tiers": cfg["tiers"],
        "dantuo": cfg["dantuo"],
        "paybackPct": round(PAYBACK * 100),
        "poolPct": round(PAYBACK_POOL * 100),
        "adjustPct": round(PAYBACK_ADJUST * 100),
        "issueFeePct": round(ISSUE_FEE * 100),
        "welfarePct": round(WELFARE * 100),
    }


def judge_prize(game_key, hit_front, hit_back):
    """按命中数判定奖级；未中奖返回 None。"""
    for f, b, name, amount in PRIZE_HIT[game_key]:
        if hit_front == f and hit_back == b:
            return {"name": name, "amount": amount, "floating": amount is None}
    return None


def last_draw_block(game_key, stats, picked, back_nums, today):
    """最近一期开奖结果，并对照今日号码算出命中数与对应奖级。"""
    lat = stats["latest"]
    front = list(lat["front"])
    back = list(lat["back"])
    try:
        d = datetime.datetime.strptime(lat["date"], "%Y-%m-%d").date()
    except (KeyError, ValueError):
        d = None
    days = (today - d).days if d else None
    if days is None:
        rel = ""
    elif days <= 0:
        rel = "今天"
    elif days == 1:
        rel = "昨天"
    elif days == 2:
        rel = "前天"
    else:
        rel = "%d 天前" % days

    hit_f = len(set(picked) & set(front))
    hit_b = len(set(back_nums) & set(back))
    return {
        "issue": lat["issue"],
        "date": lat["date"],
        "weekday": DAY_CN[d.weekday()] if d else "",
        "daysAgo": days,
        "relative": rel,
        "front": front,
        "back": back,
        "hitFront": hit_f,
        "hitBack": hit_b,
        "frontPick": len(front),
        "backPick": len(back),
        "backLabel": "后区" if game_key == "dlt" else "蓝球",
        "prize": judge_prize(game_key, hit_f, hit_b),
    }


def check_prize_tables():
    """两套奖级表必须自洽：命中表的奖级名集合与展示表完全一致。"""
    for key in ("ssq", "dlt"):
        names_hit = set(t[2] for t in PRIZE_HIT[key])
        tiers = PRIZE_RULES[key]["tiers"]
        names_rule = set(t["name"] for t in tiers)
        assert names_hit == names_rule, (key, "奖级名不一致", names_hit ^ names_rule)
        assert len(tiers) == len(names_rule), (key, "展示表奖级名重复")
        seen = set()
        for f, b, name, _ in PRIZE_HIT[key]:
            assert (f, b) not in seen, (key, "命中组合重复", f, b)
            seen.add((f, b))
            assert f >= 0 and b >= 0, (key, "命中数非法", f, b)


def expected_value(game_key):
    """一等奖概率与投入产出说明（官方规则：返奖率 51%）。"""
    if game_key == "ssq":
        total = comb(33, 6) * 16
    else:
        total = comb(35, 5) * comb(12, 2)
    return {
        "jackpot": total,
        "jackpotText": "1/{:,}".format(total),
        "cost": 2,
        "payback": PAYBACK,
        "paybackPct": round(PAYBACK * 100),
        "evPerBet": round(2 * PAYBACK, 2),
        "netPerBet": round(2 * PAYBACK - 2, 2),
    }


# --------------------------------------------------------------------------
# 批量入口
# --------------------------------------------------------------------------
def pick_all(stats_map, today=None):
    """对两个彩种生成今日号码，并附上模拟与期望值数据。"""
    check_prize_tables()
    today = today or datetime.date.today()
    out = {}
    for key in ("ssq", "dlt"):
        item = pick_game(key, stats_map[key], today)
        # 后端同时产出"排除上期 7 个号"的版本，供今日 Tab 开关直接展示
        excl = pick_game(key, stats_map[key], today, exclude_last=True)
        item["excluded"] = {
            "front": excl["front"],
            "back": excl["back"],
            "seed": excl["seed"],
        }
        item["sim"] = simulate(stats_map[key])
        item["ev"] = expected_value(key)
        item["prize"] = prize_rules(key)
        out[key] = item
    return out


def main():
    import json
    import os
    import analyze

    stats_map = {k: analyze.analyze_game(k) for k in ("ssq", "dlt")}
    res = pick_all(stats_map)
    print(json.dumps({k: {"front": v["front"], "back": v["back"],
                          "issue": v["nextIssue"], "seed": v["seed"],
                          "dantuo": v["dantuo"]} for k, v in res.items()},
                     ensure_ascii=False, indent=2))
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "data", "picks.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, separators=(",", ":"))
    print("已写入 data/picks.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
