# -*- coding: utf-8 -*-
"""抓取双色球 / 大乐透历史开奖数据，落盘为 data/*.json。

数据源（均为官方公开接口）：
  双色球：中国福利彩票官网 cwl.gov.cn
  大乐透：中国体育彩票官网 sporttery.cn
"""
import json
import os
import ssl
import time
import sys
import urllib.request
from datetime import datetime, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
os.makedirs(DATA, exist_ok=True)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def get_json(url, referer, retry=3):
    last = None
    for i in range(retry):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Referer": referer,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
            })
            with urllib.request.urlopen(req, timeout=30, context=CTX) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.2 * (i + 1))
    raise RuntimeError(f"请求失败 {url} -> {last}")


def fetch_ssq(years=5):
    """双色球：按日期区间拉取，每页 100 期。"""
    end = datetime.now()
    start = end - timedelta(days=365 * years + 10)
    out, page, total = [], 1, None
    while True:
        url = ("https://www.cwl.gov.cn/cwl_admin/front/cwlkj/search/kjxx/findDrawNotice"
               f"?name=ssq&issueCount=&issueStart=&issueEnd="
               f"&dayStart={start:%Y-%m-%d}&dayEnd={end:%Y-%m-%d}"
               f"&pageNo={page}&pageSize=100&week=&systemType=PC")
        js = get_json(url, "https://www.cwl.gov.cn/")
        res = js.get("result") or []
        total = js.get("total", 0)
        for it in res:
            reds = [int(x) for x in it["red"].split(",")]
            blues = [int(x) for x in it["blue"].split(",") if x.strip()]
            out.append({
                "issue": it["code"],
                "date": it["date"].split("(")[0],
                "front": sorted(reds),
                "back": blues,
            })
        if len(res) < 100 or len(out) >= total:
            break
        page += 1
        time.sleep(0.4)
    out.sort(key=lambda x: x["date"])
    return out


def fetch_dlt(years=5):
    """大乐透：gameNo=85 分页拉取，直到覆盖 5 年。"""
    cutoff = (datetime.now() - timedelta(days=365 * years + 10)).strftime("%Y-%m-%d")
    out, page = [], 1
    while True:
        url = ("https://webapi.sporttery.cn/gateway/lottery/getHistoryPageListV1.qry"
               f"?gameNo=85&provinceId=0&pageSize=100&isVerify=1&pageNo={page}")
        js = get_json(url, "https://static.sporttery.cn/")
        val = js.get("value") or {}
        lst = val.get("list") or []
        if not lst:
            break
        stop = False
        for it in lst:
            d = it["lotteryDrawTime"]
            if d < cutoff:
                stop = True
                continue
            nums = [int(x) for x in it["lotteryDrawResult"].split()]
            out.append({
                "issue": it["lotteryDrawNum"],
                "date": d,
                "front": sorted(nums[:5]),
                "back": sorted(nums[5:7]),
            })
        if stop:
            break
        page += 1
        time.sleep(0.4)
    out.sort(key=lambda x: x["date"])
    return out


def main():
    result = {}
    for key, fn, label in (("ssq", fetch_ssq, "双色球"), ("dlt", fetch_dlt, "大乐透")):
        try:
            rows = fn()
            path = os.path.join(DATA, f"{key}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"game": label, "updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
                           "count": len(rows), "draws": rows}, f, ensure_ascii=False)
            result[key] = {"ok": True, "count": len(rows),
                           "latest": rows[-1]["issue"] if rows else None}
        except Exception as e:  # noqa: BLE001
            result[key] = {"ok": False, "error": str(e)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if all(v.get("ok") for v in result.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
