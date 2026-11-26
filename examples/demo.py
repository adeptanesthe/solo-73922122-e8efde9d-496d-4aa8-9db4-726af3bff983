#!/usr/bin/env python3
"""对运行中的 API 做一遍演示：选仪表 → 更正粗测换号 → 对比换号前后选表 → 精测。

重点输出**换号前后同一批仪表的选择结果**，直观看到：
* 新方案号按新范围重新判定；
* 旧方案号仅供查询，保留换号前的原评估；
* 新号上新合用的仪表可以提交精测。

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

# 阈值 55：步长 1 的仪表精确切分；步长 5 的档值 55 桶 [55,59] 跨过阈值，
# 步长 11 的档值 55 桶 [55,65] 也跨过阈值 → 换号前只有 A、C 合用。
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

# 把外层上限收到 55：档值 55 的桶不再跨过阈值，B、D 由不合用变为合用，
# 换号前后选表结果因此明显不同。
CORRECTED_COARSE = {"outer": [40, 55], "inner": [0, 10], "total": [40, 65]}


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


def selection_table(body: dict) -> list[str]:
    """把一份方案响应渲染成"仪表 → 合用？/冲突档值"的选择表。"""

    assessment = body["assessment"]
    usable = [item["instrument_id"] for item in assessment["usable_instruments"]]
    lines = [
        f"方案号 {body['plan_code']}  状态 {body['status']}  "
        f"代次 {body['generation']}  可用 {usable}"
    ]
    for item in assessment["usable_instruments"]:
        lines.append(f"  {item['instrument_id']!s:<4} 步长 {item['step']:<3} 合用")
    for item in assessment["unusable_instruments"]:
        conflict = item["conflict"]
        lines.append(
            f"  {item['instrument_id']!s:<4} 步长 {item['step']:<3} 不合用   "
            f"档值 {conflict['reading']} 同档异判: "
            + ", ".join(
                f"(外{s['outer']},内{s['inner']})={s['verdict']}"
                for s in conflict["samples"]
            )
        )
    return lines


def main() -> int:
    status, health = call("GET", "/healthz")
    if status != 200:
        print(f"服务不可用，先启动：docker compose up --build（{health}）")
        return 1

    status, plan = call("POST", "/plans", PLAN)
    show("创建方案", status, plan)
    if status not in (200, 201):
        return 1
    old_code = plan["plan_code"]

    print("\n### 换号前（旧方案号）选表结果")
    for line in selection_table(plan):
        print(line)

    # 更正粗测：新范围有解 → 换方案号
    status, corrected = call("POST", f"/plans/{old_code}/coarse", CORRECTED_COARSE)
    show("更正粗测（有解，换方案号）", status, corrected)
    if status != 200:
        return 1
    new_code = corrected["plan_code"]

    # 旧号仅供查询：状态已是 superseded，但评估结果仍是换号前那一份。
    status_old, old = call("GET", f"/plans/{old_code}")
    print("\n### 换号后：新旧方案号选表对比")
    print(f"旧号 {old_code} 状态 {old['status']} -> 新号 {old.get('superseded_by')}")
    print("-- 旧号（仅供查询，保留原评估）--")
    for line in selection_table(old):
        print(line)
    print("-- 新号（按新范围判定）--")
    for line in selection_table(corrected):
        print(line)

    new_usable = [
        item["instrument_id"]
        for item in corrected["assessment"]["usable_instruments"]
    ]
    if not new_usable:
        print("新方案号没有合用仪表，终止演示")
        return 1

    # 用旧号提交精测应被拒绝
    status_stale, stale = call(
        "POST", f"/plans/{old_code}/precision", {"instrument_id": new_usable[0], "reading": 55}
    )
    show("用旧方案号提交精测（应被拒）", status_stale, stale)

    # 用新号、合用仪表、可行档值提交
    chosen = new_usable[0]
    step = next(
        item["step"]
        for item in corrected["instruments"]
        if item["instrument_id"] == chosen
    )
    reading = 55 // step * step
    status_ok, result = call(
        "POST",
        f"/plans/{new_code}/precision",
        {"instrument_id": chosen, "reading": reading},
    )
    show(f"凭现行方案号提交精测（{chosen}, 档值 {reading}）", status_ok, result)
    return 0 if status_ok == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
