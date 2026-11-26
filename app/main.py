"""FastAPI 判定 API：粗测方案评估、方案号更正、凭方案号提交精测。"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import engine
from .schemas import (
    InstrumentModel,
    PlanRequest,
    PrecisionRequest,
    RangeModel,
)
from .service import PlanService, ServiceError, coarse_from_payload

app = FastAPI(
    title="双层复合膜厚度判定 API",
    description=(
        "接收两层厚度与总厚度的整数闭区间、最多 12 个叶节点的合格判定树，"
        "以及待选测厚仪编号与正整数步长；列出与合格判定一致的仪表，"
        "并凭现行方案号提交精测档值。"
    ),
    version="1.0.0",
)

service = PlanService()


# ---------------------------------------------------------------------------
# 统一错误格式
# ---------------------------------------------------------------------------


def _error_body(code: str, message: str, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"error": {"code": code, "message": message}}
    body["error"].update(extra)
    return body


@app.exception_handler(ServiceError)
async def _service_error_handler(request: Request, exc: ServiceError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_body(exc.code, exc.message, **exc.extra),
    )


@app.exception_handler(RequestValidationError)
async def _validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    details = []
    for item in exc.errors():
        details.append(
            {
                "loc": list(item.get("loc", ())),
                "msg": item.get("msg", ""),
                "type": item.get("type", ""),
            }
        )
    return JSONResponse(
        status_code=422,
        content=_error_body("invalid_request", "请求体不合法", details=details),
    )


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _to_engine_instruments(items: list[InstrumentModel]) -> list[engine.Instrument]:
    return [engine.Instrument(id=item.id, step=item.step) for item in items]


def _create_plan(payload: PlanRequest) -> dict[str, Any]:
    coarse = coarse_from_payload(payload.coarse)
    try:
        plan = service.create_plan(
            coarse, payload.tree, _to_engine_instruments(payload.instruments)
        )
    except engine.TreeError as exc:
        raise ServiceError(422, "invalid_tree", str(exc)) from exc
    return _plan_response(plan)


def _plan_response(plan) -> dict[str, Any]:
    assessment = plan.assessment()
    return {
        "plan_code": plan.code,
        "status": plan.status,
        "generation": plan.generation,
        "coarse": plan.coarse.as_dict(),
        "instruments": plan.instruments_payload(),
        "assessment": {
            "range_feasible": assessment["range_feasible"],
            "range_error": assessment["range_error"],
            "solvable": assessment["solvable"],
            "usable_instruments": assessment["usable_instruments"],
            "unusable_instruments": assessment["unusable_instruments"],
        },
        "history": list(plan.history),
    }


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


@app.get("/healthz", summary="健康检查")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/plans",
    status_code=201,
    summary="提交粗测范围、判定树与待选仪表，创建方案号并评估仪表",
)
def create_plan(payload: PlanRequest) -> dict[str, Any]:
    return _create_plan(payload)


@app.post(
    "/assess",
    summary="仅评估、不落库（同样返回可用/不可用仪表与见证）",
)
def assess(payload: PlanRequest) -> dict[str, Any]:
    coarse = coarse_from_payload(payload.coarse)
    try:
        tree = engine.parse_tree(payload.tree)
    except engine.TreeError as exc:
        raise ServiceError(422, "invalid_tree", str(exc)) from exc
    error = coarse.error()
    if error is not None:
        raise ServiceError(422, "range_infeasible", error)
    report = engine.assess(
        coarse, tree, _to_engine_instruments(payload.instruments)
    )
    return {
        "coarse": coarse.as_dict(),
        "assessment": {
            "range_feasible": report["range_feasible"],
            "range_error": report["range_error"],
            "solvable": report["solvable"],
            "usable_instruments": report["usable_instruments"],
            "unusable_instruments": report["unusable_instruments"],
        },
    }


@app.get("/plans/{plan_code}", summary="查询方案当前状态")
def get_plan(plan_code: str) -> dict[str, Any]:
    plan = service.get_plan(plan_code)
    body = _plan_response(plan)
    body["superseded_by"] = plan.superseded_by
    body["result"] = plan.result
    return body


@app.post(
    "/plans/{plan_code}/coarse",
    summary="更正粗测范围：新范围有解则换方案号并作废旧号",
)
def correct_coarse(plan_code: str, payload: RangeModel) -> dict[str, Any]:
    coarse = coarse_from_payload(payload)
    plan = service.correct_coarse(plan_code, coarse)
    return _plan_response(plan)


@app.post(
    "/plans/{plan_code}/precision",
    summary="凭现行方案号提交合用仪表与实测档值；成功即结束本轮",
)
def submit_precision(plan_code: str, payload: PrecisionRequest) -> dict[str, Any]:
    return service.submit_precision(
        plan_code, payload.instrument_id, payload.reading
    )


@app.get("/plans/{plan_code}/readings/{instrument_id}", summary="查询某仪表的可行档值")
def instrument_readings(plan_code: str, instrument_id: str) -> dict[str, Any]:
    plan = service.get_plan(plan_code)
    instrument = next(
        (
            inst
            for inst in plan.instruments
            if str(inst.id) == instrument_id
        ),
        None,
    )
    if instrument is None:
        raise ServiceError(404, "instrument_not_in_plan", "该仪表不在本方案号中")
    report = engine.classify_instrument(
        instrument, engine.collect_cells(plan.tree, plan.coarse), plan.coarse
    )
    return {
        "plan_code": plan.code,
        "instrument_id": instrument.id,
        "step": instrument.step,
        "usable": report["usable"],
        "reading_intervals": report.get("reading_intervals"),
        "conflict": report.get("conflict"),
    }
