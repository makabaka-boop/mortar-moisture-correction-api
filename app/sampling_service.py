"""取样批次服务编排：契约模型 ↔ 仓储记录 ↔ 完整精度计算。

- 创建：只把原始读数整批落库为“待确认”，不做任何百分率换算；
- 修订：只允许待确认批次替换一组读数，同事务递增修订号并写审计；
- 确认：在确认事务锁定批次后，从库内最新原始读数逐组计算完整精度含水率，中位数出口按
  ROUND_HALF_UP 保留三位后，连同状态一次性写入。
"""
from __future__ import annotations

from decimal import Decimal

from app.sampling_calculator import evaluate_readings
from app.sampling_repository import (
    SamplingBatchRecord,
    SamplingBatchRepository,
    StoredReading,
)
from app.sampling_schemas import (
    MoistureReadingOut,
    MoistureReadingRevisionIn,
    RevisedSamplingBatchOut,
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


def revise_batch_reading(
    repo: SamplingBatchRepository,
    batch_no: str,
    index: int,
    payload: MoistureReadingRevisionIn,
) -> SamplingBatchOut:
    """修订待确认批次的一组称量，并在同一事务留下审计记录。

    新质量已经过与创建时相同的 Pydantic 质量关系校验；仓储用客户端已见
    revision_no 做乐观并发控制。成功后以最新读数返回，确认时再据此计算。
    """
    record = repo.revise_reading(
        batch_no,
        index,
        format(payload.wet_sample_mass, "f"),
        format(payload.dry_sample_mass, "f"),
        expected_revision_no=payload.revision_no,
    )
    return RevisedSamplingBatchOut(**_batch_out_data(record), revision_no=record.revision_no)


def _confirmation_results(
    readings: tuple[StoredReading, ...]
) -> tuple[list[str], str]:
    """根据锁定后的最新读数计算各组完整精度结果与三位 HALF_UP 中位数。"""
    evaluation = evaluate_readings(
        [(Decimal(r.wet_sample_mass), Decimal(r.dry_sample_mass)) for r in readings]
    )
    reading_pcts = [format(r.moisture_pct, "f") for r in evaluation.readings]
    median3 = format(round3(evaluation.median_moisture_pct), "f")
    return reading_pcts, median3


def confirm_batch(
    repo: SamplingBatchRepository, batch_no: str
) -> SamplingBatchOut:
    """按库内原始读数计算并确认批次。

    组结果保持完整精度；代表含水率为中位数按 ROUND_HALF_UP 保留三位
    后的定点字符串。编号不存在 / 已确认的异常由仓储抛出、端点映射为
    404 / 409。
    """
    confirmed = repo.confirm(
        batch_no, result_provider=_confirmation_results
    )
    return to_out(confirmed)


def _batch_out_data(record: SamplingBatchRecord) -> dict:
    """持久化记录 → 原批次响应契约所需字段（不含新增修订号）。"""
    return {
        "batch_no": record.batch_no,
        "pile_name": record.pile_name,
        "status": record.status,
        "readings": [
            MoistureReadingOut(
                index=reading.index,
                wet_sample_mass=reading.wet_sample_mass,
                dry_sample_mass=reading.dry_sample_mass,
                moisture_pct=reading.moisture_pct,
            )
            for reading in record.readings
        ],
        "representative_moisture_pct": record.representative_moisture_pct,
        "created_at": record.created_at,
        "confirmed_at": record.confirmed_at,
    }


def to_out(record: SamplingBatchRecord) -> SamplingBatchOut:
    """持久化记录 → 原批次响应契约（待确认时各含水率字段为 null）。"""
    return SamplingBatchOut(**_batch_out_data(record))
