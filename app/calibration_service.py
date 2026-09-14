"""校准曲线服务编排：契约模型 ↔ 仓储记录 ↔ Decimal 插值。

- 创建：把传感器编号与 3 ~ 8 个对照点按原始读数字符串整批落库为不可变
  记录，生成曲线编号并回显；
- 换算：按编号读曲线，对单个原始信号做相邻两点 Decimal 线性插值，中间值
  不舍入，含水率在出口按 ROUND_HALF_UP 保留三位；端点输入直接返回对应
  参考值；曲线不存在抛 CurveNotFoundError（端点 → 404），信号越界抛
  SignalOutOfRangeError（端点 → 定位到原始信号的 422）。
"""
from __future__ import annotations

from decimal import Decimal

from app.calibration_calculator import SignalOutOfRangeError, interpolate
from app.calibration_repository import (
    CalibrationCurveRecord,
    CalibrationCurveRepository,
    CurveNotFoundError,
)
from app.calibration_schemas import (
    CalibrationCurveCreate,
    CalibrationCurveOut,
    CalibrationPointOut,
    ConversionIntervalOut,
    MoistureConversionIn,
    MoistureConversionOut,
)
from app.summary import round3


def _fixed_point(value: Decimal) -> str:
    """定点字符串（如 '12.5000'），避免科学记数法往返失真。"""
    return format(value, "f")


def create_curve(
    repo: CalibrationCurveRepository, payload: CalibrationCurveCreate
) -> CalibrationCurveOut:
    """生成曲线编号并把对照点整批写入为不可变记录。"""
    points = [
        (_fixed_point(signal), _fixed_point(moisture))
        for signal, moisture in zip(
            payload.raw_signals, payload.reference_moisture_pct, strict=True
        )
    ]
    return to_out(repo.create(payload.sensor_id, points))


def to_out(record: CalibrationCurveRecord) -> CalibrationCurveOut:
    """持久化记录 → 创建响应契约（原始读数原样回显）。"""
    return CalibrationCurveOut(
        curve_no=record.curve_no,
        sensor_id=record.sensor_id,
        points=[
            CalibrationPointOut(
                index=point.index,
                raw_signal=point.raw_signal,
                reference_moisture_pct=point.reference_moisture_pct,
            )
            for point in record.points
        ],
        created_at=record.created_at,
    )


def convert_signal(
    repo: CalibrationCurveRepository, payload: MoistureConversionIn
) -> MoistureConversionOut:
    """按曲线换算单个原始信号，返回命中区间端点与三位 HALF_UP 含水率。

    曲线不存在时由仓储抛 CurveNotFoundError；信号越界时由领域计算抛
    SignalOutOfRangeError，二者由端点分别映射为 404 与定位 raw_signal 的
    422，两种失败路径都不写库。
    """
    record = repo.get(payload.curve_no)
    points = [
        (Decimal(point.raw_signal), Decimal(point.reference_moisture_pct))
        for point in record.points
    ]
    result = interpolate(points, payload.raw_signal)
    return MoistureConversionOut(
        curve_no=record.curve_no,
        raw_signal=_fixed_point(payload.raw_signal),
        interval=ConversionIntervalOut(
            lower_signal=_fixed_point(result.lower_signal),
            upper_signal=_fixed_point(result.upper_signal),
            lower_moisture_pct=_fixed_point(result.lower_moisture_pct),
            upper_moisture_pct=_fixed_point(result.upper_moisture_pct),
        ),
        moisture_pct=_fixed_point(round3(result.moisture_pct)),
    )
