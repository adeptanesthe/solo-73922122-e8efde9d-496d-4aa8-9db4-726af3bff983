"""方案状态机：粗测评估、方案号更正、凭现行方案号提交精测。"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional, Union

from . import engine

InstrumentId = Union[int, str]


class ServiceError(Exception):
    """带 HTTP 语义的业务错误。"""

    def __init__(self, status_code: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra = extra


@dataclass
class Plan:
    code: str
    coarse: engine.CoarseRange
    tree: engine.Node
    tree_raw: dict[str, Any]
    instruments: list[engine.Instrument]
    status: str = "active"  # active | superseded | completed
    superseded_by: Optional[str] = None
    generation: int = 1
    result: Optional[dict[str, Any]] = None
    history: list[dict[str, Any]] = field(default_factory=list)

    def instruments_payload(self) -> list[dict[str, Any]]:
        return [
            {"instrument_id": inst.id, "step": inst.step}
            for inst in self.instruments
        ]

    def assessment(self) -> dict[str, Any]:
        report = engine.assess(self.coarse, self.tree, self.instruments)
        report["plan_code"] = self.code
        report["status"] = self.status
        return report


def _new_plan_code() -> str:
    return uuid.uuid4().hex[:12]


class PlanService:
    """进程内方案存储。多副本部署时应替换为共享存储。"""

    def __init__(self) -> None:
        self._plans: dict[str, Plan] = {}
        self._lock = threading.Lock()

    # -- 创建 ---------------------------------------------------------------

    def create_plan(
        self,
        coarse: engine.CoarseRange,
        tree_raw: dict[str, Any],
        instruments: list[engine.Instrument],
    ) -> Plan:
        tree = engine.parse_tree(tree_raw)
        error = coarse.error()
        if error is not None:
            raise ServiceError(422, "range_infeasible", error)
        _check_instrument_steps(instruments)
        with self._lock:
            plan = Plan(
                code=_new_plan_code(),
                coarse=coarse,
                tree=tree,
                tree_raw=tree_raw,
                instruments=instruments,
            )
            self._plans[plan.code] = plan
            return plan

    # -- 查询 ---------------------------------------------------------------

    def get_plan(self, code: str) -> Plan:
        with self._lock:
            plan = self._plans.get(code)
        if plan is None:
            raise ServiceError(404, "unknown_plan", "方案号不存在")
        return plan

    # -- 更正粗测 -----------------------------------------------------------

    def correct_coarse(self, code: str, coarse: engine.CoarseRange) -> Plan:
        with self._lock:
            plan = self._plans.get(code)
            if plan is None:
                raise ServiceError(404, "unknown_plan", "方案号不存在")
            if plan.status == "superseded":
                raise ServiceError(
                    409,
                    "plan_superseded",
                    "该方案号已被作废",
                    current_plan_code=plan.superseded_by,
                )
            if plan.status == "completed":
                raise ServiceError(409, "plan_completed", "本轮已结束，不能再更正粗测")

            error = coarse.error()
            if error is not None:
                # 更正无解：保留现状，当前方案号继续有效。
                raise ServiceError(422, "range_infeasible", error)

            new_plan = Plan(
                code=_new_plan_code(),
                coarse=coarse,
                tree=plan.tree,
                tree_raw=plan.tree_raw,
                instruments=plan.instruments,
                generation=plan.generation + 1,
            )
            new_plan.history = list(plan.history) + [
                {
                    "event": "coarse_corrected",
                    "from_plan_code": plan.code,
                    "to_plan_code": new_plan.code,
                    "previous_coarse": plan.coarse.as_dict(),
                    "new_coarse": coarse.as_dict(),
                }
            ]
            plan.status = "superseded"
            plan.superseded_by = new_plan.code
            self._plans[new_plan.code] = new_plan
            return new_plan

    # -- 精测 ---------------------------------------------------------------

    def submit_precision(
        self, code: str, instrument_id: InstrumentId, reading: int
    ) -> dict[str, Any]:
        with self._lock:
            plan = self._plans.get(code)
            if plan is None:
                raise ServiceError(404, "unknown_plan", "方案号不存在")
            if plan.status == "superseded":
                raise ServiceError(
                    409,
                    "plan_superseded",
                    "该方案号已被作废，请使用现行方案号",
                    current_plan_code=plan.superseded_by,
                )
            if plan.status == "completed":
                raise ServiceError(409, "plan_completed", "本轮已结束，不能重复提交精测")

            instrument = next(
                (inst for inst in plan.instruments if inst.id == instrument_id), None
            )
            if instrument is None:
                raise ServiceError(
                    404, "instrument_not_in_plan", "该仪表不在本方案号中"
                )

            assessment = engine.assess(plan.coarse, plan.tree, plan.instruments)
            usable = {item["instrument_id"] for item in assessment["usable_instruments"]}
            if instrument_id not in usable:
                # 精测被拒：保留现状。
                raise ServiceError(
                    409,
                    "instrument_not_usable",
                    "该仪表在本方案下无法保证同档同判，精测被拒",
                )

            verdict = engine.reading_verdict(
                plan.coarse, plan.tree, instrument, reading
            )
            if verdict is None:
                raise ServiceError(
                    409,
                    "reading_not_feasible",
                    "该档值在本方案下不可行（越界、不是步长整倍数，或判定不唯一）",
                )

            reading_class = engine.reading_class(plan.coarse, instrument, reading)
            plan.status = "completed"
            plan.result = {
                "verdict": verdict,
                "instrument_id": instrument_id,
                "step": instrument.step,
                "reading": reading,
                "outer_reading_class": list(reading_class),
                "note": "外层实测档值唯一确定判定，本轮结束",
            }
            plan.history = list(plan.history) + [
                {
                    "event": "precision_submitted",
                    "instrument_id": instrument_id,
                    "reading": reading,
                    "verdict": verdict,
                }
            ]
            return {
                "plan_code": plan.code,
                "status": plan.status,
                "round_ended": True,
                **plan.result,
            }

    # -- 测试辅助 -----------------------------------------------------------

    def reset(self) -> None:
        with self._lock:
            self._plans.clear()


def _check_instrument_steps(instruments: list[engine.Instrument]) -> None:
    if not instruments:
        raise ServiceError(422, "no_instruments", "至少需要一台待选仪表")
    for inst in instruments:
        if not isinstance(inst.step, int) or isinstance(inst.step, bool) or inst.step <= 0:
            raise ServiceError(422, "invalid_step", "仪表步长必须是正整数")


def coarse_from_payload(payload: Any) -> engine.CoarseRange:
    """从已校验的 pydantic 模型构造 CoarseRange。"""

    outer = payload.outer
    inner = payload.inner
    total = payload.total
    return engine.CoarseRange(
        outer_min=outer[0],
        outer_max=outer[1],
        inner_min=inner[0],
        inner_max=inner[1],
        total_min=total[0],
        total_max=total[1],
    )
