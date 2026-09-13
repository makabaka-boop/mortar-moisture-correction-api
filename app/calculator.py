"""修正计算模块：纯 Decimal 计算，中间值保持完整精度，不做任何舍入。

固定公式（每种骨料）：
    湿投料量 = 干基目标质量 × (1 + 含水率/100)
    自由水量 = 干基目标质量 × (含水率 − 吸水率)/100
批次：
    最终加水量 = 设计加水量 − 各项自由水量之和
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
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
    """整批修正结果（完整精度，未舍入）。"""

    items: tuple[AggregateCorrection, ...]
    design_water_kg: Decimal
    total_free_water_kg: Decimal
    final_water_kg: Decimal


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


def correct_batch(
    design_water_kg: Decimal, aggregates: Sequence[AggregateLike]
) -> BatchCorrection:
    """整批修正：逐项计算后汇总自由水量，得到最终加水量。"""
    items = tuple(
        correct_aggregate(
            index=i,
            name=agg.name,
            dry_mass_kg=agg.dry_mass_kg,
            moisture_pct=agg.moisture_pct,
            absorption_pct=agg.absorption_pct,
        )
        for i, agg in enumerate(aggregates)
    )
    total_free_water = sum((item.free_water_kg for item in items), _ZERO)
    return BatchCorrection(
        items=items,
        design_water_kg=design_water_kg,
        total_free_water_kg=total_free_water,
        final_water_kg=design_water_kg - total_free_water,
    )
