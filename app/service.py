"""方案状态机：粗测评估、方案号更正、凭现行方案号提交精测。

性能模型
--------
每个方案号在创建/更正时**只编译一次**：

1. 判定树按该方案号的粗测范围裁剪一次（得到叶矩形）；
2. 每种步长只分档一次（得到 :class:`engine.StepProfile`）；
3. 各仪表的报告由所属步长的分档结果派生，保留各自编号。

之后查询方案、查询档值、提交精测都直接读这份编译结果，不再逐台、逐请求
重算。编译在全局锁**之外**完成，因此一个批次在算分档时，其他批次仍能
查询方案、提交精测。

一致性模型
----------
:class:`Plan` 是不可变快照：状态、评估结果、精测结论与历史总是一起原子
替换。一次查询只取同一个 Plan 对象，四者必然对应同一时点。每次迁移都
让 ``revision`` 加一并替换对象；更正与精测以"提交前快照仍是当前对象"作为
比较并交换（CAS）条件，因此两者同时提交时，同一方案号最多只有一项成功。
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field, replace
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


@dataclass(frozen=True)
class Plan:
    """方案号的不可变快照。

    所有字段在一次状态迁移中一起替换，读取方拿到的对象内部自洽。
    """

    code: str
    coarse: engine.CoarseRange
    tree: engine.Node
    tree_raw: dict[str, Any]
    instruments: tuple[engine.Instrument, ...]
    analysis: engine.CompiledAnalysis
    status: str = "active"  # active | superseded | completed
    superseded_by: Optional[str] = None
    generation: int = 1
    revision: int = 1
    result: Optional[dict[str, Any]] = None
    history: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def instruments_payload(self) -> list[dict[str, Any]]:
        return [
            {"instrument_id": inst.id, "step": inst.step}
            for inst in self.instruments
        ]

    def assessment(self) -> dict[str, Any]:
        """读取编译期缓存的评估结果，不做任何重算。"""

        analysis = self.analysis
        return {
            "plan_code": self.code,
            "status": self.status,
            "range_feasible": analysis.range_feasible,
            "range_error": analysis.range_error,
            "solvable": analysis.solvable,
            "usable_instruments": [dict(item) for item in analysis.usable_instruments],
            "unusable_instruments": [
                dict(item) for item in analysis.unusable_instruments
            ],
        }

    def find_instrument(self, instrument_id: InstrumentId) -> Optional[engine.Instrument]:
        return next(
            (inst for inst in self.instruments if inst.id == instrument_id), None
        )

    def step_profile(self, step: int) -> engine.StepProfile:
        return self.analysis.profiles[step]


def _new_plan_code() -> str:
    return uuid.uuid4().hex[:12]


class PlanService:
    """进程内方案存储。多副本部署时应替换为共享存储。

    全局锁只保护字典读写与快照替换（都是 O(1) 的短操作）；树裁剪与按步长
    分档在锁外完成，不会阻塞其他批次。
    """

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

        # 编译在锁外完成：同一进程的其他批次可以继续查询与提交精测。
        analysis = engine.build_analysis(coarse, tree, instruments)
        plan = Plan(
            code=_new_plan_code(),
            coarse=coarse,
            tree=tree,
            tree_raw=tree_raw,
            instruments=tuple(instruments),
            analysis=analysis,
        )
        with self._lock:
            self._plans[plan.code] = plan
        return plan

    # -- 查询 ---------------------------------------------------------------

    def get_plan(self, code: str) -> Plan:
        """返回不可变快照；一次查询内状态/评估/结论/历史对应同一时点。"""

        with self._lock:
            plan = self._plans.get(code)
        if plan is None:
            raise ServiceError(404, "unknown_plan", "方案号不存在")
        return plan

    # -- 更正粗测 -----------------------------------------------------------

    def correct_coarse(self, code: str, coarse: engine.CoarseRange) -> Plan:
        snapshot = self.get_plan(code)
        self._require_active(snapshot, action="更正粗测")

        error = coarse.error()
        if error is not None:
            # 更正无解：保留现状，当前方案号继续有效。
            raise ServiceError(422, "range_infeasible", error)

        # 新范围独立裁剪判定树、独立按步长分档，锁外完成。
        analysis = engine.build_analysis(
            coarse, snapshot.tree, list(snapshot.instruments)
        )
        new_code = _new_plan_code()
        correction_event = {
            "event": "coarse_corrected",
            "from_plan_code": snapshot.code,
            "to_plan_code": new_code,
            "previous_coarse": snapshot.coarse.as_dict(),
            "new_coarse": coarse.as_dict(),
        }
        new_plan = Plan(
            code=new_code,
            coarse=coarse,
            tree=snapshot.tree,
            tree_raw=snapshot.tree_raw,
            instruments=snapshot.instruments,
            analysis=analysis,
            generation=snapshot.generation + 1,
            history=(*snapshot.history, correction_event),
        )
        superseded_plan = replace(
            snapshot,
            status="superseded",
            superseded_by=new_code,
            revision=snapshot.revision + 1,
            history=(
                *snapshot.history,
                {
                    "event": "superseded",
                    "by_plan_code": new_code,
                    "note": "仅供查询，保留原评估",
                },
            ),
        )

        with self._lock:
            current = self._plans.get(code)
            if current is not snapshot:
                # 期间已有更正或精测成功：本次更正不得生效。
                raise _concurrent_conflict(current)
            self._plans[code] = superseded_plan
            self._plans[new_code] = new_plan
        return new_plan

    # -- 精测 ---------------------------------------------------------------

    def submit_precision(
        self, code: str, instrument_id: InstrumentId, reading: int
    ) -> dict[str, Any]:
        snapshot = self.get_plan(code)
        self._require_active(snapshot, action="提交精测")

        instrument = snapshot.find_instrument(instrument_id)
        if instrument is None:
            raise ServiceError(404, "instrument_not_in_plan", "该仪表不在本方案号中")

        # 合用性直接读编译结果；档值判定复用同一份按步长分档结果。
        if instrument_id not in snapshot.analysis.usable_ids:
            # 精测被拒：保留现状。
            raise ServiceError(
                409,
                "instrument_not_usable",
                "该仪表在本方案下无法保证同档同判，精测被拒",
            )

        verdict = snapshot.step_profile(instrument.step).verdict_for(reading)
        if verdict is None:
            raise ServiceError(
                409,
                "reading_not_feasible",
                "该档值在本方案下不可行（越界、不是步长整倍数，或判定不唯一）",
            )

        reading_class = engine.reading_class(snapshot.coarse, instrument, reading)
        result = {
            "verdict": verdict,
            "instrument_id": instrument_id,
            "step": instrument.step,
            "reading": reading,
            "outer_reading_class": list(reading_class),
            "note": "外层实测档值唯一确定判定，本轮结束",
        }
        completed = replace(
            snapshot,
            status="completed",
            revision=snapshot.revision + 1,
            result=result,
            history=(
                *snapshot.history,
                {
                    "event": "precision_submitted",
                    "instrument_id": instrument_id,
                    "reading": reading,
                    "verdict": verdict,
                },
            ),
        )

        with self._lock:
            current = self._plans.get(code)
            if current is not snapshot:
                # 期间已有更正或另一项精测成功：本项不得生效。
                raise _concurrent_conflict(current)
            self._plans[code] = completed

        return {
            "plan_code": completed.code,
            "status": completed.status,
            "revision": completed.revision,
            "round_ended": True,
            **result,
        }

    # -- 内部 ---------------------------------------------------------------

    @staticmethod
    def _require_active(plan: Plan, *, action: str) -> None:
        if plan.status == "superseded":
            raise ServiceError(
                409,
                "plan_superseded",
                "该方案号已被作废，请使用现行方案号",
                current_plan_code=plan.superseded_by,
            )
        if plan.status == "completed":
            raise ServiceError(409, "plan_completed", "本轮已结束，不能再" + action)

    # -- 测试辅助 -----------------------------------------------------------

    def reset(self) -> None:
        with self._lock:
            self._plans.clear()


def _concurrent_conflict(current: Optional[Plan]) -> ServiceError:
    if current is None:  # pragma: no cover - 方案不会被删除
        return ServiceError(409, "concurrent_update", "方案状态已变化，请重试")
    if current.status == "superseded":
        return ServiceError(
            409,
            "plan_superseded",
            "该方案号已被作废，请使用现行方案号",
            current_plan_code=current.superseded_by,
        )
    if current.status == "completed":
        return ServiceError(409, "plan_completed", "本轮已结束，不能重复提交")
    return ServiceError(409, "concurrent_update", "方案状态已变化，请重试")


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
