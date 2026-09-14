"""请求校验模块：以 Pydantic 模型定义修正单请求/响应契约。

约束（端点均包含）：
- 含水率 moisture_pct:   0 ~ 40（质量百分数）
- 吸水率 absorption_pct: 0 ~ 15（质量百分数）
- 所有质量（干基目标质量、设计加水量）必须大于零，单位 kg
- 骨料数量 1 ~ 8 种
- 目标干料总量 target_dry_total_kg 可选，传入时必须大于零且量级在可计算
  范围内；显式提交空值（null）视为非法输入，只有省略该字段才按不缩放处理
- 无法识别的请求字段（如误写的目标字段名）一律拒绝，不静默忽略

小数位数不限：高精度输入一律受理，仅按上述有效范围校验。
"""
from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator
from pydantic_core import PydanticCustomError

MOISTURE_MIN = Decimal("0")
MOISTURE_MAX = Decimal("40")
ABSORPTION_MIN = Decimal("0")
ABSORPTION_MAX = Decimal("15")

MIN_AGGREGATES = 1
MAX_AGGREGATES = 8

# 目标干料总量的可计算量级界：Decimal.adjusted()（以 10 为底的量级指数）的绝对值上限。
# 计算上下文精度随量级自适应（约为量级的 4 倍，见 calculator._required_precision），
# 而 decimal 上下文精度上限约为 1e18；响应又以定点字符串输出，量级过大时响应体本身
# 即不可行。±28 与默认十进制精度同阶，远超任何实际批次，且为计算管道各阶段
# （精度估算、缩放除法、quantize、定点格式化）留出充足余量。
TARGET_MAX_ADJUSTED = 28


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
    """修正单请求：设计加水量 + 1~8 种骨料，可选目标干料总量。

    目标干料总量仅在省略该字段时才按不缩放处理：显式提交空值（null）
    或超出可计算量级的值均以 422 拒绝并定位该字段。无法识别的请求字段
    （如误写的字段名）同样拒绝，不静默忽略。
    """

    design_water_kg: Decimal = Field(
        gt=0,
        description="设计加水量（kg），必须大于零，小数位数不限",
    )
    target_dry_total_kg: Decimal | None = Field(
        default=None,
        gt=0,
        description=(
            "目标干料总量（kg），可选，必须大于零且量级在可计算范围内；传入后以"
            "原骨料干基合计为基准求缩放系数，同比缩放各项干基质量与设计加水量"
        ),
    )
    aggregates: list[AggregateIn] = Field(
        min_length=MIN_AGGREGATES,
        max_length=MAX_AGGREGATES,
        description="骨料清单，1 ~ 8 种",
    )

    @field_validator("target_dry_total_kg", mode="before")
    @classmethod
    def _reject_explicit_null_target(cls, value: object) -> object:
        """显式提交空值（null）视为非法输入；省略该字段时才按不缩放处理。

        before 校验器仅在字段被显式传入时触发，省略字段走默认值 None，
        二者由此得以区分。
        """
        if value is None:
            raise PydanticCustomError(
                "empty_target",
                "不能显式提交空的目标干料总量；若不需要同比缩放，请省略该字段",
            )
        return value

    @field_validator("target_dry_total_kg", mode="after")
    @classmethod
    def _target_within_computable_range(
        cls, value: Decimal | None
    ) -> Decimal | None:
        """拒绝超出可计算量级范围的目标值（此时值已保证为正的有限小数）。

        自适应上下文精度、缩放除法与定点响应串的长度均随量级增长，
        超出界限的输入会在计算管道中抛出未处理异常或耗尽资源，
        必须在请求校验阶段定位字段并明确拒绝。
        """
        if value is not None and abs(value.adjusted()) > TARGET_MAX_ADJUSTED:
            raise PydanticCustomError(
                "target_out_of_computable_range",
                "超出可计算范围：目标干料总量的量级须在 1e-28 ~ 1e28 kg 之间",
            )
        return value

    model_config = ConfigDict(
        # 无法识别的字段（如误写的目标字段名）一律拒绝，不静默忽略
        extra="forbid",
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
    """修正单响应：逐项结果 + 批次汇总（质量均为三位小数字符串）。

    仅在请求传入目标干料总量时，响应才携带 target_dry_total_kg 与
    scale_factor（由端点以 exclude_none 序列化保证未传时响应原样）。
    """

    items: list[AggregateCorrectionOut]
    item_count: int = Field(description="骨料种类数")
    total_dry_mass_kg: Decimal = Field(description="干基目标质量合计（kg）")
    total_wet_mass_kg: Decimal = Field(description="湿投料量合计（kg）")
    total_free_water_kg: Decimal = Field(description="自由水量合计（kg）")
    design_water_kg: Decimal = Field(description="设计加水量（kg）")
    final_water_kg: Decimal = Field(description="最终加水量（kg），>= 0")
    target_dry_total_kg: Decimal | None = Field(
        default=None, description="目标干料总量（kg），三位小数"
    )
    scale_factor: Decimal | None = Field(
        default=None,
        description="缩放系数 = 目标干料总量 / 原骨料干基合计，ROUND_HALF_UP 保留六位小数",
    )

    @field_serializer(
        "total_dry_mass_kg",
        "total_wet_mass_kg",
        "total_free_water_kg",
        "design_water_kg",
        "final_water_kg",
    )
    def _ser_decimal(self, value: Decimal) -> str:
        return _decimal_to_str(value)

    @field_serializer("target_dry_total_kg", "scale_factor")
    def _ser_optional_decimal(self, value: Decimal | None) -> str | None:
        return None if value is None else _decimal_to_str(value)
