"""请求校验模块：以 Pydantic 模型定义修正单请求/响应契约。

约束（端点均包含）：
- 含水率 moisture_pct:   0 ~ 40（质量百分数）
- 吸水率 absorption_pct: 0 ~ 15（质量百分数）
- 所有质量（干基目标质量、设计加水量）必须大于零，单位 kg
- 骨料数量 1 ~ 8 种

小数位数不限：高精度输入一律受理，仅按上述有效范围校验。
"""
from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_serializer

MOISTURE_MIN = Decimal("0")
MOISTURE_MAX = Decimal("40")
ABSORPTION_MIN = Decimal("0")
ABSORPTION_MAX = Decimal("15")

MIN_AGGREGATES = 1
MAX_AGGREGATES = 8


class AggregateIn(BaseModel):
    """单种骨料的输入。"""

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=64, description="骨料名称")
    dry_mass_kg: Decimal = Field(
        gt=0,
        description="干基目标质量（kg），必须大于零，小数位数不限",
    )
    moisture_pct: Decimal = Field(
        ge=MOISTURE_MIN,
        le=MOISTURE_MAX,
        description="含水率（质量百分数），0 ~ 40，端点包含，小数位数不限",
    )
    absorption_pct: Decimal = Field(
        ge=ABSORPTION_MIN,
        le=ABSORPTION_MAX,
        description="吸水率（质量百分数），0 ~ 15，端点包含，小数位数不限",
    )


class CorrectionRequest(BaseModel):
    """修正单请求：设计加水量 + 1~8 种骨料。"""

    design_water_kg: Decimal = Field(
        gt=0,
        description="设计加水量（kg），必须大于零，小数位数不限",
    )
    aggregates: list[AggregateIn] = Field(
        min_length=MIN_AGGREGATES,
        max_length=MAX_AGGREGATES,
        description="骨料清单，1 ~ 8 种",
    )

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "design_water_kg": "180",
                    "aggregates": [
                        {
                            "name": "河砂A",
                            "dry_mass_kg": "800",
                            "moisture_pct": "5.0",
                            "absorption_pct": "1.0",
                        },
                        {
                            "name": "机制砂B",
                            "dry_mass_kg": "600",
                            "moisture_pct": "3.5",
                            "absorption_pct": "0.5",
                        },
                    ],
                }
            ]
        }
    )


def _decimal_to_str(value: Decimal) -> str:
    """三位小数字符串输出，保留尾随零（如 '840.000'），避免浮点误差。"""
    return format(value, "f")


class AggregateCorrectionOut(BaseModel):
    """单种骨料的修正结果（质量均已按 ROUND_HALF_UP 保留三位小数）。"""

    index: int = Field(description="骨料下标，从 0 开始")
    name: str
    dry_mass_kg: Decimal = Field(description="干基目标质量（kg）")
    wet_mass_kg: Decimal = Field(description="湿投料量（kg）")
    free_water_kg: Decimal = Field(description="自由水量（kg），可为负")

    @field_serializer("dry_mass_kg", "wet_mass_kg", "free_water_kg")
    def _ser_decimal(self, value: Decimal) -> str:
        return _decimal_to_str(value)


class CorrectionSheetOut(BaseModel):
    """修正单响应：逐项结果 + 批次汇总（质量均为三位小数字符串）。"""

    items: list[AggregateCorrectionOut]
    item_count: int = Field(description="骨料种类数")
    total_dry_mass_kg: Decimal = Field(description="干基目标质量合计（kg）")
    total_wet_mass_kg: Decimal = Field(description="湿投料量合计（kg）")
    total_free_water_kg: Decimal = Field(description="自由水量合计（kg）")
    design_water_kg: Decimal = Field(description="设计加水量（kg）")
    final_water_kg: Decimal = Field(description="最终加水量（kg），>= 0")

    @field_serializer(
        "total_dry_mass_kg",
        "total_wet_mass_kg",
        "total_free_water_kg",
        "design_water_kg",
        "final_water_kg",
    )
    def _ser_decimal(self, value: Decimal) -> str:
        return _decimal_to_str(value)
