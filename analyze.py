# -*- coding: utf-8 -*-
"""统计与均匀性检验。

对双色球 / 大乐透的 front、back 两个区分别计算频次、遗漏、和值、跨度、
奇偶、大小、三区、连号、重号分布，并做均匀性卡方检验。

所有结果写入 data/analysis.json，供 build.py 注入前端。
仅使用标准库；卡方 p 值由正则化不完全伽马函数自实现，不依赖 scipy。
"""
import json
import math
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")

# 彩种配置：号池大小、每期取球数、大小分界、三区切分
GAMES = {
    "ssq": {
        "game": "双色球",
        "front": {"label": "红球", "n": 33, "pick": 6, "small_max": 16,
                  "zones": [(1, 11), (12, 22), (23, 33)]},
        "back": {"label": "蓝球", "n": 16, "pick": 1, "small_max": 8,
                 "zones": [(1, 5), (6, 11), (12, 16)]},
    },
    "dlt": {
        "game": "大乐透",
        "front": {"label": "前区", "n": 35, "pick": 5, "small_max": 17,
                  "zones": [(1, 12), (13, 24), (25, 35)]},
        "back": {"label": "后区", "n": 12, "pick": 2, "small_max": 6,
                 "zones": [(1, 4), (5, 8), (9, 12)]},
    },
}

TREND_LIMIT = 60  # 走势页最多需要 50 期，留一点余量


# --------------------------------------------------------------------------
# 正则化不完全伽马函数：用于卡方分布上尾概率
# --------------------------------------------------------------------------
def _gser(a, x):
    """级数展开求 P(a,x)，适用于 x < a+1。"""
    if x <= 0.0:
        return 0.0
    ap = a
    total = 1.0 / a
    delta = total
    for _ in range(10000):
        ap += 1.0
        delta *= x / ap
        total += delta
        if abs(delta) < abs(total) * 1e-16:
            break
    return total * math.exp(-x + a * math.log(x) - math.lgamma(a))


def _gcf(a, x):
    """连分式（Lentz 修正）求 Q(a,x)，适用于 x >= a+1。"""
    tiny = 1e-300
    b = x + 1.0 - a
    c = 1.0 / tiny
    d = 1.0 / b
    h = d
    for i in range(1, 10000):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < tiny:
            d = tiny
        c = b + an / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-16:
            break
    return math.exp(-x + a * math.log(x) - math.lgamma(a)) * h


def chi2_sf(x, df):
    """卡方分布上尾概率 p = Q(df/2, x/2)。"""
    if df <= 0 or x <= 0.0:
        return 1.0
    a = df / 2.0
    xx = x / 2.0
    if xx < a + 1.0:
        val = 1.0 - _gser(a, xx)
    else:
        val = _gcf(a, xx)
    return max(0.0, min(1.0, val))


def chi2_uniform(freq, pick):
    """均匀性卡方检验。

    Args:
      freq: 长度 n+1 的频次数组，下标即号码，下标 0 不使用。
      pick: 每期取球数。

    Returns:
      dict(chi2, df, p, expected)
    """
    total = sum(freq[1:])
    n = len(freq) - 1
    if total == 0:
        return {"chi2": 0.0, "df": max(n - 1, 1), "p": 1.0, "expected": 0.0}
    draws = total / float(pick)
    expected = draws * pick / float(n)
    chi2 = sum((freq[i] - expected) ** 2 / expected for i in range(1, n + 1))
    df = n - 1
    return {"chi2": round(chi2, 3), "df": df, "p": chi2_sf(chi2, df),
            "expected": round(expected, 3)}


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def _pct(sorted_vals, q):
    """线性插值分位数，sorted_vals 需已升序。"""
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * q
    lo = int(math.floor(k))
    hi = int(math.ceil(k))
    if lo == hi:
        return sorted_vals[lo]
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def _dist_list(counts, total):
    """把计数数组转成 {label, count, pct} 列表。"""
    return [{"label": str(i), "count": c,
             "pct": round(100.0 * c / total, 2) if total else 0.0}
            for i, c in enumerate(counts)]


def _bins(values, width):
    """按固定桶宽生成直方图桶。"""
    if not values:
        return []
    lo = (min(values) // width) * width
    hi = ((max(values) // width) + 1) * width
    buckets = [{"from": s, "to": s + width - 1, "count": 0}
               for s in range(lo, hi, width)]
    for v in values:
        buckets[(v - lo) // width]["count"] += 1
    return buckets


# --------------------------------------------------------------------------
# 单个区（front / back）的统计
# --------------------------------------------------------------------------
def zone_stats(draws, key, cfg):
    """计算一个区的全部统计量。"""
    total = len(draws)
    n = cfg["n"]
    pick = cfg["pick"]

    freq = [0] * (n + 1)
    for d in draws:
        for v in d[key]:
            freq[v] += 1

    rate = [round(f / total, 6) if total else 0.0 for f in freq]
    rate[0] = 0.0

    # 当前遗漏
    omission = [total] * (n + 1)
    for i, d in enumerate(draws):
        for v in d[key]:
            omission[v] = total - 1 - i
    omission[0] = 0

    # 历史最大遗漏（含首次出现前的空档与当前尾部空档）
    last_seen = [-1] * (n + 1)
    max_om = [0] * (n + 1)
    for i, d in enumerate(draws):
        for v in d[key]:
            gap = i - last_seen[v] - 1
            if gap > max_om[v]:
                max_om[v] = gap
            last_seen[v] = i
    for v in range(1, n + 1):
        gap = total - 1 - last_seen[v]
        if gap > max_om[v]:
            max_om[v] = gap
    max_om[0] = 0

    avg_om = [None] * (n + 1)
    for v in range(1, n + 1):
        avg_om[v] = round(total / freq[v] - 1.0, 2) if freq[v] else None

    body = [v for v in range(1, n + 1)]
    hot = sorted(body, key=lambda v: (-freq[v], v))[:6]
    cold = sorted(body, key=lambda v: (freq[v], v))[:6]
    mean_freq = total * pick / float(n)

    def _row(v):
        return {"n": v, "freq": freq[v], "rate": rate[v],
                "omission": omission[v], "avgOmission": avg_om[v],
                "maxOmission": max_om[v]}

    # 每期的和值 / 跨度 / 奇偶 / 大小 / 三区 / 连号
    sums, spans, odds, bigs, conseq = [], [], [], [], []
    zone_counts = {}
    for d in draws:
        nums = sorted(d[key])
        sums.append(sum(nums))
        spans.append(nums[-1] - nums[0])
        odds.append(sum(1 for v in nums if v % 2 == 1))
        bigs.append(sum(1 for v in nums if v > cfg["small_max"]))
        cnt = 0
        for a, b in zip(nums, nums[1:]):
            if b - a == 1:
                cnt += 1
        conseq.append(cnt)
        zkey = []
        for lo, hi in cfg["zones"]:
            zkey.append(sum(1 for v in nums if lo <= v <= hi))
        zone_counts[tuple(zkey)] = zone_counts.get(tuple(zkey), 0) + 1

    # 与上期重号
    repeats = []
    for i in range(1, len(draws)):
        prev = set(draws[i - 1][key])
        repeats.append(sum(1 for v in draws[i][key] if v in prev))

    sum_sorted = sorted(sums)
    sum_dist = {
        "bins": _bins(sums, 5),
        "mean": round(sum(sums) / total, 2) if total else 0.0,
        "p5": _pct(sum_sorted, 0.05),
        "p95": _pct(sum_sorted, 0.95),
        "min": min(sums) if sums else 0,
        "max": max(sums) if sums else 0,
    }

    span_dist = {
        "items": [{"v": v, "count": spans.count(v)}
                  for v in range(min(spans), max(spans) + 1)] if spans else [],
        "mean": round(sum(spans) / total, 2) if total else 0.0,
        "min": min(spans) if spans else 0,
        "max": max(spans) if spans else 0,
    }

    odd_counts = [0] * (pick + 1)
    for v in odds:
        if 0 <= v <= pick:
            odd_counts[v] += 1
    big_counts = [0] * (pick + 1)
    for v in bigs:
        if 0 <= v <= pick:
            big_counts[v] += 1
    conseq_counts = [0] * pick
    for v in conseq:
        if 0 <= v < pick:
            conseq_counts[v] += 1
    repeat_counts = [0] * (pick + 1)
    for v in repeats:
        if 0 <= v <= pick:
            repeat_counts[v] += 1

    patterns = [{"z": list(k), "count": c,
                 "pct": round(100.0 * c / total, 2)}
                for k, c in zone_counts.items()]
    patterns.sort(key=lambda x: (-x["count"], x["z"]))
    # patterns 是去重后的形态列表，每个形态附带出现次数；
    # 求各区出号总数必须乘回 count，否则得到的是"形态数"而不是"号码数"。
    zone_totals = [sum(p["z"][i] * p["count"] for p in patterns)
                   for i in range(len(cfg["zones"]))]

    return {
        "label": cfg["label"],
        "n": n,
        "pick": pick,
        "smallMax": cfg["small_max"],
        "zones": [{"lo": lo, "hi": hi, "label": "%d-%d" % (lo, hi)}
                  for lo, hi in cfg["zones"]],
        "total": total,
        "freq": freq,
        "rate": rate,
        "omission": omission,
        "avgOmission": avg_om,
        "maxOmission": max_om,
        "meanFreq": round(mean_freq, 3),
        "hot": [_row(v) for v in hot],
        "cold": [_row(v) for v in cold],
        "sumDist": sum_dist,
        "spanDist": span_dist,
        "oddEven": {"items": _dist_list(odd_counts, total),
                    "counts": odd_counts},
        "bigSmall": {"items": _dist_list(big_counts, total),
                     "counts": big_counts, "split": cfg["small_max"]},
        "zones_dist": {"patterns": patterns, "totals": zone_totals},
        "consecutive": {"items": _dist_list(conseq_counts, len(draws)),
                        "counts": conseq_counts},
        "repeat": {"items": _dist_list(repeat_counts, max(len(draws) - 1, 1)),
                   "counts": repeat_counts},
        "chi2": chi2_uniform(freq, pick),
    }


def trend(draws, cfg, limit=TREND_LIMIT):
    """走势图所需的最近 limit 期数据，以及连续命中的竖线段。"""
    rows = draws[-limit:]
    series = []
    for d in rows:
        nums = sorted(d["front"])
        series.append({
            "issue": d["issue"],
            "date": d["date"],
            "front": nums,
            "sum": sum(nums),
            "odd": sum(1 for v in nums if v % 2 == 1),
        })
    links = {}
    for num in range(1, cfg["front"]["n"] + 1):
        hits = [i for i, r in enumerate(series) if num in r["front"]]
        runs = []
        if hits:
            start = prev = hits[0]
            for h in hits[1:]:
                if h == prev + 1:
                    prev = h
                else:
                    if prev - start + 1 >= 2:
                        runs.append([start, prev])
                    start = prev = h
            if prev - start + 1 >= 2:
                runs.append([start, prev])
        if runs:
            links[str(num)] = runs
    return {"series": series, "links": links,
            "numbers": list(range(1, cfg["front"]["n"] + 1))}


# --------------------------------------------------------------------------
# 读盘 / 组装
# --------------------------------------------------------------------------
def load_raw(game_key):
    """读取 data/<game>.json，返回原始字典。"""
    path = os.path.join(DATA_DIR, "%s.json" % game_key)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def analyze_game(game_key, raw=None):
    """对单个彩种做完整分析。"""
    cfg = GAMES[game_key]
    if raw is None:
        raw = load_raw(game_key)
    draws = raw["draws"]
    if not draws:
        raise ValueError("%s 无开奖数据" % game_key)
    return {
        "key": game_key,
        "game": raw.get("game", cfg["game"]),
        "updated": raw.get("updated", ""),
        "count": len(draws),
        "latest": draws[-1],
        "front": zone_stats(draws, "front", cfg["front"]),
        "back": zone_stats(draws, "back", cfg["back"]),
        "trend": trend(draws, cfg),
    }


def validate(result):
    """自检：频次总和与 p 值范围。返回 (ok, checks)。"""
    checks = []
    ok = True
    for key in ("ssq", "dlt"):
        if key not in result:
            continue
        g = result[key]
        total = g["count"]
        for zone in ("front", "back"):
            z = g[zone]
            expected = total * z["pick"]
            actual = sum(z["freq"][1:])
            passed = actual == expected
            ok = ok and passed
            checks.append({
                "id": "%s.%s.freq_sum" % (key, zone),
                "desc": "sum(freq) == 期数 x 每期球数",
                "expected": expected, "actual": actual, "pass": passed,
            })
            p = z["chi2"]["p"]
            p_ok = 0.0 <= p <= 1.0
            ok = ok and p_ok
            checks.append({
                "id": "%s.%s.p_range" % (key, zone),
                "desc": "卡方 p 值落在 [0,1]",
                "expected": "[0,1]", "actual": round(p, 8), "pass": p_ok,
            })
            # 三区出号总数必须等于 每期球数 x 期数
            zt = z["zones_dist"]["totals"]
            zt_ok = sum(zt) == expected
            ok = ok and zt_ok
            checks.append({
                "id": "%s.%s.zone_totals" % (key, zone),
                "desc": "sum(三区出号数) == 期数 x 每期球数",
                "expected": expected, "actual": sum(zt), "pass": zt_ok,
            })
    return ok, checks


def run(raws=None):
    """执行全部彩种分析并写入 data/analysis.json。"""
    result = {"meta": {"generated": "", "ok": True, "checks": []}}
    for key in ("ssq", "dlt"):
        result[key] = analyze_game(key, (raws or {}).get(key))
    ok, checks = validate(result)
    result["meta"] = {
        "generated": _now(),
        "ok": ok,
        "checks": checks,
        "counts": {"ssq": result["ssq"]["count"], "dlt": result["dlt"]["count"]},
    }
    out = os.path.join(DATA_DIR, "analysis.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, separators=(",", ":"))
    return result


def _now():
    import datetime
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def main():
    result = run()
    meta = result["meta"]
    print("=" * 56)
    print("统计自检")
    print("=" * 56)
    for c in meta["checks"]:
        flag = "PASS" if c["pass"] else "FAIL"
        print("  [%s] %-26s expected=%s actual=%s"
              % (flag, c["id"], c["expected"], c["actual"]))
    for key in ("ssq", "dlt"):
        g = result[key]
        f, b = g["front"], g["back"]
        print("-" * 56)
        print("%s  共 %d 期  最新 %s" % (g["game"], g["count"], g["latest"]["issue"]))
        print("  %s 卡方: chi2=%.3f df=%d p=%.4g -> %s"
              % (f["label"], f["chi2"]["chi2"], f["chi2"]["df"], f["chi2"]["p"],
                 "与均匀分布无显著差异" if f["chi2"]["p"] > 0.05 else "偏离均匀分布"))
        print("  %s 卡方: chi2=%.3f df=%d p=%.4g"
              % (b["label"], b["chi2"]["chi2"], b["chi2"]["df"], b["chi2"]["p"]))
        print("  和值均值 %.2f  (5%%~95%% 区间 %.0f ~ %.0f)"
              % (f["sumDist"]["mean"], f["sumDist"]["p5"], f["sumDist"]["p95"]))
        print("  热号 %s   冷号 %s"
              % (" ".join("%02d" % h["n"] for h in f["hot"]),
                 " ".join("%02d" % h["n"] for h in f["cold"])))
    print("=" * 56)
    if not meta["ok"]:
        print("自检失败", file=sys.stderr)
        return 1
    print("自检通过，已写入 data/analysis.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
