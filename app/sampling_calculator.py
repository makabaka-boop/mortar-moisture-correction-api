"""取样批次含水率计算：纯 Decimal，中间值保持完整精度。

固定公式（每组称量）：
    组含水率（%）=（湿样质量 − 干样质量）÷ 干样质量 × 100
代表含水率：
    各组含水率的中位数（偶数个时取中间两项的算术平均）。

组结果保持完整精度；仅代表含水率在出口处按 ROUND_HALF_UP 保留三位
（见 sampling_service）。计算上下文精度随输入的量级、小数跨度与有效
位数自适应：实验室称量可能带远超 50 位的有效数字，若真实结果紧挨
三位舍入边界（如 1.2345 下侧 1e-52），固定精度截断会把它推过边界
而错误进位（1.234 → 1.235），故精度必须覆盖全部输入数位并留足余量。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Sequence

_HUNDRED = Decimal("100")
_TWO = Decimal("2")
_ZERO = Decimal("0")

# 自适应精度下限：覆盖常规称量（至多十多位有效数字）的乘除与中位数平均
_MIN_PRECISION = 50


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


def _required_precision(values: Sequence[Decimal]) -> int:
    """按输入的量级、小数跨度与有效位数估算精确计算所需的上下文精度。

    精度必须同时覆盖：
    - 加减法的数位跨度（最大整数位 + 最小小数位）：湿样 − 干样在两质量
      极接近时需要逐位保留微小差量；
    - 各输入的最大有效位数：除 干样、×100 只移动小数点，舍入误差按
      有效位计，超长有效数字输入（> 50 位）必须整体保留，否则紧挨
      舍入边界的值会被截断后越过边界；
    - 中位数取中间两项平均（÷2）的余量。

    取 2 倍跨度加 16 位余量，与修正单计算模块同一思路。
    """
    max_int = max(max(v.adjusted() + 1, 0) for v in values)  # 最大整数位数
    max_frac = max(max(-v.as_tuple().exponent, 0) for v in values)  # 最小小数位
    max_sig = max(len(v.as_tuple().digits) for v in values)  # 最大有效位数
    return max(_MIN_PRECISION, 2 * (max_int + max_frac + max_sig) + 16)


def moisture_percent(
    wet_sample_mass: Decimal,
    dry_sample_mass: Decimal,
    *,
    precision: int | None = None,
) -> Decimal:
    """组含水率 =（湿样 − 干样）÷ 干样 × 100（完整精度，不在此舍入）。"""
    prec = precision or _required_precision([wet_sample_mass, dry_sample_mass])
    with localcontext() as ctx:
        ctx.prec = prec
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
    masses = [mass for pair in readings for mass in pair]
    with localcontext() as ctx:
        ctx.prec = _required_precision(masses)
        results = tuple(
            ReadingResult(
                index=i,
                wet_sample_mass=wet,
                dry_sample_mass=dry,
                moisture_pct=moisture_percent(wet, dry, precision=ctx.prec),
            )
            for i, (wet, dry) in enumerate(readings)
        )
        # 中位数平均只可能由除 2 引入一位小数，额外余量防止上下文触顶
        with localcontext() as mctx:
            mctx.prec = ctx.prec + 8
            med = median([r.moisture_pct for r in results])
    return BatchEvaluation(readings=results, median_moisture_pct=med)
