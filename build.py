# -*- coding: utf-8 -*-
"""构建入口：抓取 -> 分析 -> 选号 -> 注入模板 -> 产出 dist/index.html。

用法:
  python build.py                 # 先尝试刷新数据，失败则沿用本地缓存
  python build.py --no-fetch      # 跳过抓取，直接用本地缓存构建
  python build.py --fetch-timeout 30
"""
import argparse
import contextlib
import io
import json
import os
import sys
import threading
import time
from datetime import datetime

import analyze
import fetch_data
import guard
import pick

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")
DIST_DIR = os.path.join(BASE, "dist")
TEMPLATE = os.path.join(BASE, "template.html")
OUTPUT = os.path.join(DIST_DIR, "index.html")
PLACEHOLDER = "/*__LOTTERY_DATA__*/"


# --------------------------------------------------------------------------
# 数据加载 / 抓取
# --------------------------------------------------------------------------
def load_snapshot():
    """把本地 data/*.json 读进内存，作为抓取失败时的兜底。"""
    snap = {}
    for key in ("ssq", "dlt"):
        path = os.path.join(DATA_DIR, "%s.json" % key)
        try:
            with open(path, "r", encoding="utf-8") as f:
                snap[key] = json.load(f)
        except Exception as e:  # noqa: BLE001
            raise SystemExit("缺少可用的本地数据 %s: %s" % (path, e))
    return snap


def _parse_fetch_report(text):
    """解析 fetch_data.main() 打印的 JSON 报告。"""
    text = (text or "").strip()
    if not text:
        return None
    start = text.find("{")
    if start < 0:
        return None
    try:
        return json.loads(text[start:])
    except Exception:  # noqa: BLE001
        return None


def try_fetch(timeout=90):
    """在限定时间内尝试抓取，超时或失败均不中断构建。

    Returns:
      (fresh: dict[str,bool], report: dict|None)
    """
    fresh = {"ssq": False, "dlt": False}
    buf = io.StringIO()
    box = {"code": 1}

    def worker():
        try:
            with contextlib.redirect_stdout(buf):
                box["code"] = fetch_data.main()
        except BaseException:  # noqa: BLE001
            box["code"] = 1

    th = threading.Thread(target=worker, name="fetch", daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        print("抓取超时（%d 秒），沿用本地缓存。" % timeout)
        return fresh, None

    report = _parse_fetch_report(buf.getvalue())
    if isinstance(report, dict):
        for key in ("ssq", "dlt"):
            fresh[key] = bool((report.get(key) or {}).get("ok"))
    return fresh, report


def restore_if_broken(snapshot):
    """抓取线程被超时打断时可能留下半个文件，坏了就用内存快照补回。"""
    for key, raw in snapshot.items():
        path = os.path.join(DATA_DIR, "%s.json" % key)
        try:
            with open(path, "r", encoding="utf-8") as f:
                json.load(f)
        except Exception:  # noqa: BLE001
            with open(path, "w", encoding="utf-8") as f:
                json.dump(raw, f, ensure_ascii=False)
            print("检测到 %s.json 损坏，已用内存快照恢复。" % key)


# --------------------------------------------------------------------------
# 组装注入数据
# --------------------------------------------------------------------------
def build_payload(stats, picks, stale, generated, guard_data, raws):
    payload = {
        "updated": stats["ssq"].get("updated") or "",
        "ssq": stats["ssq"],
        "dlt": stats["dlt"],
        "today": picks,
        "guard": guard_data,
        "meta": {
            "generated": generated,
            "ok": stats["meta"]["ok"],
            "checks": list(stats["meta"]["checks"]),
            "counts": stats["meta"]["counts"],
            "stale": stale,
            "staleAny": bool(stale.get("ssq") or stale.get("dlt")),
            "updated": {"ssq": stats["ssq"].get("updated", ""),
                        "dlt": stats["dlt"].get("updated", "")},
        },
    }
    payload["ssq"]["stale"] = bool(stale.get("ssq"))
    payload["dlt"]["stale"] = bool(stale.get("dlt"))
    # 历史开奖明细：历史查询 / 中奖核对 需要全量期号
    payload["ssq"]["draws"] = raws["ssq"]["draws"]
    payload["dlt"]["draws"] = raws["dlt"]["draws"]
    return payload


def check_last_draw(payload):
    """上期开奖块必须与源数据同源：期号、号码、命中数逐项核对。"""
    out = payload["meta"]["checks"]
    ok = True
    for key in ("ssq", "dlt"):
        ld = payload["today"][key]["lastDraw"]
        latest = payload[key]["latest"]
        t = payload["today"][key]
        pairs = [
            ("issue", latest["issue"], ld["issue"]),
            ("front", latest["front"], ld["front"]),
            ("back", latest["back"], ld["back"]),
            ("hitFront", len(set(t["front"]) & set(ld["front"])), ld["hitFront"]),
            ("hitBack", len(set(t["back"]) & set(ld["back"])), ld["hitBack"]),
        ]
        for name, want, got in pairs:
            passed = want == got
            ok = ok and passed
            out.append({"id": "%s.lastDraw.%s" % (key, name),
                        "desc": "上期开奖 %s 与源数据一致" % name,
                        "expected": want, "actual": got, "pass": passed})
    return ok


def inject(payload, template_text):
    """把数据塞进模板占位符，产出单文件 HTML。"""
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    # 防止任何字符串意外闭合 script 标签
    text = text.replace("</", "<\\/")
    if PLACEHOLDER not in template_text:
        raise RuntimeError("模板中找不到占位符 %s" % PLACEHOLDER)
    return template_text.replace(PLACEHOLDER, text)


# --------------------------------------------------------------------------
# 人类可读摘要（每日定时任务读取）
# --------------------------------------------------------------------------
def summary(payload, size):
    meta = payload["meta"]
    lines = []
    lines.append("=" * 62)
    lines.append("乐透数据观察  构建于 %s" % meta["generated"])
    lines.append("=" * 62)
    for key in ("ssq", "dlt"):
        t = payload["today"][key]
        g = payload[key]
        f = g["front"]["chi2"]
        flag = "（数据未刷新，沿用缓存）" if g["stale"] else ""
        lines.append("")
        lines.append("%s  第 %s 期  预计 %s %s%s"
                     % (g["game"], t["nextIssue"], t["nextDrawDate"],
                        t["nextDrawWeekday"], flag))
        lines.append("  %s：%s" % (g["front"]["label"],
                                   " ".join("%02d" % v for v in t["front"])))
        lines.append("  %s：%s" % (g["back"]["label"],
                                   " ".join("%02d" % v for v in t["back"])))
        ld = t["lastDraw"]
        hit = ("中" + ld["prize"]["name"]) if ld["prize"] else "未中奖"
        lines.append("  上期：%s · %s %s · %s + %s → 今日命中 %d+%d %s"
                     % (ld["issue"], ld["date"], ld["weekday"],
                        " ".join("%02d" % v for v in ld["front"]),
                        " ".join("%02d" % v for v in ld["back"]),
                        ld["hitFront"], ld["hitBack"], hit))
        lines.append("  策略：%s（seed=%d）" % (t["strategy"], t["seed"]))
        lines.append("  和值 %d · 奇偶 %d:%d · 三区 %s"
                     % (t["profile"]["sum"], t["profile"]["odd"],
                        t["profile"]["even"], "-".join(str(z) for z in t["profile"]["zones"])))
        lines.append("  胆拖：胆 %s / 拖 %s / %s 注 %d 元"
                     % (" ".join("%02d" % v for v in t["dantuo"]["dan"]),
                        " ".join("%02d" % v for v in t["dantuo"]["tuo"]),
                        t["dantuo"]["bets"], t["dantuo"]["cost"]))
        lines.append("  卡方：chi2=%.3f df=%d p=%.4g（%d 期，%s）"
                     % (f["chi2"], f["df"], f["p"], g["count"],
                        "与均匀分布无显著差异" if f["p"] > 0.05 else "偏离均匀分布"))
        gr = payload["guard"][key]
        lines.append("  保底：最便宜 %s 需 %s，保底拿回 %s（最坏回收 %.1f%%）"
                     % (gr["cheapest"]["name"], gr["cheapest"]["costText"],
                        gr["cheapest"]["prizeText"],
                        gr["cheapest"]["prize"] / gr["cheapest"]["cost"] * 100))
        lines.append("  单注：中奖概率 %.4f%%（%s），期望回收 %.2f 元/注"
                     % (gr["single"]["anyWinPct"], gr["single"]["anyWinOdds"],
                        gr["single"]["expectReturn"]))
    lines.append("")
    lines.append("-" * 62)
    lines.append("自检：%s" % ("全部通过" if meta["ok"] else "存在失败项，请检查"))
    for c in meta["checks"]:
        if not c["pass"]:
            lines.append("  FAIL %s expected=%s actual=%s"
                         % (c["id"], c["expected"], c["actual"]))
    lines.append("产物：%s（%.1f KB）" % (OUTPUT, size / 1024.0))
    lines.append("=" * 62)
    return "\n".join(lines)


# --------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="构建单文件乐透数据观察页")
    ap.add_argument("--no-fetch", action="store_true", help="跳过网络抓取")
    ap.add_argument("--fetch-timeout", type=int, default=90,
                    help="抓取总超时秒数，默认 90")
    args = ap.parse_args(argv)

    t0 = time.time()
    os.makedirs(DIST_DIR, exist_ok=True)

    snapshot = load_snapshot()

    stale = {"ssq": True, "dlt": True}
    if args.no_fetch:
        print("已跳过抓取，使用本地缓存。")
    else:
        fresh, report = try_fetch(args.fetch_timeout)
        restore_if_broken(snapshot)
        stale = {k: (not fresh.get(k)) for k in ("ssq", "dlt")}
        if report:
            print("抓取：双色球 %s，大乐透 %s"
                  % ("成功" if fresh["ssq"] else "失败",
                     "成功" if fresh["dlt"] else "失败"))
        else:
            print("抓取未返回有效结果，沿用本地缓存。")

    # 重新读盘：抓取成功时使用新数据，失败时磁盘内容未变
    raws = {}
    for key in ("ssq", "dlt"):
        with open(os.path.join(DATA_DIR, "%s.json" % key), "r", encoding="utf-8") as f:
            raws[key] = json.load(f)

    stats = analyze.run(raws)
    if not stats["meta"]["ok"]:
        for c in stats["meta"]["checks"]:
            if not c["pass"]:
                print("[自检失败] %s expected=%s actual=%s"
                      % (c["id"], c["expected"], c["actual"]), file=sys.stderr)
        print("统计自检未通过，构建中止。", file=sys.stderr)
        return 2

    stats_map = {k: stats[k] for k in ("ssq", "dlt")}
    picks = pick.pick_all(stats_map)

    try:
        guard_data = guard.run()
    except AssertionError as e:  # noqa: BLE001
        print("保底计算自检未通过：%s" % (e,), file=sys.stderr)
        return 3

    generated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    payload = build_payload(stats, picks, stale, generated, guard_data, raws)
    if not check_last_draw(payload):
        for c in payload["meta"]["checks"]:
            if not c["pass"]:
                print("[自检失败] %s expected=%s actual=%s"
                      % (c["id"], c["expected"], c["actual"]), file=sys.stderr)
        print("上期开奖数据自检未通过，构建中止。", file=sys.stderr)
        return 4

    with open(TEMPLATE, "r", encoding="utf-8") as f:
        template_text = f.read()
    html = inject(payload, template_text)
    # 固定 LF 换行，保证不同平台产出一致
    with open(OUTPUT, "w", encoding="utf-8", newline="\n") as f:
        f.write(html)

    size = os.path.getsize(OUTPUT)
    print(summary(payload, size))
    print("耗时 %.2f 秒" % (time.time() - t0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
