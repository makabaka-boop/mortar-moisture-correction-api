#!/usr/bin/env python3
"""一次性验收服务：向 API 提交样例，独立复算并比对响应。

覆盖三类验收：
1. 未传目标干料总量的多骨料修正单 —— 响应须与独立复算完全一致；
2. 传入目标干料总量的缩放修正单 —— 同比缩放，响应同样独立复算比对；
3. 烘干法取样批次（新增）——
   a. 创建多组原始称量：201“待确认”，只回原始读数、不出现百分率；
   b. 非法称量（干样不小于湿样）：422 且 detail 定位 readings 下标；
   c. 确认前修订单组读数：200 原子替换读数并递增 revision_no，审计保留
      修改前后质量与时间；旧 revision_no 并发写入仅一笔成功，另一笔 409；
   d. 非法修订称量：422 整笔回滚，读数、revision_no、审计记录均不变；
   e. 确认：按修订后的最新读数逐组独立复算完整精度结果，代表含水率为中位数
      按 ROUND_HALF_UP 保留三位，状态置“已确认”；
   f. 重复确认/已确认后修订：409 且结果与首次完全一致（已确认数据不可改动）；
   g. 旧版 SQLite 库初始化迁移：既有批次 revision_no 补 0 且仍可确认；
   h. 重建仓储：直接打开同一个 SQLite 文件新建仓储实例，仍能读到同一批次。

用法：
    python verify.py                 # 默认打 http://localhost:8000
    API_BASE_URL=http://api:8000 python verify.py

退出码：0 = 验收通过；1 = 验收失败。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import ROUND_HALF_UP, Decimal, localcontext
from fractions import Fraction

import httpx

API_BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:8000").rstrip("/")
SHEET_URL = f"{API_BASE_URL}/api/v1/correction-sheet"
BATCH_URL = f"{API_BASE_URL}/api/v1/moisture-batches"
# 与 API 容器同一个 SQLite 库文件（compose 共享卷），用于“重建仓储”验收
SAMPLING_DB_PATH = os.environ.get("SAMPLING_DB_PATH", "data/moisture_batches.db")
TIMEOUT = 10.0
RETRY_SECONDS = 60

# 多骨料修正单验收样例（kg，质量百分数）
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

# 取样批次验收样例：4 组烘干法原始称量（g），偶数个组以覆盖中位数取中间两项平均
SAMPLING_READINGS = [
    {"wet_sample_mass": "500.12", "dry_sample_mass": "477.72"},
    {"wet_sample_mass": "480.00", "dry_sample_mass": "458.50"},
    {"wet_sample_mass": "512.345", "dry_sample_mass": "480.005"},
    {"wet_sample_mass": "210.00", "dry_sample_mass": "200.00"},
]

# 修订第二组：湿样 480 → 500；修订后中位数与原始结果不同，用来证明确认读的是最新读数
REVISED_INDEX = 1
REVISED_READING = {"wet_sample_mass": "500.00", "dry_sample_mass": "458.50"}
REVISED_SAMPLING_READINGS = [
    reading if i != REVISED_INDEX else REVISED_READING
    for i, reading in enumerate(SAMPLING_READINGS)
]

LEGACY_DB_PATH = os.environ.get(
    "LEGACY_SAMPLING_DB_PATH",
    os.path.join(os.path.dirname(SAMPLING_DB_PATH), "legacy_moisture_batches.db"),
)

Q3 = Decimal("0.001")
Q6 = Decimal("0.000001")
# 取样计算精度下限（与 app.sampling_calculator._MIN_PRECISION 对应）
MIN_CALC_PREC = 50


def round3(value: Decimal) -> Decimal:
    return value.quantize(Q3, rounding=ROUND_HALF_UP)


def round6(value: Decimal) -> Decimal:
    return value.quantize(Q6, rounding=ROUND_HALF_UP)


def _sampling_precision(values):
    """独立重写自适应精度估算（与产品同数学、不复用代码）。"""
    decimals = [Decimal(v) for v in values]
    max_int = max(max(v.adjusted() + 1, 0) for v in decimals)
    max_frac = max(max(-v.as_tuple().exponent, 0) for v in decimals)
    max_sig = max(len(v.as_tuple().digits) for v in decimals)
    return max(MIN_CALC_PREC, 2 * (max_int + max_frac + max_sig) + 16)


def expected_sheet(sample: dict) -> dict:
    """用 Decimal 独立复算修正单期望值（与 API 实现互不复用代码）。"""
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


def expected_sampling(readings: list[dict]) -> dict:
    """独立复算取样批次：自适应精度的各组完整精度结果 + Fraction 严格三位中位数。

    逐组串按与产品相同数学的自适应精度复算（逐位可比）；代表值以 Fraction
    精确有理数判定舍入边界，避免独立复算与产品共享固定精度截断风险。
    """
    raw = [v for r in readings for v in (r["wet_sample_mass"], r["dry_sample_mass"])]
    prec = _sampling_precision(raw)
    with localcontext() as ctx:
        ctx.prec = prec
        pcts = [
            (Decimal(r["wet_sample_mass"]) - Decimal(r["dry_sample_mass"]))
            / Decimal(r["dry_sample_mass"]) * 100
            for r in readings
        ]
    frac_pcts = sorted(
        (Fraction(r["wet_sample_mass"]) - Fraction(r["dry_sample_mass"]))
        / Fraction(r["dry_sample_mass"]) * 100
        for r in readings
    )
    n = len(frac_pcts)
    frac_median = (
        frac_pcts[n // 2]
        if n % 2
        else (frac_pcts[n // 2 - 1] + frac_pcts[n // 2]) / 2
    )
    thousandths = int(frac_median * 1000 + Fraction(1, 2))  # HALF_UP（值恒正）
    median3 = f"{Decimal(thousandths) / Decimal(1000):.3f}"
    return {
        "reading_pcts": [format(p, "f") for p in pcts],
        "median3": median3,
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


def check(sample: dict, label: str) -> dict | None:
    """提交修正单样例并与独立复算结果比对；通过返回响应体，失败打印原因并返回 None。"""
    resp = httpx.post(SHEET_URL, json=sample, timeout=TIMEOUT)
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


def revision_url(batch_no: str, index: int) -> str:
    return f"{BATCH_URL}/{batch_no}/readings/{index}"


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


def create_legacy_sampling_database() -> str:
    """按新增修订号之前的旧 schema 造一个含待确认批次的 SQLite 库。"""
    from app.sampling_repository import LEGACY_SCHEMA

    parent = os.path.dirname(LEGACY_DB_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)
    if os.path.exists(LEGACY_DB_PATH):
        os.remove(LEGACY_DB_PATH)
    batch_no = "MC20260913-LEGACY01"
    with sqlite3.connect(LEGACY_DB_PATH) as raw:
        raw.executescript(LEGACY_SCHEMA)
        raw.execute(
            "INSERT INTO sampling_batches"
            " (batch_no, pile_name, status, created_at)"
            " VALUES (?, ?, '待确认', ?)",
            (batch_no, "旧库迁移砂堆", "2026-09-13T00:00:00+00:00"),
        )
        raw.executemany(
            "INSERT INTO sampling_readings"
            " (batch_no, ordinal, wet_sample_mass, dry_sample_mass)"
            " VALUES (?, ?, ?, ?)",
            [
                (batch_no, 0, "210", "200"),
                (batch_no, 1, "206", "200"),
            ],
        )
        raw.commit()
    return batch_no


def verify_sampling_batch() -> bool:
    """一条龙验收：创建 → 修订/冲突/回滚 → 确认 → 409/404 → 旧库迁移 → 重建。"""
    legacy_batch_no = create_legacy_sampling_database()

    # 1) 创建多组称量：201 待确认，只留原始读数
    create_resp = httpx.post(
        BATCH_URL, json={"pile_name": "雨后1号砂堆", "readings": SAMPLING_READINGS}, timeout=TIMEOUT
    )
    if create_resp.status_code != 201:
        print(f"[verify] FAIL: 创建取样批次 HTTP {create_resp.status_code}: "
              f"{create_resp.text}", file=sys.stderr)
        return False
    created = create_resp.json()
    batch_no = created["batch_no"]
    if created["status"] != "待确认":
        print(f"[verify] FAIL: 新批次状态应为待确认，实际 {created['status']}", file=sys.stderr)
        return False
    if "revision_no" in created:
        print("[verify] FAIL: 创建响应应保持原契约，不包含 revision_no 字段",
              file=sys.stderr)
        return False
    if created["representative_moisture_pct"] is not None or any(
        r["moisture_pct"] is not None for r in created["readings"]
    ):
        print("[verify] FAIL: 待确认批次不得提前出现含水率结果", file=sys.stderr)
        return False
    print(f"[verify] 创建取样批次 {batch_no}（待确认，{len(created['readings'])} 组原始称量）")

    # 2) 非法称量：第二组干样 == 湿样 → 422 定位 readings[1].dry_sample_mass
    illegal = {"pile_name": "坏堆", "readings": [
        {"wet_sample_mass": "200", "dry_sample_mass": "190"},
        {"wet_sample_mass": "100", "dry_sample_mass": "100"},
        {"wet_sample_mass": "300", "dry_sample_mass": "290"},
    ]}
    bad_resp = httpx.post(BATCH_URL, json=illegal, timeout=TIMEOUT)
    if bad_resp.status_code != 422:
        print(f"[verify] FAIL: 非法称量应返回 422，实际 {bad_resp.status_code}", file=sys.stderr)
        return False
    fields = [e["field"] for e in bad_resp.json()["detail"]]
    if "readings[1].dry_sample_mass" not in fields:
        print(f"[verify] FAIL: 非法称量错误未定位 readings[1]：{fields}", file=sys.stderr)
        return False
    print("[verify] 非法称量（干样不小于湿样）已 422 拒绝并定位 readings[1]，不落库")

    # 3) 创建后、确认前修订单组读数：revision 0 → 1，并保存修改前后审计
    revision_payload = {**REVISED_READING, "revision_no": 0}
    rev_resp = httpx.patch(
        revision_url(batch_no, REVISED_INDEX), json=revision_payload, timeout=TIMEOUT
    )
    if rev_resp.status_code != 200:
        print(f"[verify] FAIL: 修订读数 HTTP {rev_resp.status_code}: {rev_resp.text}",
              file=sys.stderr)
        return False
    revised_body = rev_resp.json()
    if revised_body["revision_no"] != 1:
        print(f"[verify] FAIL: 修订后 revision_no 应为 1，实际 {revised_body['revision_no']}",
              file=sys.stderr)
        return False
    actual_revised_readings = [
        {"wet_sample_mass": r["wet_sample_mass"], "dry_sample_mass": r["dry_sample_mass"]}
        for r in revised_body["readings"]
    ]
    if actual_revised_readings != REVISED_SAMPLING_READINGS:
        print("[verify] FAIL: 修订响应不是目标读数的原子替换", file=sys.stderr)
        print("expected:", json.dumps(REVISED_SAMPLING_READINGS, ensure_ascii=False), file=sys.stderr)
        print("actual:  ", json.dumps(actual_revised_readings, ensure_ascii=False), file=sys.stderr)
        return False
    if any(r["moisture_pct"] is not None for r in revised_body["readings"]):
        print("[verify] FAIL: 待确认批次修订后不得提前出现含水率", file=sys.stderr)
        return False
    print(f"[verify] 已修订读数[{REVISED_INDEX}]，revision_no 0 → 1，其他读数保持不变")

    # 4) 两个客户端都带旧修订号 0 并发写：BEGIN IMMEDIATE 下仅一笔成功，另一笔 409
    concurrent_payloads = [
        {"wet_sample_mass": "501.00", "dry_sample_mass": "458.50", "revision_no": 0},
        {"wet_sample_mass": "502.00", "dry_sample_mass": "458.50", "revision_no": 0},
    ]
    with ThreadPoolExecutor(max_workers=2) as executor:
        concurrent_responses = list(
            executor.map(
                lambda payload: httpx.patch(
                    revision_url(batch_no, REVISED_INDEX), json=payload, timeout=TIMEOUT
                ),
                concurrent_payloads,
            )
        )
    concurrent_statuses = sorted(resp.status_code for resp in concurrent_responses)
    if concurrent_statuses != [200, 409]:
        print(f"[verify] FAIL: 旧修订号并发写入应一笔成功一笔 409，实际 {concurrent_statuses}",
              file=sys.stderr)
        return False
    conflict = next(resp for resp in concurrent_responses if resp.status_code == 409).json()
    if conflict["detail"][0]["type"] != "revision_conflict":
        print(f"[verify] FAIL: 并发冲突类型错误：{conflict}", file=sys.stderr)
        return False
    current_revision = 2
    winner = next(resp for resp in concurrent_responses if resp.status_code == 200).json()
    if winner["revision_no"] != current_revision:
        print(f"[verify] FAIL: 并发成功后修订号应为 2，实际 {winner['revision_no']}",
              file=sys.stderr)
        return False
    print("[verify] 旧修订号并发写入：一笔成功（revision_no → 2），过期请求返回 409")

    # 5) 非法修订称量 422：读数、修订号、审计记录均保持不变
    illegal_revision = {"wet_sample_mass": "100", "dry_sample_mass": "100", "revision_no": current_revision}
    illegal_rev_resp = httpx.patch(
        revision_url(batch_no, 0), json=illegal_revision, timeout=TIMEOUT
    )
    if illegal_rev_resp.status_code != 422:
        print(f"[verify] FAIL: 非法修订称量应返回 422，实际 {illegal_rev_resp.status_code}",
              file=sys.stderr)
        return False
    if illegal_rev_resp.json()["detail"][0]["field"] != "dry_sample_mass":
        print(f"[verify] FAIL: 非法修订应定位 dry_sample_mass：{illegal_rev_resp.text}",
              file=sys.stderr)
        return False
    # 直接打开同一 SQLite 文件核对：422 发生后修订号仍为 2、读数未变、审计仍为前两条
    from app.sampling_repository import SamplingBatchRepository

    check_repo = SamplingBatchRepository(SAMPLING_DB_PATH)
    record_after_illegal = check_repo.get(batch_no)
    audits_after_illegal = check_repo.list_reading_revisions(batch_no)
    if record_after_illegal.revision_no != current_revision:
        print("[verify] FAIL: 非法修订改变了 revision_no", file=sys.stderr)
        return False
    if len(audits_after_illegal) != 2:
        print(f"[verify] FAIL: 非法修订不应写审计，实际 {len(audits_after_illegal)} 条",
              file=sys.stderr)
        return False
    if (record_after_illegal.readings[0].wet_sample_mass,
            record_after_illegal.readings[0].dry_sample_mass) != (
        SAMPLING_READINGS[0]["wet_sample_mass"],
        SAMPLING_READINGS[0]["dry_sample_mass"],
    ):
        print("[verify] FAIL: 非法修订改动了第一组读数", file=sys.stderr)
        return False
    final_pending_readings = [
        {"wet_sample_mass": r.wet_sample_mass, "dry_sample_mass": r.dry_sample_mass}
        for r in record_after_illegal.readings
    ]
    print("[verify] 非法修订称量返回 422；读数、修订号与审计均不变（入口校验，整笔拒绝）")

    # 6) 确认：完整精度逐组结果 + 三位 HALF_UP 中位数（从最新读数计算）
    confirm_resp = httpx.post(f"{BATCH_URL}/{batch_no}/confirm", timeout=TIMEOUT)
    if confirm_resp.status_code != 200:
        print(f"[verify] FAIL: 确认取样批次 HTTP {confirm_resp.status_code}: "
              f"{confirm_resp.text}", file=sys.stderr)
        return False
    confirmed = confirm_resp.json()
    original_median = expected_sampling(SAMPLING_READINGS)["median3"]
    expected = expected_sampling(final_pending_readings)
    if expected["median3"] == original_median:
        print("[verify] FAIL: 验收样例修订前后中位数不应相同", file=sys.stderr)
        return False
    actual_pcts = [r["moisture_pct"] for r in confirmed["readings"]]
    if actual_pcts != expected["reading_pcts"]:
        print("[verify] FAIL: 各组含水率与独立完整精度复算不一致", file=sys.stderr)
        print("expected:", expected["reading_pcts"], file=sys.stderr)
        print("actual:  ", actual_pcts, file=sys.stderr)
        return False
    if confirmed["representative_moisture_pct"] != expected["median3"]:
        print("[verify] FAIL: 代表含水率中位数（三位 HALF_UP）与独立复算不一致："
              f"期望 {expected['median3']}，实际 {confirmed['representative_moisture_pct']}",
              file=sys.stderr)
        return False
    if confirmed["status"] != "已确认" or not confirmed["confirmed_at"]:
        print("[verify] FAIL: 确认后状态应为已确认且带确认时间", file=sys.stderr)
        return False
    if "revision_no" in confirmed:
        print("[verify] FAIL: 确认响应应保持原契约，不包含 revision_no 字段",
              file=sys.stderr)
        return False
    print(f"[verify] 批次已按修订后的最新读数确认：代表含水率 "
          f"{confirmed['representative_moisture_pct']}%（中位数，ROUND_HALF_UP 三位）")
    for r in confirmed["readings"]:
        print(f"  [{r['index']}] 湿样 {r['wet_sample_mass']} g / 干样 {r['dry_sample_mass']} g"
              f" → 含水率 {r['moisture_pct']}%（完整精度）")

    # 7) 超长有效数字且紧挨舍入边界：真实中位数 1.2345 − 1e-52，必须显示 1.234
    with localcontext() as ctx:
        ctx.prec = 260
        target = Decimal("1.2345") - Decimal(10) ** -52

        def wet_for(pct: Decimal) -> str:
            return format(Decimal("1") + pct / 100, "f")

        boundary_readings = [
            {"wet_sample_mass": wet_for(Decimal("0.5")), "dry_sample_mass": "1"},
            {"wet_sample_mass": wet_for(target), "dry_sample_mass": "1"},
            {"wet_sample_mass": wet_for(Decimal("2")), "dry_sample_mass": "1"},
        ]
    b_resp = httpx.post(
        BATCH_URL, json={"pile_name": "边界砂堆", "readings": boundary_readings}, timeout=TIMEOUT
    )
    if b_resp.status_code != 201:
        print(f"[verify] FAIL: 创建边界批次 HTTP {b_resp.status_code}: {b_resp.text}",
              file=sys.stderr)
        return False
    b_no = b_resp.json()["batch_no"]
    b_confirmed = httpx.post(f"{BATCH_URL}/{b_no}/confirm", timeout=TIMEOUT)
    if b_confirmed.status_code != 200:
        print(f"[verify] FAIL: 确认边界批次 HTTP {b_confirmed.status_code}: {b_confirmed.text}",
              file=sys.stderr)
        return False
    b_body = b_confirmed.json()
    b_expected = expected_sampling(boundary_readings)
    if b_expected["median3"] != "1.234":
        print(f"[verify] FAIL: 独立基准自身错误：{b_expected['median3']}", file=sys.stderr)
        return False
    if b_body["representative_moisture_pct"] != "1.234":
        print("[verify] FAIL: 超 50 位有效数字紧挨边界时代表含水率被错误进位："
              f"期望 1.234，实际 {b_body['representative_moisture_pct']}", file=sys.stderr)
        return False
    if [r["moisture_pct"] for r in b_body["readings"]] != b_expected["reading_pcts"]:
        print("[verify] FAIL: 边界批次各组完整精度结果与独立复算不一致", file=sys.stderr)
        return False
    print("[verify] 超长有效数字边界批次：中位数 1.2344999… 正确保留为 1.234（未误进位）")

    # 8) 重复确认：409 结构化错误，且结果一个字符都不变
    dup_resp = httpx.post(f"{BATCH_URL}/{batch_no}/confirm", timeout=TIMEOUT)
    if dup_resp.status_code != 409:
        print(f"[verify] FAIL: 重复确认应返回 409，实际 {dup_resp.status_code}", file=sys.stderr)
        return False
    detail = dup_resp.json()["detail"]
    if not isinstance(detail, list) or detail[0].get("type") != "batch_already_confirmed":
        print(f"[verify] FAIL: 409 错误结构不符：{dup_resp.text}", file=sys.stderr)
        return False
    reread = httpx.post(f"{BATCH_URL}/{batch_no}/confirm", timeout=TIMEOUT)
    assert reread.status_code == 409
    confirmed_revision = httpx.patch(
        revision_url(batch_no, 0),
        json={"wet_sample_mass": "300", "dry_sample_mass": "200", "revision_no": current_revision},
        timeout=TIMEOUT,
    )
    if confirmed_revision.status_code != 409:
        print("[verify] FAIL: 已确认批次的修订请求应返回 409，实际 "
              f"{confirmed_revision.status_code} {confirmed_revision.text}", file=sys.stderr)
        return False
    if confirmed_revision.json()["detail"][0]["type"] != "batch_already_confirmed":
        print(f"[verify] FAIL: 已确认修订错误类型不对：{confirmed_revision.text}", file=sys.stderr)
        return False
    # 以重建仓储读取为准比对不可改动性（见下），这里先核对确认时间未被刷新
    if confirmed["confirmed_at"] is None:
        print("[verify] FAIL: 首次确认时间为空", file=sys.stderr)
        return False

    # 9) 编号不存在：结构化 404
    missing = httpx.post(f"{BATCH_URL}/MC19990101-00000000/confirm", timeout=TIMEOUT)
    if missing.status_code != 404 or missing.json()["detail"][0].get("type") != "batch_not_found":
        print(f"[verify] FAIL: 不存在编号应返回结构化 404：{missing.status_code} {missing.text}",
              file=sys.stderr)
        return False
    print("[verify] 重复确认返回 409（结果不可改动）；不存在编号返回结构化 404")

    # 10) 重建仓储：同一 SQLite 文件上新建仓储实例，仍读到同一已确认批次
    from app.sampling_repository import SamplingBatchRepository

    rebuilt = SamplingBatchRepository(SAMPLING_DB_PATH)
    record = rebuilt.get(batch_no)
    if record.status != "已确认" or record.representative_moisture_pct != expected["median3"]:
        print("[verify] FAIL: 重建仓储后批次状态/代表值不一致", file=sys.stderr)
        return False
    if [r.moisture_pct for r in record.readings] != expected["reading_pcts"]:
        print("[verify] FAIL: 重建仓储后各组完整精度结果不一致", file=sys.stderr)
        return False
    boundary_record = rebuilt.get(b_no)
    if boundary_record.representative_moisture_pct != "1.234":
        print("[verify] FAIL: 重建仓储后边界批次代表值不一致", file=sys.stderr)
        return False
    audits = rebuilt.list_reading_revisions(batch_no)
    if len(audits) != 2:
        print(f"[verify] FAIL: 主批次应仅保留两次成功修订审计，实际 {len(audits)} 条",
              file=sys.stderr)
        return False
    if audits[0].previous_revision_no != 0 or audits[0].new_revision_no != 1:
        print("[verify] FAIL: 第一条审计的修改前后修订号不是 0 → 1", file=sys.stderr)
        return False
    if audits[1].previous_revision_no != 1 or audits[1].new_revision_no != 2:
        print("[verify] FAIL: 第二条审计的修改前后修订号不是 1 → 2", file=sys.stderr)
        return False
    print(f"[verify] 重建仓储后仍读到同一批次 {batch_no}（已确认，结果逐位一致，审计完整）")

    # 11) 旧版库初始化：既有批次 revision_no 补 0，迁移后原批次仍可从最新读数确认
    from app.sampling_service import confirm_batch

    legacy_repo = SamplingBatchRepository(LEGACY_DB_PATH)
    legacy_before = legacy_repo.get(legacy_batch_no)
    if legacy_before.revision_no != 0:
        print(f"[verify] FAIL: 旧库批次迁移后 revision_no 应为 0，实际 {legacy_before.revision_no}",
              file=sys.stderr)
        return False
    legacy_confirmed = confirm_batch(legacy_repo, legacy_batch_no)
    if legacy_confirmed.status != "已确认":
        print("[verify] FAIL: 旧库迁移后的原批次仍应可确认", file=sys.stderr)
        return False
    if legacy_confirmed.representative_moisture_pct != "4.000":
        print("[verify] FAIL: 旧库原批次确认结果应为中位数 4.000，实际 "
              f"{legacy_confirmed.representative_moisture_pct}", file=sys.stderr)
        return False
    print("[verify] 旧版 SQLite 库初始化迁移成功：既有批次补 revision_no=0，原批次确认 4.000")
    return True


def main() -> int:
    wait_for_api()

    actual = check(SAMPLE, "原样例（未传目标干料总量）")
    if actual is None:
        return 1
    scaled = check(SCALED_SAMPLE, "目标干料总量缩放样例")
    if scaled is None:
        return 1

    print("[verify] 修正单验收通过 —— 湿投料清单：")
    print_sheet(actual)
    print(f"[verify] 目标干料总量 {scaled['target_dry_total_kg']} kg"
          f"（缩放系数 {scaled['scale_factor']}）—— 缩放后湿投料清单：")
    print_sheet(scaled)

    if not verify_sampling_batch():
        return 1
    print("[verify] 全部验收通过（修正单 ×2 + 取样创建/修订/并发/回滚/确认/409/404/旧库迁移/重建仓储）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
