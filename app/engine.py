"""合格判定树、粗测范围与测厚仪适配性的纯计算引擎。

核心思想
--------
判定树只在单层厚度上做 ``<= 阈值`` 比较，因此树的每个叶节点都对应一个
轴对齐的整数矩形。把矩形与粗测范围（外层区间 × 内层区间）以及总厚度约束
（``total_min <= e + i <= total_max``）求交，就得到该叶节点真正可能覆盖的点集。

测厚仪只把外层厚度向下取整到 ``step`` 的整倍数，所以"档值"就是外层步长桶
``[d*step, d*step + step - 1]``。对每个叶矩形，可以解析地算出它能覆盖哪些
档值区间（与总厚度约束的可行性有关；这些区间必定连续）。把这些区间按判定
结果求并：

* 若合格与返工的档值区间不相交 -> 每个可能档值只对应一种判定，仪表合用；
* 若相交 -> 该档值下存在两种判定，仪表不合用，并可在交集中取一个档值，
  到两个叶矩形里各取一个具体厚度组合作为"同档异判"的见证。

全程只做整数区间运算，计算量与粗测范围的大小无关，绝不逐个枚举厚度。
复杂度为 O(叶节点数 × 仪表数)，与区间宽度、步长大小均无关。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Optional, Union

Verdict = Literal["pass", "rework"]
Layer = Literal["outer", "inner"]


class TreeError(ValueError):
    """判定树结构不合法。"""


# ---------------------------------------------------------------------------
# 判定树
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Leaf:
    verdict: Verdict


@dataclass(frozen=True)
class Branch:
    layer: Layer
    threshold: int
    le: "Node"  # 厚度 <= 阈值
    gt: "Node"  # 厚度 >  阈值


Node = Union[Leaf, Branch]


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def parse_tree(raw: object) -> Node:
    """校验并解析 JSON 判定树。

    节点格式：
    * 叶节点 ``{"verdict": "pass" | "rework"}``；
    * 分支节点 ``{"layer": "outer" | "inner", "threshold": <int>,
      "le": <节点>, "gt": <节点>}``；
    * 不允许多余字段；叶节点总数 <= 12。

    校验用显式栈完成，避免恶意深层 JSON 触发 Python 递归上限；
    深度超过 11 时叶节点数必然超过 12，直接拒绝。
    """

    if not isinstance(raw, dict):
        raise TreeError("树根必须是 JSON 对象")

    stack: list[tuple[object, int]] = [(raw, 0)]
    leaves = 0
    while stack:
        obj, depth = stack.pop()
        if depth > 11:
            raise TreeError("判定树过深：叶节点数必然超过 12")
        if not isinstance(obj, dict):
            raise TreeError("每个树节点必须是 JSON 对象")
        keys = set(obj)
        if "verdict" in keys:
            if keys != {"verdict"}:
                extra = sorted(map(str, keys - {"verdict"}))
                raise TreeError(f"叶节点存在多余字段: {extra}")
            verdict = obj["verdict"]
            if not isinstance(verdict, str) or verdict not in ("pass", "rework"):
                raise TreeError("叶节点 verdict 只能是 'pass' 或 'rework'")
            leaves += 1
            if leaves > 12:
                raise TreeError("判定树最多 12 个叶节点，实际超过 12 个")
            continue

        expected = {"layer", "threshold", "le", "gt"}
        if keys != expected:
            missing = sorted(map(str, expected - keys))
            extra = sorted(map(str, keys - expected))
            raise TreeError(f"分支节点字段不匹配：缺少 {missing}，多余 {extra}")
        if obj["layer"] not in ("outer", "inner"):
            raise TreeError("分支 layer 只能是 'outer' 或 'inner'")
        if not _is_int(obj["threshold"]):
            raise TreeError("分支 threshold 必须是整数")
        stack.append((obj["le"], depth + 1))
        stack.append((obj["gt"], depth + 1))

    def build(obj: dict[str, Any]) -> Node:
        if "verdict" in obj:
            return Leaf(verdict=obj["verdict"])
        return Branch(
            layer=obj["layer"],
            threshold=obj["threshold"],
            le=build(obj["le"]),
            gt=build(obj["gt"]),
        )

    return build(raw)


def verdict_at(root: Node, outer: int, inner: int) -> Verdict:
    """给出一个具体厚度组合的判定（用于校验与测试）。"""

    node = root
    while isinstance(node, Branch):
        value = outer if node.layer == "outer" else inner
        node = node.le if value <= node.threshold else node.gt
    return node.verdict


# ---------------------------------------------------------------------------
# 粗测范围与矩形
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CoarseRange:
    """两层厚度与总厚度的整数闭区间。"""

    outer_min: int
    outer_max: int
    inner_min: int
    inner_max: int
    total_min: int
    total_max: int

    def as_dict(self) -> dict[str, list[int]]:
        return {
            "outer": [self.outer_min, self.outer_max],
            "inner": [self.inner_min, self.inner_max],
            "total": [self.total_min, self.total_max],
        }

    def error(self) -> Optional[str]:
        """范围不合法或无解时返回原因，否则返回 None。"""

        values = {
            "outer_min": self.outer_min,
            "outer_max": self.outer_max,
            "inner_min": self.inner_min,
            "inner_max": self.inner_max,
            "total_min": self.total_min,
            "total_max": self.total_max,
        }
        for name, value in values.items():
            if not _is_int(value):
                return f"{name} 必须是整数"
            if value < 0:
                return f"{name} 必须是非负整数"
        if self.outer_min > self.outer_max:
            return "外层厚度区间的下界大于上界"
        if self.inner_min > self.inner_max:
            return "内层厚度区间的下界大于上界"
        if self.total_min > self.total_max:
            return "总厚度区间的下界大于上界"

        # 两层厚度之和恰好覆盖整数区间
        # [outer_min + inner_min, outer_max + inner_max]
        lowest = max(self.total_min, self.outer_min + self.inner_min)
        highest = min(self.total_max, self.outer_max + self.inner_max)
        if lowest > highest:
            return "范围无解：两层厚度之和无法落入总厚度区间"
        return None


@dataclass(frozen=True)
class Rect:
    lo_e: int
    hi_e: int
    lo_i: int
    hi_i: int

    def empty(self) -> bool:
        return self.lo_e > self.hi_e or self.lo_i > self.hi_i


@dataclass(frozen=True)
class Cell:
    """一个叶节点在粗测范围内真正覆盖的矩形。"""

    rect: Rect
    verdict: Verdict


def _ceil_div(a: int, b: int) -> int:
    return -((-a) // b)


def feasible_point(rect: Rect, coarse: CoarseRange) -> Optional[tuple[int, int]]:
    """在矩形与总厚度约束的交集中取一个具体点；不存在返回 None。"""

    if rect.empty():
        return None
    lo_e = max(rect.lo_e, coarse.total_min - rect.hi_i)
    hi_e = min(rect.hi_e, coarse.total_max - rect.lo_i)
    if lo_e > hi_e:
        return None
    e = lo_e
    lo_i = max(rect.lo_i, coarse.total_min - e)
    hi_i = min(rect.hi_i, coarse.total_max - e)
    if lo_i > hi_i:
        return None
    return e, lo_i


def collect_cells(root: Node, coarse: CoarseRange) -> list[Cell]:
    """把判定树裁剪到粗测范围内，返回所有可行叶矩形。"""

    base = Rect(coarse.outer_min, coarse.outer_max, coarse.inner_min, coarse.inner_max)
    cells: list[Cell] = []

    def walk(node: Node, rect: Rect) -> None:
        if rect.empty():
            return
        if isinstance(node, Leaf):
            if feasible_point(rect, coarse) is not None:
                cells.append(Cell(rect=rect, verdict=node.verdict))
            return
        threshold = node.threshold
        if node.layer == "outer":
            le_rect = Rect(rect.lo_e, min(rect.hi_e, threshold), rect.lo_i, rect.hi_i)
            gt_rect = Rect(max(rect.lo_e, threshold + 1), rect.hi_e, rect.lo_i, rect.hi_i)
        else:
            le_rect = Rect(rect.lo_e, rect.hi_e, rect.lo_i, min(rect.hi_i, threshold))
            gt_rect = Rect(rect.lo_e, rect.hi_e, max(rect.lo_i, threshold + 1), rect.hi_i)
        walk(node.le, le_rect)
        walk(node.gt, gt_rect)

    walk(root, base)
    return cells


# ---------------------------------------------------------------------------
# 档值（读数）区间运算
# ---------------------------------------------------------------------------


def _bucket_feasible(
    rect: Rect, step: int, d: int, coarse: CoarseRange
) -> bool:
    """档 d 的整段外层读数类 [d*step, d*step+step-1] 与矩形、总厚度约束
    是否有交。"""

    lo_e = max(rect.lo_e, d * step)
    hi_e = min(rect.hi_e, d * step + step - 1)
    if lo_e > hi_e:
        return False
    # 固定 d 时 e + i 连续覆盖 [lo_e + lo_i, hi_e + hi_i]
    return (
        lo_e + rect.lo_i <= coarse.total_max
        and hi_e + rect.hi_i >= coarse.total_min
    )


def cell_reading_span(
    cell: Cell, step: int, coarse: CoarseRange
) -> Optional[tuple[int, int]]:
    """叶矩形覆盖的档值编号闭区间 [d_lo, d_hi]（读数 = d * step）。

    档 d 内的可行点为 ``e in [d*step, d*step+step-1]``，内层 ``i`` 在其区间内。
    对固定 d，可行的 ``e + i`` 连续覆盖 ``[min_e + lo_i, max_e + hi_i]``，
    与总厚度区间相交即可行。``min_e`` / ``max_e`` 随 d 单调不减，因此可行
    档值必是一个连续区间。

    对严格位于两端的档（d_lo < d < d_hi），桶完全落在矩形内，
    ``min_e = d*step``、``max_e = d*step + step - 1``，可直接解析求出
    ``[ceil((total_min - hi_i - step + 1)/step),
      floor((total_max - lo_i)/step)]``。两端档会被矩形边界裁剪，另做一次
    精确校验即可（至多调整一个端点）。全程不做厚度枚举。

    调用前提：cell 已通过 ``feasible_point`` 检验（矩形与总厚度区间有交）。
    """

    rect = cell.rect
    d_lo = rect.lo_e // step
    d_hi = rect.hi_e // step

    lower = max(
        d_lo,
        _ceil_div(coarse.total_min - rect.hi_i - (step - 1), step),
    )
    upper = min(d_hi, (coarse.total_max - rect.lo_i) // step)
    if lower > upper:
        return None
    # 两端档会被矩形边界裁剪，理论上解析下界/上界已保证可行；
    # 这里仍做一次有界校正（至多 2 步）作为防御，绝不随量程大小循环。
    for _ in range(3):
        if _bucket_feasible(rect, step, lower, coarse):
            break
        lower += 1
    else:
        return None
    for _ in range(3):
        if _bucket_feasible(rect, step, upper, coarse):
            break
        upper -= 1
    else:
        return None
    if lower > upper:
        return None
    return lower, upper


def _merge(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged: list[tuple[int, int]] = [ordered[0]]
    for lo, hi in ordered[1:]:
        prev_lo, prev_hi = merged[-1]
        if lo <= prev_hi + 1:
            merged[-1] = (prev_lo, max(prev_hi, hi))
        else:
            merged.append((lo, hi))
    return merged


def _intersect(
    left: list[tuple[int, int]], right: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    i = j = 0
    while i < len(left) and j < len(right):
        a, b = left[i]
        c, d = right[j]
        lo, hi = max(a, c), min(b, d)
        if lo <= hi:
            out.append((lo, hi))
        if b < d:
            i += 1
        else:
            j += 1
    return out


def _count(intervals: list[tuple[int, int]]) -> int:
    return sum(hi - lo + 1 for lo, hi in intervals)


def _witness_in_bucket(
    cell: Cell, step: int, reading_index: int, coarse: CoarseRange
) -> Optional[tuple[int, int]]:
    """在叶矩形、指定档值桶与总厚度约束的交集中取一个点。"""

    rect = cell.rect
    lo_e = max(rect.lo_e, reading_index * step)
    hi_e = min(rect.hi_e, reading_index * step + step - 1)
    if lo_e > hi_e:
        return None
    lo_e = max(lo_e, coarse.total_min - rect.hi_i)
    hi_e = min(hi_e, coarse.total_max - rect.lo_i)
    if lo_e > hi_e:
        return None
    e = lo_e
    lo_i = max(rect.lo_i, coarse.total_min - e)
    hi_i = min(rect.hi_i, coarse.total_max - e)
    if lo_i > hi_i:
        return None
    return e, lo_i


# ---------------------------------------------------------------------------
# 仪表适配性与整体评估
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Instrument:
    id: Union[int, str]
    step: int


def _check_instrument(instrument: Instrument) -> None:
    if not _is_int(instrument.step) or instrument.step <= 0:
        raise ValueError("仪表步长必须是正整数")
    if not isinstance(instrument.id, (int, str)) or isinstance(instrument.id, bool):
        raise ValueError("仪表编号必须是整数或字符串")


@dataclass(frozen=True)
class StepProfile:
    """某个步长下的分档结果，与具体仪表编号无关。

    同一方案号内相同步长的所有仪表共用一份：档值区间、同档异判的交集和
    见证都只算一次。查询方案、查询档值和提交精测都直接复用，不再逐台重算。
    """

    step: int
    pass_intervals: tuple[tuple[int, int], ...]
    rework_intervals: tuple[tuple[int, int], ...]
    conflicts: tuple[tuple[int, int], ...]
    conflict_template: Optional[dict[str, Any]]
    possible_reading_count: int

    def verdict_for(self, reading: int) -> Optional[Verdict]:
        """实测档值唯一对应的判定；不可行或同档两判时返回 None。"""

        if (
            not _is_int(reading)
            or reading < 0
            or reading % self.step != 0
        ):
            return None
        index = reading // self.step
        found: set[Verdict] = set()
        if any(lo <= index <= hi for lo, hi in self.pass_intervals):
            found.add("pass")
        if any(lo <= index <= hi for lo, hi in self.rework_intervals):
            found.add("rework")
        return found.pop() if len(found) == 1 else None

    def intervals_payload(self) -> list[dict[str, Any]]:
        intervals = [
            {
                "verdict": verdict,
                "from_reading": lo * self.step,
                "to_reading": hi * self.step,
            }
            for verdict, spans in (
                ("pass", self.pass_intervals),
                ("rework", self.rework_intervals),
            )
            for lo, hi in spans
        ]
        intervals.sort(key=lambda item: item["from_reading"])
        return intervals


def build_step_profile(
    cells: list[Cell], coarse: CoarseRange, step: int
) -> StepProfile:
    """把一个步长的分档只计算一次，得到可复用的 :class:`StepProfile`。"""

    if not _is_int(step) or step <= 0:
        raise ValueError("仪表步长必须是正整数")

    by_verdict: dict[str, list[tuple[int, int]]] = {"pass": [], "rework": []}
    cell_spans: list[tuple[Cell, tuple[int, int]]] = []
    for cell in cells:
        span = cell_reading_span(cell, step, coarse)
        if span is None:
            continue
        by_verdict[cell.verdict].append(span)
        cell_spans.append((cell, span))

    pass_intervals = _merge(by_verdict["pass"])
    rework_intervals = _merge(by_verdict["rework"])
    conflicts = _intersect(pass_intervals, rework_intervals)

    conflict_template: Optional[dict[str, Any]] = None
    if conflicts:
        reading_index = conflicts[0][0]
        pass_cell = next(
            cell
            for cell, (lo, hi) in cell_spans
            if cell.verdict == "pass" and lo <= reading_index <= hi
        )
        rework_cell = next(
            cell
            for cell, (lo, hi) in cell_spans
            if cell.verdict == "rework" and lo <= reading_index <= hi
        )
        samples: list[dict[str, Any]] = []
        for cell in (pass_cell, rework_cell):
            point = _witness_in_bucket(cell, step, reading_index, coarse)
            if point is None:  # pragma: no cover - 区间公式的防御性断言
                raise AssertionError("档值区间可行但取不到见证点")
            outer, inner = point
            reading = reading_index * step
            if outer // step != reading_index:
                raise AssertionError("见证点未落在目标档值")
            if not (coarse.outer_min <= outer <= coarse.outer_max):
                raise AssertionError("见证点外层厚度越界")
            if not (coarse.inner_min <= inner <= coarse.inner_max):
                raise AssertionError("见证点内层厚度越界")
            if not (coarse.total_min <= outer + inner <= coarse.total_max):
                raise AssertionError("见证点总厚度越界")
            samples.append(
                {
                    "outer": outer,
                    "inner": inner,
                    "total": outer + inner,
                    "reading": reading,
                    "verdict": cell.verdict,
                }
            )
        if samples[0]["verdict"] == samples[1]["verdict"]:  # pragma: no cover
            raise AssertionError("同档见证点判定相同")
        reading = reading_index * step
        conflict_template = {
            "reading": reading,
            "reading_class": [reading, reading + step - 1],
            "conflict_reading_count": _count(conflicts),
            "samples": samples,
        }

    return StepProfile(
        step=step,
        pass_intervals=tuple(pass_intervals),
        rework_intervals=tuple(rework_intervals),
        conflicts=tuple(conflicts),
        conflict_template=conflict_template,
        possible_reading_count=_count(pass_intervals) + _count(rework_intervals),
    )


def _copy_conflict(template: dict[str, Any]) -> dict[str, Any]:
    return {
        "reading": template["reading"],
        "reading_class": list(template["reading_class"]),
        "conflict_reading_count": template["conflict_reading_count"],
        "samples": [dict(sample) for sample in template["samples"]],
    }


def report_from_profile(
    instrument: Instrument, profile: StepProfile
) -> dict[str, Any]:
    """用共享的分档结果生成某台仪表自己的报告（保留自己的编号）。"""

    _check_instrument(instrument)
    if instrument.step != profile.step:  # pragma: no cover - 调用方保证一致
        raise ValueError("仪表步长与分档结果不一致")

    if profile.conflicts:
        if profile.conflict_template is None:  # pragma: no cover
            raise AssertionError("存在冲突档值却没有见证")
        return {
            "instrument_id": instrument.id,
            "step": instrument.step,
            "usable": False,
            "conflict": _copy_conflict(profile.conflict_template),
        }
    return {
        "instrument_id": instrument.id,
        "step": instrument.step,
        "usable": True,
        "reading_intervals": profile.intervals_payload(),
        "possible_reading_count": profile.possible_reading_count,
    }


def classify_instrument(
    instrument: Instrument, cells: list[Cell], coarse: CoarseRange
) -> dict[str, Any]:
    """判定单台仪表，返回可直接序列化的报告。"""

    _check_instrument(instrument)
    return report_from_profile(
        instrument, build_step_profile(cells, coarse, instrument.step)
    )


@dataclass(frozen=True)
class CompiledAnalysis:
    """一个方案号评估结果的一次性编译产物。

    判定树按粗测范围裁剪一次，每种步长只分档一次；各仪表的报告也在这里
    生成并缓存。后续查询方案、查询档值和提交精测都只读这份结果。
    """

    range_feasible: bool
    range_error: Optional[str]
    solvable: bool
    cells: tuple[Cell, ...]
    profiles: dict[int, StepProfile]
    instrument_reports: tuple[dict[str, Any], ...]
    usable_instruments: tuple[dict[str, Any], ...]
    unusable_instruments: tuple[dict[str, Any], ...]
    usable_ids: frozenset[Any]
    distinct_steps: int


def build_analysis(
    coarse: CoarseRange, root: Node, instruments: list[Instrument]
) -> CompiledAnalysis:
    """一次性完成树裁剪与按步长分档，供方案全生命周期复用。"""

    range_error = coarse.error()
    if range_error is not None:
        return CompiledAnalysis(
            range_feasible=False,
            range_error=range_error,
            solvable=False,
            cells=(),
            profiles={},
            instrument_reports=(),
            usable_instruments=(),
            unusable_instruments=(),
            usable_ids=frozenset(),
            distinct_steps=0,
        )

    cells = tuple(collect_cells(root, coarse))
    profiles: dict[int, StepProfile] = {}
    reports: list[dict[str, Any]] = []
    usable: list[dict[str, Any]] = []
    unusable: list[dict[str, Any]] = []
    for instrument in instruments:
        _check_instrument(instrument)
        profile = profiles.get(instrument.step)
        if profile is None:
            profile = build_step_profile(list(cells), coarse, instrument.step)
            profiles[instrument.step] = profile
        report = report_from_profile(instrument, profile)
        reports.append(report)
        (usable if report["usable"] else unusable).append(report)

    return CompiledAnalysis(
        range_feasible=True,
        range_error=None,
        solvable=bool(usable),
        cells=cells,
        profiles=profiles,
        instrument_reports=tuple(reports),
        usable_instruments=tuple(usable),
        unusable_instruments=tuple(unusable),
        usable_ids=frozenset(report["instrument_id"] for report in usable),
        distinct_steps=len(profiles),
    )


def assess(
    coarse: CoarseRange, root: Node, instruments: list[Instrument]
) -> dict[str, Any]:
    """评估整份方案：范围是否有解、各仪表是否合用、不合用见证。"""

    compiled = build_analysis(coarse, root, instruments)
    return {
        "range_feasible": compiled.range_feasible,
        "range_error": compiled.range_error,
        # 方案可执行 = 范围有解且至少有一台仪表能保证同档同判。
        "solvable": compiled.solvable,
        "usable_instruments": list(compiled.usable_instruments),
        "unusable_instruments": list(compiled.unusable_instruments),
    }


def reading_verdict(
    coarse: CoarseRange,
    root: Node,
    instrument: Instrument,
    reading: int,
) -> Optional[Verdict]:
    """给定某台仪表的实测档值，返回唯一判定；档值不可行/不唯一返回 None。

    档值必须已是步长的整倍数（仪表读数天然如此）。这里按步长重新分档一次，
    语义与方案缓存一致；服务层提交精测时直接复用缓存的 StepProfile。
    """

    _check_instrument(instrument)
    profile = build_step_profile(
        collect_cells(root, coarse), coarse, instrument.step
    )
    return profile.verdict_for(reading)


def reading_class(
    coarse: CoarseRange, instrument: Instrument, reading: int
) -> tuple[int, int]:
    """该档值对应的外层厚度类，裁剪到粗测外层范围内。

    仅用于回执展示：例如量程外层从 42 起、步长 10，档值 40 实际对应
    外层 [42, 49] 而不是 [40, 49]。
    """

    _check_instrument(instrument)
    step = instrument.step
    lo = max(reading, coarse.outer_min)
    hi = min(reading + step - 1, coarse.outer_max)
    return lo, hi
