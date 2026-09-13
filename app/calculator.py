"""修正计算模块：纯 Decimal 计算，中间值保持完整精度，不做任何舍入。

固定公式（每种骨料）：
    湿投料量 = 干基目标质量 × (1 + 含水率/100)
    自由水量 = 干基目标质量 × (含水率 − 吸水率)/100
批次：
    最终加水量 = 设计加水量 − 各项自由水量之和
可选缩放（传入目标干料总量时）：
    缩放系数 = 目标干料总量 / 原骨料干基合计
    各项干基质量与设计加水量先同比缩放，再进入上述固定公式
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Protocol, Sequence

_HUNDRED = Decimal("100")
_ONE = Decimal("1")
_ZERO = Decimal("0")


class AggregateLike(Protocol):
    """计算所需的最小骨料输入接口（Pydantic 模型天然满足）。"""

    name: str
    dry_mass_kg: Decimal
    moisture_pct: Decimal
    absorption_pct: Decimal


@dataclass(frozen=True)
class AggregateCorrection:
    """单种骨料的修正结果（完整精度，未舍入）。"""

    index: int
    name: str
    dry_mass_kg: Decimal
    wet_mass_kg: Decimal
    free_water_kg: Decimal


@dataclass(frozen=True)
class BatchCorrection:
    """整批修正结果（完整精度，未舍入）。

    请求传入目标干料总量时，items 与 design_water_kg 均为缩放后的值，
    target_dry_total_kg / scale_factor 记录请求目标与缩放系数；否则为 None。
    """

    items: tuple[AggregateCorrection, ...]
    design_water_kg: Decimal
    total_free_water_kg: Decimal
    final_water_kg: Decimal
    target_dry_total_kg: Decimal | None = None
    scale_factor: Decimal | None = None


def wet_batch_mass(dry_mass_kg: Decimal, moisture_pct: Decimal) -> Decimal:
    """湿投料量 = 干基目标质量 × (1 + 含水率/100)。"""
    return dry_mass_kg * (_ONE + moisture_pct / _HUNDRED)


def free_water_mass(
    dry_mass_kg: Decimal, moisture_pct: Decimal, absorption_pct: Decimal
) -> Decimal:
    """自由水量 = 干基目标质量 × (含水率 − 吸水率)/100，吸水率更高时为负。"""
    return dry_mass_kg * (moisture_pct - absorption_pct) / _HUNDRED


def correct_aggregate(
    index: int,
    name: str,
    dry_mass_kg: Decimal,
    moisture_pct: Decimal,
    absorption_pct: Decimal,
) -> AggregateCorrection:
    """计算单种骨料的湿投料量与自由水量。"""
    return AggregateCorrection(
        index=index,
        name=name,
        dry_mass_kg=dry_mass_kg,
        wet_mass_kg=wet_batch_mass(dry_mass_kg, moisture_pct),
        free_water_kg=free_water_mass(dry_mass_kg, moisture_pct, absorption_pct),
    )


def _required_precision(
    design_water_kg: Decimal,
    aggregates: Sequence[AggregateLike],
    target_dry_total_kg: Decimal | None = None,
) -> int:
    """按输入的量级与小数跨度估算精确计算所需的上下文精度。

    精度必须同时覆盖：
    - 乘积的有效位数（不超过乘数有效位之和）；
    - 加减法的数位跨度（最大整数位 − 最小小数位）。否则当设计加水量
      远大于自由水量（如 1e28 − 0.6）时，小数部分会被上下文精度吞掉，
      最终加水量因舍入而失真；
    - 传入目标干料总量时，缩放系数（目标 / 原干基合计）的除法有效位，
      以及缩放后设计加水量可能被放大的整数位。
    """
    values = [design_water_kg]
    for agg in aggregates:
        values.extend([agg.dry_mass_kg, agg.moisture_pct, agg.absorption_pct])

    max_int = max(max(v.adjusted() + 1, 0) for v in values)  # 最大整数位数（量级）
    max_frac = max(max(-v.as_tuple().exponent, 0) for v in values)  # 最小小数位
    max_sig = max(len(v.as_tuple().digits) for v in values)  # 最大有效位数

    if target_dry_total_kg is not None:
        max_int = max(max_int, max(target_dry_total_kg.adjusted() + 1, 0))
        max_frac = max(max_frac, max(-target_dry_total_kg.as_tuple().exponent, 0))
        max_sig = max(max_sig, len(target_dry_total_kg.as_tuple().digits))
        # 缩放后设计加水量的量级 ≈ 设计加水量 × 目标 / 原干基合计，
        # 整数位可能相应放大（如 1e28 × 1e28 / 0.001）
        total_dry = sum((agg.dry_mass_kg for agg in aggregates), _ZERO)
        scale_mag = target_dry_total_kg.adjusted() - total_dry.adjusted() + 1
        max_int += max(scale_mag, 0)

    # 乘积小数位 ≤ 两乘数小数位之和（含 /100 的 2 位），求和至多 +1 位整数；
    # 取 2 倍跨度加余量，覆盖上述全部组合
    return max(28, 2 * (max_int + max_frac + max_sig) + 16)


def correct_batch(
    design_water_kg: Decimal,
    aggregates: Sequence[AggregateLike],
    target_dry_total_kg: Decimal | None = None,
) -> BatchCorrection:
    """整批修正：逐项计算后汇总自由水量，得到最终加水量。

    传入目标干料总量时，先以原骨料干基合计为基准求唯一缩放系数
    （目标 / 原干基合计），同比缩放各项干基质量与设计加水量；
    缩放值保持完整精度进入既有湿投料、自由水与最终加水计算。
    未传入时计算与传入前完全一致。
    """
    with localcontext() as ctx:
        ctx.prec = _required_precision(design_water_kg, aggregates, target_dry_total_kg)
        scale_factor: Decimal | None = None
        if target_dry_total_kg is None:
            dry_masses = [agg.dry_mass_kg for agg in aggregates]
            scaled_design = design_water_kg
        else:
            total_dry = sum((agg.dry_mass_kg for agg in aggregates), _ZERO)
            scale_factor = target_dry_total_kg / total_dry
            dry_masses = [agg.dry_mass_kg * scale_factor for agg in aggregates]
            scaled_design = design_water_kg * scale_factor
        items = tuple(
            correct_aggregate(
                index=i,
                name=agg.name,
                dry_mass_kg=dry_masses[i],
                moisture_pct=agg.moisture_pct,
                absorption_pct=agg.absorption_pct,
            )
            for i, agg in enumerate(aggregates)
        )
        total_free_water = sum((item.free_water_kg for item in items), _ZERO)
        return BatchCorrection(
            items=items,
            design_water_kg=scaled_design,
            total_free_water_kg=total_free_water,
            final_water_kg=scaled_design - total_free_water,
            target_dry_total_kg=target_dry_total_kg,
            scale_factor=scale_factor,
        )
