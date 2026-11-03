#!/usr/bin/env python3
"""从 source_pool.json 重建 config，保证影视仓源清单始终等于策展种子。
不在 CI 做可达性判定/删源（遵守 2026-10-20 审计结论：美国机房无法判定国内源死活）。
"""
import json
import os

HERE = os.path.abspath(__file__)
# .github/scripts/refresh_config.py -> 上三级即仓库根
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))


def main():
    pool_path = os.path.join(REPO, "source_pool.json")
    with open(pool_path, "r", encoding="utf-8") as f:
        pool = json.load(f)
    aggregators = pool.get("aggregators", [])
    base = pool.get("base", [])
    config = {
        "storeName": pool.get("storeName", "影视仓"),
        "urls": aggregators + base,
    }
    out_path = os.path.join(REPO, "config")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    print(
        f"config 已重建：{len(aggregators)} 个聚合仓 + "
        f"{len(base)} 个原源 = 共 {len(config['urls'])} 个源"
    )


if __name__ == "__main__":
    main()
