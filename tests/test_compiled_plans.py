"""编译缓存、并发与快照一致性的测试。"""

from __future__ import annotations

import threading
import time

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


TREE = {
    "layer": "outer",
    "threshold": 50,
    "le": {"verdict": "pass"},
    "gt": {"verdict": "rework"},
}


def _payload(steps=(1, 5, 10), coarse=None, count=None):
    instruments = [{"id": f"g{i}", "step": step} for i, step in enumerate(steps)]
    if count is not None:
        instruments = [
            {"id": f"g{i}", "step": steps[i % len(steps)]} for i in range(count)
        ]
    return {
        "coarse": coarse
        or {"outer": [40, 60], "inner": [0, 10], "total": [40, 70]},
        "tree": TREE,
        "instruments": instruments,
    }


# ---------------------------------------------------------------------------
# 编译一次：树只裁剪一次，每种步长只分档一次
# ---------------------------------------------------------------------------


def test_tree_pruned_once_and_step_profiles_shared(monkeypatch):
    calls = {"collect": 0, "profile": 0}
    real_collect = engine.collect_cells
    real_profile = engine.build_step_profile

    def counting_collect(*args, **kwargs):
        calls["collect"] += 1
        return real_collect(*args, **kwargs)

    def counting_profile(*args, **kwargs):
        calls["profile"] += 1
        return real_profile(*args, **kwargs)

    monkeypatch.setattr(engine, "collect_cells", counting_collect)
    monkeypatch.setattr(engine, "build_step_profile", counting_profile)

    # 1000 台仪表，只有 3 种步长。
    plan = service.create_plan(
        engine.CoarseRange(40, 60, 0, 10, 40, 70),
        TREE,
        [engine.Instrument(f"g{i}", (1, 5, 10)[i % 3]) for i in range(1000)],
    )

    assert calls == {"collect": 1, "profile": 3}
    assert len(plan.analysis.instrument_reports) == 1000
    assert plan.analysis.distinct_steps == 3
    # 相同步长的仪表共用同一个分档对象。
    assert plan.step_profile(5) is plan.step_profile(5)
    assert plan.step_profile(1) is not plan.step_profile(5)

    # 查询方案、查询档值、提交精测都不再重算。
    client = TestClient(app)
    assert client.get(f"/plans/{plan.code}").status_code == 200
    assert client.get(f"/plans/{plan.code}/readings/g1").status_code == 200
    ok = client.post(
        f"/plans/{plan.code}/precision", json={"instrument_id": "g0", "reading": 40}
    )
    assert ok.status_code == 200
    assert calls == {"collect": 1, "profile": 3}


def test_each_plan_code_gets_its_own_single_compile():
    first = service.create_plan(
        engine.CoarseRange(40, 60, 0, 10, 40, 70),
        TREE,
        [engine.Instrument("a", 1), engine.Instrument("b", 5)],
    )
    second = service.create_plan(
        engine.CoarseRange(40, 60, 0, 10, 40, 70),
        TREE,
        [engine.Instrument("a", 1), engine.Instrument("b", 5)],
    )
    # 相同输入、不同方案号：各编译各的，互不共享，生命周期独立。
    assert first.analysis is not second.analysis
    assert first.code != second.code


def test_instruments_return_their_own_ids_from_shared_profile(client):
    body = client.post("/plans", json=_payload(steps=(5, 5, 5))).json()
    unusable = body["assessment"]["unusable_instruments"]
    assert [item["instrument_id"] for item in unusable] == ["g0", "g1", "g2"]
    # 三台同一步长仪表共享见证内容，但各自编号不同。
    assert {item["conflict"]["reading"] for item in unusable} == {50}
    for_id = client.get(f"/plans/{body['plan_code']}/readings/g2").json()
    assert for_id["instrument_id"] == "g2"
    assert for_id["conflict"]["reading"] == 50


# ---------------------------------------------------------------------------
# 换号：新号按新范围判定，旧号仅供查询、保留原评估
# ---------------------------------------------------------------------------


def test_correction_new_code_reassessed_old_code_keeps_original(client):
    created = client.post("/plans", json=_payload()).json()
    old = created["plan_code"]
    assert {
        item["instrument_id"]
        for item in created["assessment"]["usable_instruments"]
    } == {"g0"}

    corrected = client.post(
        f"/plans/{old}/coarse",
        json={"outer": [51, 54], "inner": [0, 0], "total": [51, 54]},
    )
    assert corrected.status_code == 200
    body = corrected.json()
    new = body["plan_code"]
    assert new != old
    # 新范围下步长 5/10 的档值 50 桶全是 rework，三台都合用。
    assert {
        item["instrument_id"]
        for item in body["assessment"]["usable_instruments"]
    } == {"g0", "g1", "g2"}

    old_body = client.get(f"/plans/{old}").json()
    assert old_body["status"] == "superseded"
    assert old_body["superseded_by"] == new
    # 旧号保留原评估与当时的方案号，不因换号而改变。
    assert {
        item["instrument_id"]
        for item in old_body["assessment"]["usable_instruments"]
    } == {"g0"}
    assert old_body["assessment"]["usable_instruments"][0]["instrument_id"] == "g0"
    # 旧号的档值查询仍走旧号自己的缓存。
    old_readings = client.get(f"/plans/{old}/readings/g1").json()
    assert old_readings["status"] == "superseded"
    assert old_readings["usable"] is False
    new_readings = client.get(f"/plans/{new}/readings/g1").json()
    assert new_readings["usable"] is True


def test_precision_on_new_code_uses_new_range(client):
    old = client.post("/plans", json=_payload(steps=(1,))).json()["plan_code"]
    new = client.post(
        f"/plans/{old}/coarse",
        json={"outer": [51, 54], "inner": [0, 0], "total": [51, 54]},
    ).json()["plan_code"]

    # 新范围 51..54 在阈值为 50 的树上全部 rework。
    ok = client.post(
        f"/plans/{new}/precision", json={"instrument_id": "g0", "reading": 51}
    )
    assert ok.status_code == 200
    assert ok.json()["verdict"] == "rework"


# ---------------------------------------------------------------------------
# 并发：精测与更正同时提交，同一方案号最多一项成功
# ---------------------------------------------------------------------------


def test_concurrent_correction_and_precision_only_one_wins(monkeypatch):
    plan = service.create_plan(
        engine.CoarseRange(40, 60, 0, 10, 40, 70),
        TREE,
        [engine.Instrument("g0", 1)],
    )
    entered = threading.Event()
    release = threading.Event()
    real_build = engine.build_analysis

    def stalled_build(coarse, root, instruments):
        # 只在更正的新范围编译时停住，模拟"正在算分档"。
        if coarse.outer_min == 45:
            entered.set()
            assert release.wait(5)
        return real_build(coarse, root, instruments)

    monkeypatch.setattr(engine, "build_analysis", stalled_build)
    results: dict[str, object] = {}

    def correct():
        try:
            service.correct_coarse(
                plan.code, engine.CoarseRange(45, 60, 0, 10, 45, 70)
            )
            results["correction"] = "ok"
        except Exception as exc:  # noqa: BLE001 - 测试记录业务异常
            results["correction"] = exc

    thread = threading.Thread(target=correct)
    thread.start()
    assert entered.wait(5), "更正应已进入分档计算"

    # 更正尚未落库时，精测成功拿到提交前快照。
    precision = service.submit_precision(plan.code, "g0", 40)
    assert precision["verdict"] == "pass"

    release.set()
    thread.join(5)
    assert not thread.is_alive()

    assert isinstance(results["correction"], Exception)
    assert getattr(results["correction"], "code") == "plan_completed"
    # 方案号仍只有一个，结论来自成功的那一项。
    assert service.get_plan(plan.code).status == "completed"


def test_correction_before_precision_starts_supersedes_old_code():
    plan = service.create_plan(
        engine.CoarseRange(40, 60, 0, 10, 40, 70),
        TREE,
        [engine.Instrument("g0", 1)],
    )
    new_plan = service.correct_coarse(
        plan.code, engine.CoarseRange(45, 60, 0, 10, 45, 70)
    )
    assert new_plan.code != plan.code

    with pytest.raises(Exception) as info:
        service.submit_precision(plan.code, "g0", 40)
    assert getattr(info.value, "code") == "plan_superseded"
    assert service.get_plan(plan.code).status == "superseded"


# ---------------------------------------------------------------------------
# 一个批次算分档时，其他批次仍能查询与提交精测
# ---------------------------------------------------------------------------


def test_slow_batch_does_not_block_other_batch(monkeypatch):
    other = service.create_plan(
        engine.CoarseRange(40, 60, 0, 10, 40, 70),
        TREE,
        [engine.Instrument("a", 1)],
    )
    entered = threading.Event()
    release = threading.Event()
    real_build = engine.build_analysis

    def stalled_build(coarse, root, instruments):
        if coarse.outer_min == 1000:
            entered.set()
            assert release.wait(5)
        return real_build(coarse, root, instruments)

    monkeypatch.setattr(engine, "build_analysis", stalled_build)

    slow = threading.Thread(
        target=lambda: service.create_plan(
            engine.CoarseRange(1000, 1100, 0, 10, 1000, 1110),
            TREE,
            [engine.Instrument(f"s{i}", 5) for i in range(1000)],
        )
    )
    slow.start()
    assert entered.wait(5), "慢批次应已进入分档计算"

    started = time.monotonic()
    client = TestClient(app)
    assert client.get(f"/plans/{other.code}").status_code == 200
    assert client.get(f"/plans/{other.code}/readings/a").status_code == 200
    ok = client.post(
        f"/plans/{other.code}/precision",
        json={"instrument_id": "a", "reading": 40},
    )
    elapsed = time.monotonic() - started
    assert ok.status_code == 200
    # 慢批次仍在分档时，另一批次的查询与精测已全部完成。
    assert slow.is_alive()
    assert elapsed < 2

    release.set()
    slow.join(5)
    assert not slow.is_alive()


# ---------------------------------------------------------------------------
# 同一时点的一致性：一次查询只看到同一份快照
# ---------------------------------------------------------------------------


def test_snapshot_is_immutable_across_state_change(client):
    body = client.post("/plans", json=_payload(steps=(1,))).json()
    code = body["plan_code"]
    before = service.get_plan(code)
    assert before.status == "active"
    assert before.result is None
    assert len(before.history) == 0

    client.post(f"/plans/{code}/precision", json={"instrument_id": "g0", "reading": 40})

    # 旧快照不变，新快照自洽。
    assert before.status == "active"
    assert before.result is None
    assert len(before.history) == 0
    after = service.get_plan(code)
    assert after.status == "completed"
    assert after.result["verdict"] == "pass"
    assert after.revision == before.revision + 1
    assert after.history[-1]["event"] == "precision_submitted"

    # 一次 GET 的四个字段来自同一快照。
    latest = client.get(f"/plans/{code}").json()
    assert latest["status"] == "completed"
    assert latest["result"]["verdict"] == "pass"
    assert latest["history"][-1]["event"] == "precision_submitted"
    assert latest["revision"] == after.revision
    assert latest["assessment"]["solvable"] is True


def test_precision_response_reports_revision(client):
    code = client.post("/plans", json=_payload(steps=(1,))).json()["plan_code"]
    response = client.post(
        f"/plans/{code}/precision", json={"instrument_id": "g0", "reading": 40}
    ).json()
    assert response["revision"] == 2
    assert client.get(f"/plans/{code}").json()["revision"] == 2


def _race(workers):
    """同时启动若干 worker，返回各自的成功/异常结果。"""

    barrier = threading.Barrier(len(workers))
    results: list[object] = [None] * len(workers)

    def run(index, fn):
        barrier.wait(5)
        try:
            results[index] = fn()
        except Exception as exc:  # noqa: BLE001 - 测试记录业务异常
            results[index] = exc

    threads = [
        threading.Thread(target=run, args=(i, fn))
        for i, fn in enumerate(workers)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert all(not thread.is_alive() for thread in threads)
    return results


def test_racing_precisions_only_one_succeeds():
    plan = service.create_plan(
        engine.CoarseRange(40, 60, 0, 10, 40, 70),
        TREE,
        [engine.Instrument("g0", 1), engine.Instrument("g1", 1)],
    )
    workers = [
        lambda: service.submit_precision(plan.code, "g0", 40),
        lambda: service.submit_precision(plan.code, "g1", 55),
        lambda: service.submit_precision(plan.code, "g0", 55),
    ]
    results = _race(workers)
    successes = [item for item in results if isinstance(item, dict)]
    failures = [item for item in results if isinstance(item, Exception)]
    assert len(successes) == 1
    assert len(failures) == 2
    assert {getattr(item, "code") for item in failures} == {"plan_completed"}
    # 状态与结论来自唯一成功的那一项。
    final = service.get_plan(plan.code)
    assert final.status == "completed"
    assert final.result["verdict"] == successes[0]["verdict"]


def test_racing_corrections_only_one_succeeds():
    plan = service.create_plan(
        engine.CoarseRange(40, 60, 0, 10, 40, 70),
        TREE,
        [engine.Instrument("g0", 1)],
    )
    workers = [
        lambda: service.correct_coarse(
            plan.code, engine.CoarseRange(45, 60, 0, 10, 45, 70)
        ),
        lambda: service.correct_coarse(
            plan.code, engine.CoarseRange(46, 60, 0, 10, 46, 70)
        ),
    ]
    results = _race(workers)
    successes = [item for item in results if not isinstance(item, Exception)]
    failures = [item for item in results if isinstance(item, Exception)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert getattr(failures[0], "code") == "plan_superseded"
    current_code = service.get_plan(plan.code).superseded_by
    assert current_code == successes[0].code
    # 胜出的新方案号与最终指向一致。
    assert service.get_plan(current_code).status == "active"
