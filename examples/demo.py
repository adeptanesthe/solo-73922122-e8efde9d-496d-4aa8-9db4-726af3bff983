#!/usr/bin/env python3
"""对运行中的 API 做一遍演示：选仪表 → 更正粗测 → 提交精测。

用法：
    BASE_URL=http://localhost:8000 python3 examples/demo.py
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")

PLAN = {
    "coarse": {"outer": [40, 60], "inner": [0, 10], "total": [40, 70]},
    "tree": {
        "layer": "outer",
        "threshold": 55,
        "le": {"verdict": "pass"},
        "gt": {"verdict": "rework"},
    },
    "instruments": [
        {"id": "A", "step": 1},
        {"id": "B", "step": 5},
        {"id": "C", "step": 7},
        {"id": "D", "step": 11},
    ],
}


def call(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        BASE_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def show(title: str, status: int, body: dict) -> None:
    print(f"\n=== {title} (HTTP {status}) ===")
    print(json.dumps(body, ensure_ascii=False, indent=2))


def main() -> int:
    status, health = call("GET", "/healthz")
    if status != 200:
        print(f"服务不可用，先启动：docker compose up --build（{health}）")
        return 1

    status, plan = call("POST", "/plans", PLAN)
    show("创建方案", status, plan)
    if status not in (200, 201):
        return 1

    code = plan["plan_code"]
    usable = [item["instrument_id"] for item in plan["assessment"]["usable_instruments"]]
    print(f"\n合用仪表: {usable}")
    for item in plan["assessment"]["unusable_instruments"]:
        conflict = item["conflict"]
        print(
            f"不合用 {item['instrument_id']!r}: 档值 {conflict['reading']} "
            f"同档异判 -> "
            + ", ".join(
                f"外{s['outer']}/内{s['inner']}={s['verdict']}"
                for s in conflict["samples"]
            )
        )

    if not usable:
        print("没有合用仪表，终止演示")
        return 1

    chosen = usable[0]

    # 更正粗测：新范围有解 -> 换方案号
    status, corrected = call(
        "POST",
        f"/plans/{code}/coarse",
        {"outer": [50, 60], "inner": [0, 10], "total": [50, 70]},
    )
    show("更正粗测（有解，换方案号）", status, corrected)
    if status != 200:
        return 1
    new_code = corrected["plan_code"]
    print(f"旧方案号 {code} 状态: ", end="")
    status_old, old = call("GET", f"/plans/{code}")
    print(old["status"], "->", old.get("superseded_by"))

    # 用旧号提交精测应被拒绝
    status_stale, stale = call(
        "POST", f"/plans/{code}/precision", {"instrument_id": chosen, "reading": 50}
    )
    show("用旧方案号提交精测（应被拒）", status_stale, stale)

    # 用新号、合用仪表、可行档值提交
    step = next(
        item["step"]
        for item in corrected["instruments"]
        if item["instrument_id"] == chosen
    )
    reading = 50 // step * step
    status_ok, result = call(
        "POST",
        f"/plans/{new_code}/precision",
        {"instrument_id": chosen, "reading": reading},
    )
    show(f"凭现行方案号提交精测（{chosen}, 档值 {reading}）", status_ok, result)
    return 0 if status_ok == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
