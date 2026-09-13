#!/usr/bin/env python3
"""一次性验收服务：向 API 提交多骨料样例，校验并打印湿投料清单与最终加水量。

用法：
    python verify.py                 # 默认打 http://localhost:8000
    API_BASE_URL=http://api:8000 python verify.py

退出码：0 = 验收通过；1 = 验收失败。
"""
from __future__ import annotations

import json
import os
import sys
import time
from decimal import Decimal

import httpx

API_BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:8000").rstrip("/")
URL = f"{API_BASE_URL}/api/v1/correction-sheet"
TIMEOUT = 10.0
RETRY_SECONDS = 60

# 多骨料验收样例（kg，质量百分数）
SAMPLE = {
    "design_water_kg": "180",
    "aggregates": [
        {"name": "河砂A", "dry_mass_kg": "800", "moisture_pct": "5.0", "absorption_pct": "1.0"},
        {"name": "机制砂B", "dry_mass_kg": "600", "moisture_pct": "3.5", "absorption_pct": "0.5"},
        {"name": "石粉", "dry_mass_kg": "200", "moisture_pct": "0.5", "absorption_pct": "0.2"},
    ],
}

Q3 = Decimal("0.001")


def round3(value: Decimal) -> Decimal:
    return value.quantize(Q3)


def expected_sheet(sample: dict) -> dict:
    """用 Decimal 独立复算期望值（与 API 实现互不复用代码）。"""
    items = []
    total_free = Decimal("0")
    total_wet = Decimal("0")
    for i, agg in enumerate(sample["aggregates"]):
        dry = Decimal(agg["dry_mass_kg"])
        wet = dry * (1 + Decimal(agg["moisture_pct"]) / 100)
        free = dry * (Decimal(agg["moisture_pct"]) - Decimal(agg["absorption_pct"])) / 100
        total_free += free
        total_wet += wet
        items.append(
            {
                "index": i,
                "name": agg["name"],
                "dry_mass_kg": f"{round3(dry):f}",
                "wet_mass_kg": f"{round3(wet):f}",
                "free_water_kg": f"{round3(free):f}",
            }
        )
    design = Decimal(sample["design_water_kg"])
    return {
        "items": items,
        "item_count": len(items),
        "total_dry_mass_kg": f"{round3(sum(Decimal(a['dry_mass_kg']) for a in sample['aggregates'])):f}",
        "total_wet_mass_kg": f"{round3(total_wet):f}",
        "total_free_water_kg": f"{round3(total_free):f}",
        "design_water_kg": f"{round3(design):f}",
        "final_water_kg": f"{round3(design - total_free):f}",
    }


def wait_for_api() -> None:
    deadline = time.monotonic() + RETRY_SECONDS
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(f"{API_BASE_URL}/health", timeout=TIMEOUT)
            if resp.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise SystemExit(f"[verify] FAIL: API 在 {RETRY_SECONDS}s 内未就绪（{API_BASE_URL}）")


def main() -> int:
    wait_for_api()
    resp = httpx.post(URL, json=SAMPLE, timeout=TIMEOUT)
    if resp.status_code != 200:
        print(f"[verify] FAIL: HTTP {resp.status_code}: {resp.text}", file=sys.stderr)
        return 1
    actual = resp.json()
    expected = expected_sheet(SAMPLE)
    if actual != expected:
        print("[verify] FAIL: 响应与独立复算结果不一致", file=sys.stderr)
        print("expected:", json.dumps(expected, ensure_ascii=False, indent=2), file=sys.stderr)
        print("actual:  ", json.dumps(actual, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1

    print("[verify] 验收通过 —— 湿投料清单：")
    for item in actual["items"]:
        print(
            f"  [{item['index']}] {item['name']}: "
            f"湿投料 {item['wet_mass_kg']} kg"
            f"（干基 {item['dry_mass_kg']} kg，自由水 {item['free_water_kg']} kg）"
        )
    print(f"[verify] 湿投料合计 {actual['total_wet_mass_kg']} kg，"
          f"自由水量合计 {actual['total_free_water_kg']} kg")
    print(f"[verify] 最终加水量 {actual['final_water_kg']} kg"
          f"（设计加水量 {actual['design_water_kg']} kg）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
