"""区间引擎与 HTTP API 的测试。"""

from __future__ import annotations

import random

import pytest
from fastapi.testclient import TestClient

from app import engine
from app.main import app, service


@pytest.fixture(autouse=True)
def _clean_state():
    service.reset()
    yield
    service.reset()


@pytest.fixture()
def client():
    return TestClient(app)


# ---------------------------------------------------------------------------
# 树解析
# ---------------------------------------------------------------------------


def test_parse_simple_tree_and_verdict():
    root = engine.parse_tree(
        {
            "layer": "outer",
            "threshold": 50,
            "le": {"verdict": "pass"},
            "gt": {"verdict": "rework"},
        }
    )
    assert engine.verdict_at(root, 50, 0) == "pass"
    assert engine.verdict_at(root, 51, 0) == "rework"


@pytest.mark.parametrize(
    "bad",
    [
        {"verdict": "maybe"},
        {"verdict": "pass", "extra": 1},
        {"layer": "side", "threshold": 1, "le": {"verdict": "pass"}, "gt": {"verdict": "pass"}},
        {"layer": "outer", "threshold": 1.5, "le": {"verdict": "pass"}, "gt": {"verdict": "pass"}},
        {"layer": "outer", "threshold": 1, "le": {"verdict": "pass"}},
        [],
        "tree",
    ],
)
def test_parse_rejects_bad_trees(bad):
    with pytest.raises(engine.TreeError):
        engine.parse_tree(bad)


def _chain(leaves: int, verdicts=("pass", "rework")):
    """构造恰好 leaves 个叶的退化链（深度 leaves-1）。"""

    node = {"verdict": verdicts[0]}
    for idx in range(1, leaves):
        node = {
            "layer": "outer",
            "threshold": 1000 + idx,
            "le": {"verdict": verdicts[idx % len(verdicts)]},
            "gt": node,
        }
    return node


def test_leaf_limit_boundary():
    assert engine.parse_tree(_chain(12)) is not None
    with pytest.raises(engine.TreeError):
        engine.parse_tree(_chain(13))


def test_extremely_deep_tree_rejected_without_recursion_error():
    node = {"verdict": "pass"}
    for _ in range(5000):
        node = {
            "layer": "outer",
            "threshold": 1,
            "le": {"verdict": "pass"},
            "gt": node,
        }
    with pytest.raises(engine.TreeError):
        engine.parse_tree(node)


# ---------------------------------------------------------------------------
# 粗测范围
# ---------------------------------------------------------------------------


def test_range_validation():
    assert engine.CoarseRange(10, 5, 0, 1, 0, 10).error() is not None
    assert engine.CoarseRange(-1, 5, 0, 1, 0, 10).error() is not None
    # 总厚度区间与两层之和不相交
    assert engine.CoarseRange(0, 10, 0, 10, 50, 60).error() is not None
    assert engine.CoarseRange(0, 10, 0, 10, 5, 15).error() is None


# ---------------------------------------------------------------------------
# 与暴力枚举对照（小范围，仅用于验证区间算法的正确性）
# ---------------------------------------------------------------------------


def _brute(root, coarse, step):
    buckets = {}
    for outer in range(coarse.outer_min, coarse.outer_max + 1):
        for inner in range(coarse.inner_min, coarse.inner_max + 1):
            if coarse.total_min <= outer + inner <= coarse.total_max:
                reading = outer // step * step
                buckets.setdefault(reading, set()).add(
                    engine.verdict_at(root, outer, inner)
                )
    return buckets


def _random_tree(rng, depth):
    if depth == 0 or rng.random() < 0.4:
        return {"verdict": rng.choice(["pass", "rework"])}
    return {
        "layer": rng.choice(["outer", "inner"]),
        "threshold": rng.randint(0, 12),
        "le": _random_tree(rng, depth - 1),
        "gt": _random_tree(rng, depth - 1),
    }


def test_engine_matches_brute_force():
    rng = random.Random(20240601)
    checked = 0
    for _ in range(250):
        raw = _random_tree(rng, 3)
        try:
            root = engine.parse_tree(raw)
        except engine.TreeError:
            continue
        e0 = rng.randint(0, 6)
        e1 = rng.randint(e0, 9)
        i0 = rng.randint(0, 6)
        i1 = rng.randint(i0, 9)
        t0 = rng.randint(0, e1 + i1)
        t1 = rng.randint(t0, e1 + i1)
        coarse = engine.CoarseRange(e0, e1, i0, i1, t0, t1)
        if coarse.error() is not None:
            continue
        cells = engine.collect_cells(root, coarse)
        for step in range(1, 8):
            instrument = engine.Instrument("m", step)
            report = engine.classify_instrument(instrument, cells, coarse)
            buckets = _brute(root, coarse, step)
            conflicting = {d for d, vs in buckets.items() if len(vs) > 1}
            assert report["usable"] == (not conflicting)
            if conflicting:
                reading = report["conflict"]["reading"]
                assert reading in conflicting
                verdicts = set()
                for sample in report["conflict"]["samples"]:
                    e, i, t = sample["outer"], sample["inner"], sample["total"]
                    assert t == e + i
                    assert e // step * step == reading
                    assert coarse.outer_min <= e <= coarse.outer_max
                    assert coarse.inner_min <= i <= coarse.inner_max
                    assert coarse.total_min <= t <= coarse.total_max
                    assert sample["verdict"] == engine.verdict_at(root, e, i)
                    verdicts.add(sample["verdict"])
                assert verdicts == {"pass", "rework"}
            else:
                assert report["possible_reading_count"] == len(buckets)
                for interval in report["reading_intervals"]:
                    d = interval["from_reading"]
                    while d <= interval["to_reading"]:
                        assert buckets[d] == {interval["verdict"]}
                        assert (
                            engine.reading_verdict(coarse, root, instrument, d)
                            == interval["verdict"]
                        )
                        d += step
            checked += 1
    assert checked > 500


def test_no_enumeration_on_huge_range():
    """量程达到 10^18 也必须瞬间完成——证明实现没有逐个枚举厚度。"""

    root = engine.parse_tree(
        {
            "layer": "outer",
            "threshold": 10**15 - 1,
            "le": {"verdict": "pass"},
            "gt": {"verdict": "rework"},
        }
    )
    coarse = engine.CoarseRange(0, 10**18, 0, 10**18, 0, 2 * 10**18)
    report = engine.assess(
        coarse, root, [engine.Instrument(idx, 10**idx) for idx in range(1, 13)]
    )
    assert report["range_feasible"]
    # 阈值 10^15-1 恰好是每个步长的桶边界，判定不会被步长桶切断 -> 全部合用
    assert len(report["usable_instruments"]) == 12
    assert not report["unusable_instruments"]


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------


def _plan_payload(step=5):
    return {
        "coarse": {"outer": [40, 60], "inner": [0, 10], "total": [40, 70]},
        "tree": {
            "layer": "outer",
            "threshold": 50,
            "le": {"verdict": "pass"},
            "gt": {"verdict": "rework"},
        },
        "instruments": [
            {"id": 1, "step": 1},
            {"id": 2, "step": 5},
            {"id": 3, "step": 10},
        ],
    }


def test_create_plan_lists_usable_and_conflicts(client):
    response = client.post("/plans", json=_plan_payload())
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "active"
    usable = {item["instrument_id"] for item in body["assessment"]["usable_instruments"]}
    unusable = {
        item["instrument_id"]: item
        for item in body["assessment"]["unusable_instruments"]
    }
    assert usable == {1}
    assert set(unusable) == {2, 3}
    conflict = unusable[2]["conflict"]
    assert conflict["reading"] == 50
    verdicts = {sample["verdict"] for sample in conflict["samples"]}
    assert verdicts == {"pass", "rework"}
    for sample in conflict["samples"]:
        assert sample["outer"] // unusable[2]["step"] * unusable[2]["step"] == 50


def test_precision_flow_end_to_end(client):
    plan = client.post("/plans", json=_plan_payload()).json()
    code = plan["plan_code"]

    rejected = client.post(f"/plans/{code}/precision", json={"instrument_id": 2, "reading": 50})
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "instrument_not_usable"

    bad_reading = client.post(f"/plans/{code}/precision", json={"instrument_id": 1, "reading": 39})
    assert bad_reading.status_code == 409
    assert bad_reading.json()["error"]["code"] == "reading_not_feasible"

    not_multiple = client.post(f"/plans/{code}/precision", json={"instrument_id": 1, "reading": 41.0})
    assert not_multiple.status_code == 422

    ok = client.post(f"/plans/{code}/precision", json={"instrument_id": 1, "reading": 40})
    assert ok.status_code == 200
    body = ok.json()
    assert body["verdict"] == "pass"
    assert body["round_ended"] is True
    assert body["outer_reading_class"] == [40, 40]

    again = client.post(f"/plans/{code}/precision", json={"instrument_id": 1, "reading": 55})
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "plan_completed"


def test_rework_verdict(client):
    plan = client.post("/plans", json=_plan_payload()).json()
    code = plan["plan_code"]
    ok = client.post(f"/plans/{code}/precision", json={"instrument_id": 1, "reading": 55})
    assert ok.status_code == 200
    assert ok.json()["verdict"] == "rework"


def test_correction_switches_plan_code_and_invalidates_old(client):
    plan = client.post("/plans", json=_plan_payload()).json()
    old = plan["plan_code"]
    corrected = client.post(
        f"/plans/{old}/coarse",
        json={"outer": [40, 60], "inner": [0, 10], "total": [40, 70]},
    )
    assert corrected.status_code == 200
    new = corrected.json()["plan_code"]
    assert new != old
    assert corrected.json()["generation"] == 2

    old_plan = client.get(f"/plans/{old}").json()
    assert old_plan["status"] == "superseded"
    assert old_plan["superseded_by"] == new

    stale = client.post(f"/plans/{old}/precision", json={"instrument_id": 1, "reading": 40})
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "plan_superseded"
    assert stale.json()["error"]["current_plan_code"] == new

    stale_correction = client.post(
        f"/plans/{old}/coarse",
        json={"outer": [40, 60], "inner": [0, 10], "total": [40, 70]},
    )
    assert stale_correction.status_code == 409


def test_correction_to_feasible_range_without_usable_instrument(client):
    """更正只要求范围有解；是否有合用仪表是另一回事，仍可换号。"""

    payload = _plan_payload()
    payload["instruments"] = [{"id": 1, "step": 5}, {"id": 2, "step": 10}]
    plan = client.post("/plans", json=payload).json()
    code = plan["plan_code"]
    assert plan["assessment"]["usable_instruments"] == []

    response = client.post(
        f"/plans/{code}/coarse",
        json={"outer": [48, 52], "inner": [0, 0], "total": [48, 52]},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["plan_code"] != code
    assert body["assessment"]["solvable"] is False
    assert body["assessment"]["range_feasible"] is True

    old = client.get(f"/plans/{code}").json()
    assert old["status"] == "superseded"


def test_reading_class_clipped_to_coarse_range(client):
    plan = client.post("/plans", json=_plan_payload()).json()
    code = plan["plan_code"]
    body = client.post(
        f"/plans/{code}/precision", json={"instrument_id": 1, "reading": 40}
    ).json()
    assert body["outer_reading_class"] == [40, 40]


def test_infeasible_correction_keeps_current_plan(client):
    plan = client.post("/plans", json=_plan_payload()).json()
    code = plan["plan_code"]
    response = client.post(
        f"/plans/{code}/coarse",
        json={"outer": [0, 5], "inner": [0, 5], "total": [100, 120]},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "range_infeasible"

    current = client.get(f"/plans/{code}").json()
    assert current["status"] == "active"
    # 现行方案号仍可用于精测
    ok = client.post(f"/plans/{code}/precision", json={"instrument_id": 1, "reading": 40})
    assert ok.status_code == 200


def test_invalid_tree_and_range_rejected(client):
    payload = _plan_payload()
    payload["tree"] = _chain(13)
    response = client.post("/plans", json=payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_tree"

    payload = _plan_payload()
    payload["coarse"] = {"outer": [0, 5], "inner": [0, 5], "total": [100, 120]}
    response = client.post("/plans", json=payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "range_infeasible"

    payload = _plan_payload()
    payload["instruments"] = [{"id": 1, "step": 0}]
    response = client.post("/plans", json=payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"

    payload = _plan_payload()
    payload["instruments"] = [{"id": 1, "step": 1}, {"id": 1, "step": 2}]
    response = client.post("/plans", json=payload)
    assert response.status_code == 422

    payload = _plan_payload()
    payload["coarse"] = {"outer": [40, 60], "inner": [0, 10], "total": [40, 70], "extra": 1}
    response = client.post("/plans", json=payload)
    assert response.status_code == 422


def test_assess_endpoint_does_not_persist(client):
    payload = _plan_payload()
    response = client.post("/assess", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["assessment"]["solvable"] is True
    assert body["assessment"]["usable_instruments"][0]["instrument_id"] == 1
    # 不应创建任何方案
    assert client.get("/plans/does-not-exist").status_code == 404


def test_readings_endpoint_reports_intervals_and_conflict(client):
    plan = client.post("/plans", json=_plan_payload()).json()
    code = plan["plan_code"]

    good = client.get(f"/plans/{code}/readings/1").json()
    assert good["usable"] is True
    assert good["reading_intervals"][0]["verdict"] == "pass"
    assert good["conflict"] is None

    bad = client.get(f"/plans/{code}/readings/2").json()
    assert bad["usable"] is False
    assert bad["reading_intervals"] is None
    assert bad["conflict"]["reading"] == 50

    missing = client.get(f"/plans/{code}/readings/999")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "instrument_not_in_plan"


def test_string_instrument_id_precision(client):
    payload = _plan_payload()
    payload["instruments"] = [{"id": "gauge-7", "step": 1}]
    plan = client.post("/plans", json=payload).json()
    code = plan["plan_code"]
    response = client.post(
        f"/plans/{code}/precision", json={"instrument_id": "gauge-7", "reading": 55}
    )
    assert response.status_code == 200
    assert response.json()["verdict"] == "rework"
    assert response.json()["instrument_id"] == "gauge-7"


def test_inner_layer_branch_and_total_constraint(client):
    # 内层阈值取 10，覆盖内层整个范围 [0, 10]，因此该层分支不产生歧义；
    # 是否合用只取决于外层阈值 50 能否被步长桶切分。
    payload = {
        "coarse": {"outer": [40, 60], "inner": [0, 10], "total": [45, 65]},
        "tree": {
            "layer": "inner",
            "threshold": 10,
            "le": {
                "layer": "outer",
                "threshold": 50,
                "le": {"verdict": "pass"},
                "gt": {"verdict": "rework"},
            },
            "gt": {"verdict": "pass"},
        },
        "instruments": [{"id": 1, "step": 1}, {"id": 2, "step": 5}],
    }
    body = client.post("/plans", json=payload).json()
    usable = {item["instrument_id"] for item in body["assessment"]["usable_instruments"]}
    assert usable == {1}
    unusable = body["assessment"]["unusable_instruments"][0]
    assert unusable["instrument_id"] == 2
    assert unusable["conflict"]["reading"] == 50
    for sample in unusable["conflict"]["samples"]:
        assert 45 <= sample["total"] <= 65

    code = body["plan_code"]
    assert (
        client.post(
            f"/plans/{code}/precision", json={"instrument_id": 1, "reading": 45}
        ).json()["verdict"]
        == "pass"
    )


def test_inner_dependent_tree_leaves_no_instrument_usable(client):
    """判定依赖内层时，只测外层的仪表在任一档值下都可能两种判定。"""

    payload = {
        "coarse": {"outer": [40, 60], "inner": [0, 10], "total": [45, 65]},
        "tree": {
            "layer": "inner",
            "threshold": 4,
            "le": {
                "layer": "outer",
                "threshold": 50,
                "le": {"verdict": "pass"},
                "gt": {"verdict": "rework"},
            },
            "gt": {"verdict": "rework"},
        },
        "instruments": [{"id": 1, "step": 1}, {"id": 2, "step": 5}],
    }
    body = client.post("/plans", json=payload).json()
    assert body["assessment"]["usable_instruments"] == []
    assert body["assessment"]["solvable"] is False
    assert body["assessment"]["range_feasible"] is True
    conflict = body["assessment"]["unusable_instruments"][0]["conflict"]
    verdicts = {sample["verdict"] for sample in conflict["samples"]}
    assert verdicts == {"pass", "rework"}
    # 两组见证的外层读数必须相同
    assert len({sample["reading"] for sample in conflict["samples"]}) == 1


def test_openapi_schema_available(client):
    response = client.get("/openapi.json")
    assert response.status_code == 200
    paths = response.json()["paths"]
    assert "/plans" in paths
    assert "/plans/{plan_code}/precision" in paths


def test_total_thickness_constraint_changes_conflicts():
    """总厚度约束会裁掉部分叶矩形，进而改变仪表是否合用。"""

    tree = {
        "layer": "outer",
        "threshold": 50,
        "le": {"verdict": "pass"},
        "gt": {"verdict": "rework"},
    }
    # 步长 5，读数档 50 的桶是 [50, 54]；若总厚度上限 50，则 51..54 全被裁掉，
    # 档 50 只剩 pass，仪表合用。
    coarse = engine.CoarseRange(40, 60, 0, 0, 40, 50)
    report = engine.assess(coarse, engine.parse_tree(tree), [engine.Instrument("A", 5)])
    assert report["usable_instruments"][0]["usable"] is True

    coarse_open = engine.CoarseRange(40, 60, 0, 10, 40, 70)
    report_open = engine.assess(
        coarse_open, engine.parse_tree(tree), [engine.Instrument("A", 5)]
    )
    assert report_open["unusable_instruments"][0]["usable"] is False
