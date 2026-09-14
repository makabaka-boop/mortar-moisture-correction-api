"""取样批次模块的请求/响应契约（Pydantic）。

烘干法原始称量直接成批留痕，不在表外换算百分率：
- 创建时只接收料堆名称与 2 ~ 5 组湿样/干样质量，以“待确认”保存；
- 确认前可按批次编号和读数下标修订一组称量，请求同时携带客户端已见修订号；
- 确认时才由（湿样质量 − 干样质量）÷ 干样质量 × 100 计算各组结果，
  以中位数形成代表含水率并置为“已确认”。

约束：
- wet_sample_mass / dry_sample_mass 为正质量（单位 g），小数位数不限；
- 同组干样质量必须严格小于湿样质量（等于即零含水率，视为称量或录入错误），
  错误定位到 readings[i].dry_sample_mass；
- readings 数量 2 ~ 5 组；
- 无法识别的字段一律拒绝（extra="forbid"），不静默忽略。
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

# 批次状态原文：创建“待确认”→ 确认后“已确认”（已确认即不可改动）
STATUS_PENDING = "待确认"
STATUS_CONFIRMED = "已确认"

MIN_READINGS = 2
MAX_READINGS = 5


def _fixed_point(value: Decimal) -> str:
    """定点字符串（如 '100.000'、'5.12345'），避免科学记数法往返失真。"""
    return format(value, "f")


class MoistureReadingIn(BaseModel):
    """一组烘干法原始称量：湿样质量与干样质量（单位 g）。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    wet_sample_mass: Decimal = Field(
        gt=0,
        description="湿样质量（g），必须大于零，小数位数不限",
    )
    dry_sample_mass: Decimal = Field(
        gt=0,
        description="干样质量（g），必须大于零且严格小于湿样质量，小数位数不限",
    )

    @field_validator("dry_sample_mass")
    @classmethod
    def _dry_strictly_below_wet(cls, value: Decimal, info) -> Decimal:
        """干样质量必须严格小于湿样质量。

        湿样字段先于干样完成基础校验：缺失/非正/不可解析时本校验器拿到的
        湿样值不是合法正 Decimal，直接放行，由其自身的错误反馈定位即可，
        避免一条称量同时报两个错。
        """
        wet = info.data.get("wet_sample_mass")
        if isinstance(wet, Decimal) and value >= wet:
            raise PydanticCustomError(
                "dry_not_below_wet",
                "干样质量必须严格小于湿样质量（湿样 {wet} g，干样 {dry} g）",
                {"wet": _fixed_point(wet), "dry": _fixed_point(value)},
            )
        return value


class SamplingBatchCreate(BaseModel):
    """取样批次创建请求：料堆名称 + 2 ~ 5 组原始称量。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    pile_name: str = Field(min_length=1, max_length=64, description="料堆名称")
    readings: list[MoistureReadingIn] = Field(
        min_length=MIN_READINGS,
        max_length=MAX_READINGS,
        description="原始称量组，2 ~ 5 组，每组含湿样/干样质量（g）",
    )


class MoistureReadingRevisionIn(MoistureReadingIn):
    """待确认批次中单组称量的修订请求。

    复用单组称量的全部质量校验；revision_no 是客户端已见的批次修订号，
    用于乐观并发控制（新建批次为 0，每次成功修订加 1）。
    """

    revision_no: int = Field(
        ge=0,
        description="客户端已见修订号；与服务端当前值不一致时返回 409",
    )


class MoistureReadingOut(BaseModel):
    """单组称量及其含水率结果（待确认时 moisture_pct 为 null）。

    质量为创建时的原始读数定点串；含水率为完整精度（仅代表值出口舍入）。
    """

    index: int = Field(description="称量组下标，从 0 开始")
    wet_sample_mass: str = Field(description="湿样质量原始读数（g）")
    dry_sample_mass: str = Field(description="干样质量原始读数（g）")
    moisture_pct: str | None = Field(
        default=None,
        description="组含水率（%）=（湿样 − 干样）÷ 干样 × 100，完整精度；待确认时为 null",
    )


class SamplingBatchOut(BaseModel):
    """取样批次响应：创建回显与确认结果共用同一契约。"""

    batch_no: str = Field(description="批次编号")
    pile_name: str = Field(description="料堆名称")
    status: str = Field(description="状态：待确认 / 已确认")
    readings: list[MoistureReadingOut] = Field(description="原始称量与各组结果")
    representative_moisture_pct: str | None = Field(
        default=None,
        description="代表含水率（%）：组含水率中位数，ROUND_HALF_UP 保留三位；待确认时为 null",
    )
    created_at: str = Field(description="创建时间（UTC ISO 8601）")
    confirmed_at: str | None = Field(
        default=None, description="确认时间（UTC ISO 8601），待确认时为 null"
    )


class RevisedSamplingBatchOut(SamplingBatchOut):
    """修订接口响应：在原批次契约上补充当前批次修订号。"""

    revision_no: int = Field(
        description="批次修订号：新建及旧库迁移为 0，每次成功修订单组读数后加 1"
    )
