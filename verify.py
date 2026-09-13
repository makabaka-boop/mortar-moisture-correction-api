#!/usr/bin/env python3
"""一次性验收服务：向 API 提交样例，校验并打印湿投料清单与最终加水量。

覆盖两类样例：
1. 未传目标干料总量的多骨料样例 —— 响应须与独立复算完全一致；
2. 传入目标干料总量的缩放样例 —— 以原骨料干基合计为基准同比缩放，
   响应（含三位小数目标与六位缩放系数）同样独立复算比对。

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
from decimal import ROUND_HALF_UP, Decimal

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

# 目标干料总量缩放样例：原干基合计 1600 → 目标 3200，缩放系数 2
SCALED_SAMPLE = {
    **SAMPLE,
    "target_dry_total_kg": "3200",
}

Q3 = Decimal("0.001")
Q6 = Decimal("0.000001")


def round3(value: Decimal) -> Decimal:
    return value.quantize(Q3, rounding=ROUND_HALF_UP)


def round6(value: Decimal) -> Decimal:
    return value.quantize(Q6, rounding=ROUND_HALF_UP)


def expected_sheet(sample: dict) -> dict:
    """用 Decimal 独立复算期望值（与 API 实现互不复用代码）。"""
    target = sample.get("target_dry_total_kg")
    factor = None
    if target is not None:
        base_total = sum(Decimal(a["dry_mass_kg"]) for a in sample["aggregates"])
        factor = Decimal(target) / base_total
    items = []
    total_free = Decimal("0")
    total_wet = Decimal("0")
    total_dry = Decimal("0")
    for i, agg in enumerate(sample["aggregates"]):
        dry = Decimal(agg["dry_mass_kg"])
        if factor is not None:
            dry = dry * factor
        wet = dry * (1 + Decimal(agg["moisture_pct"]) / 100)
        free = dry * (Decimal(agg["moisture_pct"]) - Decimal(agg["absorption_pct"])) / 100
        total_dry += dry
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
    if factor is not None:
        design = design * factor
    body = {
        "items": items,
        "item_count": len(items),
        "total_dry_mass_kg": f"{round3(total_dry):f}",
        "total_wet_mass_kg": f"{round3(total_wet):f}",
        "total_free_water_kg": f"{round3(total_free):f}",
        "design_water_kg": f"{round3(design):f}",
        "final_water_kg": f"{round3(design - total_free):f}",
    }
    if target is not None:
        body["target_dry_total_kg"] = f"{round3(Decimal(target)):f}"
        body["scale_factor"] = f"{round6(factor):f}"
    return body


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


def check(sample: dict, label: str) -> dict | None:
    """提交样例并与独立复算结果比对；通过返回响应体，失败打印原因并返回 None。"""
    resp = httpx.post(URL, json=sample, timeout=TIMEOUT)
    if resp.status_code != 200:
        print(f"[verify] FAIL: {label} HTTP {resp.status_code}: {resp.text}", file=sys.stderr)
        return None
    actual = resp.json()
    expected = expected_sheet(sample)
    if actual != expected:
        print(f"[verify] FAIL: {label} 响应与独立复算结果不一致", file=sys.stderr)
        print("expected:", json.dumps(expected, ensure_ascii=False, indent=2), file=sys.stderr)
        print("actual:  ", json.dumps(actual, ensure_ascii=False, indent=2), file=sys.stderr)
        return None
    return actual


def print_sheet(sheet: dict) -> None:
    for item in sheet["items"]:
        print(
            f"  [{item['index']}] {item['name']}: "
            f"湿投料 {item['wet_mass_kg']} kg"
            f"（干基 {item['dry_mass_kg']} kg，自由水 {item['free_water_kg']} kg）"
        )
    print(f"[verify] 湿投料合计 {sheet['total_wet_mass_kg']} kg，"
          f"自由水量合计 {sheet['total_free_water_kg']} kg")
    print(f"[verify] 最终加水量 {sheet['final_water_kg']} kg"
          f"（设计加水量 {sheet['design_water_kg']} kg）")


def main() -> int:
    wait_for_api()

    actual = check(SAMPLE, "原样例（未传目标干料总量）")
    if actual is None:
        return 1
    scaled = check(SCALED_SAMPLE, "目标干料总量缩放样例")
    if scaled is None:
        return 1

    print("[verify] 验收通过 —— 湿投料清单：")
    print_sheet(actual)
    print(f"[verify] 目标干料总量 {scaled['target_dry_total_kg']} kg"
          f"（缩放系数 {scaled['scale_factor']}）—— 缩放后湿投料清单：")
    print_sheet(scaled)
    return 0


if __name__ == "__main__":
    sys.exit(main())
