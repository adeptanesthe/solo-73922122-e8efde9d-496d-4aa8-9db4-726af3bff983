# 双层复合膜厚度判定 API

FastAPI 服务：接收两层厚度与总厚度的整数闭区间、一棵最多 12 个叶节点的
合格判定树，以及待选测厚仪的编号与正整数步长；列出**每个可能档值只对应
一种判定**的仪表，对不合用的仪表给出**同档异判**的两组厚度见证，并支持
凭现行方案号提交精测档值。

## 业务规则

* 判定树每个分支比较**某一层**厚度是否 `<=` 整数阈值；叶节点判定为
  `pass`（合格）或 `rework`（返工）。叶节点数最多 12。
* 两层厚度为非负整数，总厚度 = 外层 + 内层，且必须落在 `total` 区间内。
* 每台仪表把**外层**厚度向下取整到自身步长的整倍数，得到"档值"。
  同一档值下若存在两种判定，该仪表不合用。
* 判断**不做厚度枚举**：只对叶节点的轴对齐矩形、步长桶与总厚度约束做
  整数区间运算，复杂度 O(叶节点数 × 仪表数)，与量程大小无关
  （测试覆盖了 `10^18` 量程）。
* 状态机：新建方案 → 可更正粗测（新范围有解则生成新方案号并作废旧号；
  无解则保留现状）→ 凭**现行**方案号提交合用仪表与实测档值；档值可行
  才返回合格/返工结论，并结束本轮。精测被拒、方案号已作废或已结束时
  均保留现状。

## 目录

```
app/
  engine.py    区间算法：树裁剪、档值区间、合用性、见证点
  schemas.py   pydantic 请求模型（严格整数校验）
  service.py   方案状态机
  main.py      FastAPI 路由与统一错误格式
tests/
  test_api.py  单元测试 + 与暴力枚举对照（小范围）
examples/      演示脚本
Dockerfile
docker-compose.yml
```

## 本地运行

```bash
python3 -m pip install --user --break-system-packages -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

## Docker Compose

```bash
docker compose up --build
# API:  http://localhost:8000
# Docs: http://localhost:8000/docs
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/healthz` | 健康检查 |
| POST | `/plans` | 创建方案号并评估仪表 |
| POST | `/assess` | 仅评估，不落库 |
| GET | `/plans/{plan_code}` | 查询方案状态与结果 |
| POST | `/plans/{plan_code}/coarse` | 更正粗测范围（换方案号/作废旧号） |
| POST | `/plans/{plan_code}/precision` | 凭现行方案号提交精测档值 |
| GET | `/plans/{plan_code}/readings/{instrument_id}` | 查询某仪表可行档值 |

### 创建方案

```bash
curl -s -X POST http://localhost:8000/plans \
  -H 'Content-Type: application/json' \
  -d '{
    "coarse": {"outer": [40, 60], "inner": [0, 10], "total": [40, 70]},
    "tree": {
      "layer": "outer", "threshold": 50,
      "le": {"verdict": "pass"},
      "gt": {"verdict": "rework"}
    },
    "instruments": [
      {"id": 1, "step": 1},
      {"id": 2, "step": 5},
      {"id": 3, "step": 10}
    ]
  }'
```

返回中 `usable_instruments` 为合用仪表；`unusable_instruments` 的各条带
`conflict.samples`，是同一档值下分别判合格、返工的两组厚度。

### 提交精测

```bash
curl -s -X POST http://localhost:8000/plans/<plan_code>/precision \
  -H 'Content-Type: application/json' \
  -d '{"instrument_id": 1, "reading": 40}'
```

成功时 `status` 变为 `completed`，并返回 `verdict`（`pass`/`rework`）
与 `round_ended: true`。档值越界/非步长整倍数/判定不唯一、仪表不合用、
方案号已作废或已结束，都会返回 4xx 且不改动方案状态。

> 注意：判定依赖内层厚度时，只测外层的仪表在任何档值下都可能出现两种判定，
> 因此这类方案没有合用仪表（`solvable: false`）。这是题设条件下的必然结论。

### 更正粗测

```bash
curl -s -X POST http://localhost:8000/plans/<plan_code>/coarse \
  -H 'Content-Type: application/json' \
  -d '{"outer": [45, 60], "inner": [0, 10], "total": [45, 70]}'
```

新范围有解（两层区间与总厚度区间相容）则返回新 `plan_code`（`generation`
加一），旧号 `status` 变 `superseded` 并给出 `superseded_by`；新范围无解或
已被更正过/本轮已结束时返回 4xx，现有方案号保留不变。

## 运行测试

```bash
python3 -m pytest -q
```

## 已知限制

* 方案状态存于进程内存：多副本/重启会丢失，生产部署需替换为共享存储。
* 单进程锁只保证同进程内的一致性。
