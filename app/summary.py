"""批次汇总模块：三位小数 ROUND_HALF_UP 舍入与修正单响应组装。

所有中间值在 calculator 中保持完整精度，仅在此模块出口处统一舍入；
合计值由未舍入的中间值求和后再舍入，而非累加已舍入的逐项展示值。
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP, localcontext

from app.calculator import BatchCorrection
from app.schemas import AggregateCorrectionOut, CorrectionSheetOut

THREE_PLACES = Decimal("0.001")
SIX_PLACES = Decimal("0.000001")
_ZERO = Decimal("0")


def _round_half_up(value: Decimal, quantum: Decimal) -> Decimal:
    """按 ROUND_HALF_UP 把 value 舍入到 quantum 位（负数按绝对值远离零方向进位）。

    上下文精度按值的量级自适应（整数位 + 小数位 + 1 位余量），
    保证任意大小/精度的值 quantize 都不抛 InvalidOperation。
    """
    with localcontext() as ctx:
        ctx.prec = max(28, value.adjusted() + 1 - quantum.as_tuple().exponent)
        return value.quantize(quantum, rounding=ROUND_HALF_UP)


def round3(value: Decimal) -> Decimal:
    """按 ROUND_HALF_UP 保留三位小数。"""
    return _round_half_up(value, THREE_PLACES)


def round6(value: Decimal) -> Decimal:
    """按 ROUND_HALF_UP 保留六位小数（缩放系数展示用）。"""
    return _round_half_up(value, SIX_PLACES)


def build_summary(correction: BatchCorrection) -> CorrectionSheetOut:
    """把完整精度的批次修正结果组装成三位小数的修正单响应。

    批次带目标干料总量时，响应补充三位小数的目标值与六位小数的缩放系数；
    否则两字段为 None，由端点序列化时剔除，响应与未传目标时完全一致。
    """
    items = [
        AggregateCorrectionOut(
            index=item.index,
            name=item.name,
            dry_mass_kg=round3(item.dry_mass_kg),
            wet_mass_kg=round3(item.wet_mass_kg),
            free_water_kg=round3(item.free_water_kg),
        )
        for item in correction.items
    ]
    total_dry = sum((item.dry_mass_kg for item in correction.items), _ZERO)
    total_wet = sum((item.wet_mass_kg for item in correction.items), _ZERO)
    target = correction.target_dry_total_kg
    factor = correction.scale_factor
    return CorrectionSheetOut(
        items=items,
        item_count=len(items),
        total_dry_mass_kg=round3(total_dry),
        total_wet_mass_kg=round3(total_wet),
        total_free_water_kg=round3(correction.total_free_water_kg),
        design_water_kg=round3(correction.design_water_kg),
        final_water_kg=round3(correction.final_water_kg),
        target_dry_total_kg=round3(target) if target is not None else None,
        scale_factor=round6(factor) if factor is not None else None,
    )
