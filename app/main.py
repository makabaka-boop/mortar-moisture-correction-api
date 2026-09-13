"""FastAPI 装配：端点、统一 422 错误反馈（定位到骨料下标）。"""
from __future__ import annotations

from typing import Any, Sequence

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.calculator import correct_batch
from app.schemas import CorrectionRequest, CorrectionSheetOut
from app.summary import build_summary, round3

app = FastAPI(
    title="预拌砂浆含水修正 API",
    version="1.0.0",
    description=(
        "雨后砂堆含水变化时，按干配方直接投料会同时偏离骨料量与实际加水量。"
        "本服务根据各骨料含水率/吸水率计算湿投料量、自由水量与最终加水量，"
        "输出可复算的修正单。"
    ),
)

# 常见校验错误的中文提示，未覆盖的类型回退到 Pydantic 原始信息
_MESSAGE_MAP = {
    "greater_than": "必须大于 {gt}",
    "greater_than_equal": "必须大于或等于 {ge}",
    "less_than": "必须小于 {lt}",
    "less_than_equal": "必须小于或等于 {le}",
    "too_short": "数量或长度不足，至少需要 {min_length}",
    "too_long": "数量或长度超限，最多允许 {max_length}",
    "missing": "缺少必填字段",
    "decimal_parsing": "不是合法的十进制数",
    "string_type": "必须是字符串",
    "list_type": "必须是数组",
}


def _format_field(loc: Sequence[Any]) -> str:
    """把 Pydantic 的 loc 元组格式化为路径，骨料错误定位到下标。

    例：("body", "aggregates", 2, "moisture_pct") -> "aggregates[2].moisture_pct"
    """
    parts = [p for p in loc if p != "body"]
    out = ""
    for part in parts:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += ("." if out else "") + str(part)
    return out or "body"


def _format_message(error: dict) -> str:
    template = _MESSAGE_MAP.get(error.get("type"))
    if template is None:
        return error.get("msg", "校验失败")
    ctx = error.get("ctx") or {}
    try:
        return template.format(**ctx)
    except (KeyError, IndexError):
        return error.get("msg", "校验失败")


@app.exception_handler(RequestValidationError)
async def request_validation_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """统一 422 反馈：每条错误都带定位到骨料下标的 field 路径。"""
    detail = [
        {
            "field": _format_field(err.get("loc", ())),
            "message": _format_message(err),
            "type": err.get("type", "unknown"),
        }
        for err in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": detail})


@app.get("/health", summary="健康检查")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/api/v1/correction-sheet",
    response_model=CorrectionSheetOut,
    # 未传目标干料总量时剔除 target_dry_total_kg / scale_factor，响应保持原样
    response_model_exclude_none=True,
    summary="生成砂浆含水修正单",
)
def correction_sheet(payload: CorrectionRequest) -> CorrectionSheetOut | JSONResponse:
    """按干配方与各骨料含水/吸水率计算湿投料清单与最终加水量。

    传入目标干料总量时，以原骨料干基合计为基准同比缩放全单。
    最终加水量为零合法；小于零时以 422 整体拒绝，不返回部分修正单。
    """
    correction = correct_batch(
        payload.design_water_kg, payload.aggregates, payload.target_dry_total_kg
    )
    if correction.final_water_kg < 0:
        detail = [
            {
                "field": "final_water_kg",
                "message": (
                    f"最终加水量 {round3(correction.final_water_kg)} kg 小于零"
                    f"（设计加水量 {round3(correction.design_water_kg)} kg，"
                    f"自由水量合计 {round3(correction.total_free_water_kg)} kg），整单拒绝"
                ),
                "type": "negative_final_water",
            }
        ]
        # 直接返回 JSONResponse，保证错误结构与校验错误一致，且不返回部分修正单
        return JSONResponse(
            status_code=422, content={"detail": detail}
        )
    return build_summary(correction)
