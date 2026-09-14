"""FastAPI 装配：端点、统一 422 错误反馈（定位到骨料下标）。"""
from __future__ import annotations

import os
from typing import Any, Sequence

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app import sampling_service
from app.calculator import correct_batch
from app.sampling_repository import (
    BatchAlreadyConfirmedError,
    BatchNotFoundError,
    InvalidReadingIndexError,
    ReadingIndexOutOfBoundsError,
    RevisionConflictError,
    SamplingBatchRepository,
    parse_reading_index,
)
from app.sampling_schemas import (
    MoistureReadingRevisionIn,
    RevisedSamplingBatchOut,
    SamplingBatchCreate,
    SamplingBatchOut,
)
from app.schemas import CorrectionRequest, CorrectionSheetOut
from app.summary import build_summary, round3

# 取样批次 SQLite 库路径（标准库持久化，无独立数据库服务）；
# 可用 SAMPLING_DB_PATH 覆盖（compose 中挂到共享卷）。
DEFAULT_SAMPLING_DB_PATH = "data/moisture_batches.db"

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
    "int_type": "必须是整数",
    "string_type": "必须是字符串",
    "list_type": "必须是数组",
    "extra_forbidden": "无法识别的字段",
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


# --------------------------------------------------------------------------
# 取样批次（烘干法原始称量留痕 → 确认出代表含水率）
# --------------------------------------------------------------------------

_sampling_repo: SamplingBatchRepository | None = None


def get_sampling_repository() -> SamplingBatchRepository:
    """进程级单例 SQLite 仓储；SAMPLING_DB_PATH 可覆盖库文件位置。

    测试以 FastAPI 的 dependency_overrides 替换为临时库仓储。
    """
    global _sampling_repo
    if _sampling_repo is None:
        db_path = os.environ.get("SAMPLING_DB_PATH", DEFAULT_SAMPLING_DB_PATH)
        _sampling_repo = SamplingBatchRepository(db_path)
    return _sampling_repo


def _error(status_code: int, field: str, message: str, err_type: str) -> JSONResponse:
    """结构化错误：沿用 detail 数组契约。"""
    return JSONResponse(
        status_code=status_code,
        content={"detail": [{"field": field, "message": message, "type": err_type}]},
    )


@app.post(
    "/api/v1/moisture-batches",
    response_model=SamplingBatchOut,
    status_code=201,
    summary="创建取样批次（烘干法原始称量，待确认）",
)
def create_moisture_batch(
    payload: SamplingBatchCreate,
    repo: SamplingBatchRepository = Depends(get_sampling_repository),
) -> SamplingBatchOut:
    """接收料堆名称与 2 ~ 5 组湿样/干样质量，生成批次编号并以“待确认”保存。

    只留原始称量，不在表外换算百分率；非法称量（非正、干样不小于湿样、
    未知字段、组数越界）由 Pydantic 在入口以 422 拒绝，不落任何数据。
    """
    return sampling_service.create_batch(repo, payload)


@app.patch(
    "/api/v1/moisture-batches/{batch_no}/readings/{index}",
    response_model=RevisedSamplingBatchOut,
    summary="修订待确认批次的一组称量（留下审计痕迹）",
    status_code=200,
)
def revise_moisture_batch_reading(
    batch_no: str,
    index: str,
    payload: MoistureReadingRevisionIn,
    repo: SamplingBatchRepository = Depends(get_sampling_repository),
) -> RevisedSamplingBatchOut | JSONResponse:
    """原子替换待确认批次中的一组湿样/干样质量。

    成功时同时递增 revision_no，并保存修改前后质量与修订时间；请求中的
    revision_no 必须是客户端最新已见值。编号不存在返回 404，下标越界
    返回定位到 index 的 422；已确认或修订号过期返回 409，冲突不写入读数，
    也不追加审计记录。
    """
    try:
        reading_index = parse_reading_index(index)
    except InvalidReadingIndexError:
        return _error(
            422,
            "index",
            f"读数下标必须是整数：{index}",
            "reading_index_invalid",
        )
    try:
        return sampling_service.revise_batch_reading(
            repo, batch_no, reading_index, payload
        )
    except BatchNotFoundError:
        return _error(
            404,
            "batch_no",
            f"取样批次不存在：{batch_no}",
            "batch_not_found",
        )
    except BatchAlreadyConfirmedError:
        return _error(
            409,
            "batch_no",
            f"取样批次已确认，称量不可修订：{batch_no}",
            "batch_already_confirmed",
        )
    except RevisionConflictError as exc:
        _batch_no, current, expected = exc.args[0]
        return _error(
            409,
            "revision_no",
            f"修订号已过期：客户端 {expected}，服务端当前 {current}",
            "revision_conflict",
        )
    except ReadingIndexOutOfBoundsError as exc:
        _batch_no, bad_index = exc.args[0]
        return _error(
            422,
            "index",
            f"读数下标越界：{bad_index}",
            "reading_index_out_of_bounds",
        )


@app.post(
    "/api/v1/moisture-batches/{batch_no}/confirm",
    response_model=SamplingBatchOut,
    summary="确认取样批次（计算各组含水率与中位数代表值）",
)
def confirm_moisture_batch(
    batch_no: str,
    repo: SamplingBatchRepository = Depends(get_sampling_repository),
) -> SamplingBatchOut | JSONResponse:
    """确认批次：（湿样 − 干样）÷ 干样 × 100 逐组计算，中位数形成代表含水率。

    代表值按 ROUND_HALF_UP 保留三位并置“已确认”。编号不存在返回结构化
    404；重复确认返回 409 且已确认数据不可改动。
    """
    try:
        return sampling_service.confirm_batch(repo, batch_no)
    except BatchNotFoundError:
        return _error(
            404,
            "batch_no",
            f"取样批次不存在：{batch_no}",
            "batch_not_found",
        )
    except BatchAlreadyConfirmedError:
        return _error(
            409,
            "batch_no",
            f"取样批次已确认，结果不可改动：{batch_no}",
            "batch_already_confirmed",
        )
