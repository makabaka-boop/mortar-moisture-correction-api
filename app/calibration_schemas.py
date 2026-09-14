"""校准曲线模块的请求/响应契约（Pydantic）。

更换探头后，实验员把标准样对照数据固化为独立校准曲线（而不是在设备侧保存
零散系数）：
- 创建时只接收传感器编号与 3 ~ 8 个“原始信号 / 参考含水率”对照点，生成
  曲线编号后作为不可变记录写入既有 SQLite 文件；
- 换算时给曲线编号与单个原始信号，返回命中区间端点与按 ROUND_HALF_UP
  保留三位的含水率；端点输入直接返回对应参考值。

约束（创建请求）：
- raw_signals 为有限十进制数，数量 3 ~ 8，且严格递增（重复或倒序以 422
  拒绝）；信号单位由传感器约定，小数位数不限，故不施加数值范围；
- reference_moisture_pct 为含水率质量百分数，0 ~ 40（端点包含），数量必须
  与原始信号一致且严格递增；
- sensor_id 为非空字符串（≤ 64 字符）；
- 无法识别的字段一律拒绝（extra="forbid"），不静默忽略。

非法点集（长度不一致、重复信号点、参考含水率越界等）由 Pydantic 在入口
以 422 拒绝，失败时不产生或改写任何记录。
"""
from __future__ import annotations

from decimal import Decimal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
)
from pydantic_core import PydanticCustomError

MOISTURE_MIN = Decimal("0")
MOISTURE_MAX = Decimal("40")

MIN_POINTS = 3
MAX_POINTS = 8


def _fixed_point(value: Decimal) -> str:
    """定点字符串（如 '100.000'、'5.12345'），避免科学记数法往返失真。"""
    return format(value, "f")


class CalibrationCurveCreate(BaseModel):
    """校准曲线创建请求：传感器编号 + 3 ~ 8 个严格递增的对照点。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    sensor_id: str = Field(
        min_length=1, max_length=64, description="传感器编号（更换探头后的新探头所属传感器）"
    )
    raw_signals: list[Decimal] = Field(
        min_length=MIN_POINTS,
        max_length=MAX_POINTS,
        description="原始信号点，3 ~ 8 个有限十进制数，必须严格递增",
    )
    reference_moisture_pct: list[Decimal] = Field(
        min_length=MIN_POINTS,
        max_length=MAX_POINTS,
        description="参考含水率点（质量百分数），0 ~ 40，与原始信号一一对应且严格递增",
    )

    @field_validator("raw_signals")
    @classmethod
    def _raw_signals_strictly_increasing(cls, values: list[Decimal]) -> list[Decimal]:
        """原始信号必须严格递增：重复点与倒序都拒绝（非有限值已被字段类型拒绝）。"""
        for index in range(1, len(values)):
            if values[index] <= values[index - 1]:
                raise PydanticCustomError(
                    "raw_signals_not_strictly_increasing",
                    "原始信号必须严格递增：第 {index} 点 {current} 不大于前一点 {previous}",
                    {
                        "index": index,
                        "current": _fixed_point(values[index]),
                        "previous": _fixed_point(values[index - 1]),
                    },
                )
        return values

    @field_validator("reference_moisture_pct")
    @classmethod
    def _reference_moisture_valid(cls, values: list[Decimal], info) -> list[Decimal]:
        """参考含水率逐点限 0 ~ 40、整体严格递增，且与原始信号点数一致。

        非有限值已由字段类型拒绝；越界、倒序/重复点与点数不匹配分别给出
        独立错误类型，错误均定位到 reference_moisture_pct。
        """
        for index, value in enumerate(values):
            if value < MOISTURE_MIN or value > MOISTURE_MAX:
                raise PydanticCustomError(
                    "reference_moisture_out_of_range",
                    "参考含水率必须在 0 ~ 40 之间：第 {index} 点为 {value}",
                    {"index": index, "value": _fixed_point(value)},
                )
        for index in range(1, len(values)):
            if values[index] <= values[index - 1]:
                raise PydanticCustomError(
                    "reference_moisture_not_strictly_increasing",
                    "参考含水率必须严格递增：第 {index} 点 {current} 不大于前一点 {previous}",
                    {
                        "index": index,
                        "current": _fixed_point(values[index]),
                        "previous": _fixed_point(values[index - 1]),
                    },
                )
        raw_signals = info.data.get("raw_signals")
        if isinstance(raw_signals, list) and len(values) != len(raw_signals):
            raise PydanticCustomError(
                "point_count_mismatch",
                "参考含水率点数（{ref_count}）必须与原始信号点数（{signal_count}）一致",
                {"ref_count": len(values), "signal_count": len(raw_signals)},
            )
        return values


class CalibrationPointOut(BaseModel):
    """单个对照点：原始信号与参考含水率均为入库时的原始定点串。"""

    index: int = Field(description="对照点下标，从 0 开始")
    raw_signal: str = Field(description="原始信号（原样字符串，不规范化）")
    reference_moisture_pct: str = Field(
        description="参考含水率（%，原样字符串），0 ~ 40"
    )


class CalibrationCurveOut(BaseModel):
    """校准曲线创建响应：编号、传感器编号、对照点与创建时间（不可变记录）。"""

    curve_no: str = Field(description="曲线编号")
    sensor_id: str = Field(description="传感器编号")
    points: list[CalibrationPointOut] = Field(description="严格递增的对照点")
    created_at: str = Field(description="创建时间（UTC ISO 8601）")


class MoistureConversionIn(BaseModel):
    """换算请求：曲线编号 + 单个原始信号（有限十进制数，小数位数不限）。"""

    model_config = ConfigDict(extra="forbid")

    curve_no: str = Field(min_length=1, max_length=64, description="换算所依据的校准曲线编号")
    raw_signal: Decimal = Field(
        description="待换算的单个原始信号；落在曲线范围外以 422 拒绝，不做外推"
    )


class ConversionIntervalOut(BaseModel):
    """命中区间的两端点（原始信号与对应参考含水率，原样定点串）。"""

    lower_signal: str = Field(description="区间下端点原始信号")
    upper_signal: str = Field(description="区间上端点原始信号")
    lower_moisture_pct: str = Field(description="下端点参考含水率（%）")
    upper_moisture_pct: str = Field(description="上端点参考含水率（%）")


class MoistureConversionOut(BaseModel):
    """换算响应：命中区间端点与 ROUND_HALF_UP 保留三位的含水率（定点串）。"""

    curve_no: str = Field(description="实际使用的校准曲线编号")
    raw_signal: str = Field(description="接收到的原始信号（定点串）")
    interval: ConversionIntervalOut = Field(description="命中的相邻区间端点")
    moisture_pct: str = Field(
        description="换算含水率（%）：Decimal 线性插值后按 ROUND_HALF_UP 保留三位"
    )
