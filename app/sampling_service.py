"""取样批次服务编排：契约模型 ↔ 仓储记录 ↔ 完整精度计算。

- 创建：只把原始读数整批落库为“待确认”，不做任何百分率换算；
- 确认：从库内原始读数逐组计算完整精度含水率，中位数出口按
  ROUND_HALF_UP 保留三位后，连同状态一次性写入。
"""
from __future__ import annotations

from decimal import Decimal

from app.sampling_calculator import evaluate_readings
from app.sampling_repository import SamplingBatchRecord, SamplingBatchRepository
from app.sampling_schemas import (
    MoistureReadingOut,
    SamplingBatchCreate,
    SamplingBatchOut,
)
from app.summary import round3


def create_batch(
    repo: SamplingBatchRepository, payload: SamplingBatchCreate
) -> SamplingBatchOut:
    """接收料堆名称与 2 ~ 5 组原始称量，生成编号并以“待确认”整批保存。"""
    readings = [
        (format(r.wet_sample_mass, "f"), format(r.dry_sample_mass, "f"))
        for r in payload.readings
    ]
    record = repo.create(payload.pile_name, readings)
    return to_out(record)


def confirm_batch(
    repo: SamplingBatchRepository, batch_no: str
) -> SamplingBatchOut:
    """按库内原始读数计算并确认批次。

    组结果保持完整精度；代表含水率为中位数按 ROUND_HALF_UP 保留三位
    后的定点字符串。编号不存在 / 已确认的异常由仓储抛出、端点映射为
    404 / 409。
    """
    record = repo.get(batch_no)  # 不存在 → BatchNotFoundError
    evaluation = evaluate_readings(
        [(Decimal(r.wet_sample_mass), Decimal(r.dry_sample_mass)) for r in record.readings]
    )
    reading_pcts = [format(r.moisture_pct, "f") for r in evaluation.readings]
    median3 = format(round3(evaluation.median_moisture_pct), "f")
    confirmed = repo.confirm(batch_no, reading_pcts, median3)
    return to_out(confirmed)


def to_out(record: SamplingBatchRecord) -> SamplingBatchOut:
    """持久化记录 → 响应契约（待确认时各含水率字段为 null）。"""
    return SamplingBatchOut(
        batch_no=record.batch_no,
        pile_name=record.pile_name,
        status=record.status,
        readings=[
            MoistureReadingOut(
                index=reading.index,
                wet_sample_mass=reading.wet_sample_mass,
                dry_sample_mass=reading.dry_sample_mass,
                moisture_pct=reading.moisture_pct,
            )
            for reading in record.readings
        ],
        representative_moisture_pct=record.representative_moisture_pct,
        created_at=record.created_at,
        confirmed_at=record.confirmed_at,
    )
