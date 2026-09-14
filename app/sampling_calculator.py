"""取样批次含水率计算：纯 Decimal，中间值保持完整精度。

固定公式（每组称量）：
    组含水率（%）=（湿样质量 − 干样质量）÷ 干样质量 × 100
代表含水率：
    各组含水率的中位数（偶数个时取中间两项的算术平均）。

组结果保持完整精度；仅代表含水率在出口处按 ROUND_HALF_UP 保留三位
（见 sampling_service.summarize）。固定计算上下文精度 50 位：
实验室称量（至多十多位有效数字）的乘除与中位数平均都不会触顶，
同时保证验收端按同一精度独立复算时能逐位一致。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Sequence

# 与修正单的自适应高精度不同：本模块入参量级有界（实验称量），
# 采用固定精度即可，且让外部独立复算可复现同样的字符串结果。
CALC_PRECISION = 50

_HUNDRED = Decimal("100")
_ZERO = Decimal("0")
_TWO = Decimal("2")


@dataclass(frozen=True)
class ReadingResult:
    """单组称量的完整精度结果。"""

    index: int
    wet_sample_mass: Decimal
    dry_sample_mass: Decimal
    moisture_pct: Decimal


@dataclass(frozen=True)
class BatchEvaluation:
    """整批确认结果（组结果完整精度，median 为完整精度中位数）。"""

    readings: tuple[ReadingResult, ...]
    median_moisture_pct: Decimal


def moisture_percent(wet_sample_mass: Decimal, dry_sample_mass: Decimal) -> Decimal:
    """组含水率 =（湿样 − 干样）÷ 干样 × 100（完整精度，不在此舍入）。"""
    with localcontext() as ctx:
        ctx.prec = CALC_PRECISION
        return (wet_sample_mass - dry_sample_mass) / dry_sample_mass * _HUNDRED


def median(values: Sequence[Decimal]) -> Decimal:
    """中位数：奇数取中间项，偶数取中间两项的算术平均。"""
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / _TWO


def evaluate_readings(
    readings: Sequence[tuple[Decimal, Decimal]],
) -> BatchEvaluation:
    """按原始称量顺序逐组计算，并以完整精度求中位数。

    readings 为 (湿样质量, 干样质量) 序列；调用方（Pydantic 契约）
    已保证质量为正且干样严格小于湿样。
    """
    with localcontext() as ctx:
        ctx.prec = CALC_PRECISION
        results = tuple(
            ReadingResult(
                index=i,
                wet_sample_mass=wet,
                dry_sample_mass=dry,
                moisture_pct=moisture_percent(wet, dry),
            )
            for i, (wet, dry) in enumerate(readings)
        )
        # 中位数平均只可能由除 2 引入一位小数，额外余量防止上下文触顶
        with localcontext() as mctx:
            mctx.prec = CALC_PRECISION + 8
            med = median([r.moisture_pct for r in results])
    return BatchEvaluation(readings=results, median_moisture_pct=med)
