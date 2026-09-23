# 差分隐私预算账本 API（DP Budget Ledger）

基于 **Python · FastAPI · SQLite · asyncio** 的差分隐私预算账本。客户端为数据集登记
ε/δ 总预算上限，随后提交携带 `request_id`、查询类型、ε、δ 的分析申请；服务端按
**Rényi Differential Privacy（RDP）** 在一组 orders 上计算多次申请的组合消耗，
对重复 `request_id` 幂等处理，并在并发申请下**原子地**拒绝超预算请求或预留成功。

## 特性

- **RDP 组合**：每次申请的 (ε, δ)-DP 按
  `ε_RDP(α) = ε + log(1/δ)/(α−1)` 转换到 α orders（默认
  `2,3,4,5,6,8,12,16,24,32,48,64,96,128,256`）；同 α 上线性相加；
  再按 `ε(α) = ε_RDP(α) + log(1/δ)/(α−1)` 对所有 α 取最小值得到组合后的 (ε, δ)-DP。
- **δ 精确组合**：`δ_total = 1 − Π(1 − δᵢ)`（基本组合定理精确形式）。
- **幂等预留**：`(dataset_id, request_id)` 唯一约束；相同请求重放返回首次结果
  （`replayed: true`），相同 ID 不同参数返回 `409`。
- **并发原子性**：每个预留事务以 `BEGIN IMMEDIATE` 立即获取 SQLite 写锁，
  “读当前消耗 → 判预算 → 写预留”全程串行化；`SQLITE_BUSY` 指数退避重试。
  并发下要么预留成功（`201/accepted`），要么原子拒绝（`400/rejected`，落库但不消耗预算）。
- **预算查询**：已消耗 ε/δ、剩余额度、accepted/rejected 计数与完整 RDP 曲线。
- **一键启动**：Docker Compose 构建并运行，SQLite 文件持久化到命名卷，自带健康检查。

## 目录结构

```
dp-budget-ledger/
├── app/
│   ├── __init__.py
│   ├── config.py        # 环境变量配置
│   ├── db.py            # aiosqlite 异步持久层 + IMMEDIATE 事务
│   ├── rdp.py           # Rényi DP 组合数学
│   ├── models.py        # Pydantic 请求/响应模型
│   ├── ledger.py        # 预算账本业务逻辑（原子预留/幂等/汇总）
│   └── main.py          # FastAPI 入口与路由
├── tests/test_api.py    # RDP 数学 + API 端到端 + 并发测试（pytest）
├── scripts/demo.py      # 纯标准库端到端演示（含并发）
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 一键启动（Docker Compose）

```bash
cd dp-budget-ledger
docker compose up --build -d
```

查看状态 / 日志 / 停止：

```bash
docker compose ps
docker compose logs -f
docker compose down            # 停止；数据保留在命名卷 dp-ledger-data 中
docker compose down -v         # 停止并删除数据卷（清空账本）
```

- **API 地址**：`http://localhost:8000`
- **交互式文档（Swagger UI）**：`http://localhost:8000/docs`
- **ReDoc**：`http://localhost:8000/redoc`
- **OpenAPI Schema**：`http://localhost:8000/openapi.json`
- **健康检查**：`GET http://localhost:8000/health`

> 端口可在启动前通过环境变量改：`DP_LEDGER_PORT=9000 docker compose up --build -d`
> （映射为 `9000:8000`）。

## 配置

均为可选环境变量（见 `app/config.py`）：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DP_LEDGER_DB_PATH` | `/data/ledger.db`（容器内） | SQLite 文件路径，Compose 已挂载命名卷 |
| `DP_LEDGER_PORT` | `8000` | 宿主机映射端口（compose 内生效） |
| `DP_LEDGER_TOLERANCE` | `1e-12` | 预算比较的浮点容忍度 |
| `DP_LEDGER_TX_RETRIES` | `50` | 写锁冲突最大重试次数（指数退避） |

## API 一览

### 1. 登记数据集（预算上限）

```bash
curl -X POST http://localhost:8000/v1/datasets \
  -H 'Content-Type: application/json' \
  -d '{"dataset_id":"ds-1","epsilon_limit":1.0,"delta_limit":1e-5}'
# 201
```

- `epsilon_limit`：ε 总预算上限（≥0）
- `delta_limit`：δ 总预算上限，`[0,1]`，允许 `0`（纯 DP）
- 重复登记返回 `409`。

### 2. 提交分析申请（预留预算，幂等）

```bash
curl -X POST http://localhost:8000/v1/datasets/ds-1/requests \
  -H 'Content-Type: application/json' \
  -d '{"request_id":"q-001","query_type":"count","epsilon":0.4,"delta":0.0}'
```

响应语义：

| 场景 | HTTP | body.status | 说明 |
| --- | --- | --- | --- |
| 预留成功 | `201` | `accepted` | 预算已原子预留并落库 |
| 超预算被拒 | `400` | `rejected` | 原子拒绝、落库留痕、`reason` 说明超 ε/δ；不消耗预算 |
| 重复 request_id（参数相同） | `200`/`400`（同首次） | 首次状态 | 幂等重放，`replayed: true`，不重复扣预算 |
| 同 request_id 参数不同 | `409` | — | 冲突，拒绝歧义写入 |
| 数据集不存在 | `404` | — | — |

判定方式：把本次申请加入所有已 accepted 申请后重算 RDP 组合，
若组合 ε > ε 上限 或 组合 δ > δ 上限则拒绝（δ 组合用
`1 − Π(1−δᵢ)` 精确计算，ε 在实际组合 δ 处折算取各 order 最小值）。

### 3. 查询单个申请 / 列出全部申请

```bash
curl http://localhost:8000/v1/datasets/ds-1/requests/q-001
curl http://localhost:8000/v1/datasets/ds-1/requests
```

### 4. 预算明细与剩余额度

```bash
curl http://localhost:8000/v1/datasets/ds-1/budget
```

```json
{
  "dataset_id": "ds-1",
  "epsilon_limit": 1.0,
  "delta_limit": 1e-05,
  "epsilon_consumed": 0.9,
  "delta_consumed": 0.0,
  "epsilon_remaining": 0.1,
  "delta_remaining": 1e-05,
  "accepted_requests": 2,
  "rejected_requests": 1,
  "rdp_curve": [{"order": 2.0, "epsilon": 0.9}, {"order": 3.0, "epsilon": 0.9}]
}
```

### 5. 数据集列表

```bash
curl http://localhost:8000/v1/datasets
```

## 本地开发（不用 Docker）

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
# 默认 SQLite 路径 ./ledger.db，可用 DP_LEDGER_DB_PATH 覆盖
```

## 测试与演示

```bash
pip install -r requirements.txt pytest httpx
python -m pytest -q                 # 13 个用例：RDP 性质、幂等、拒绝、并发原子性

# 端到端演示（先启动服务，仅用标准库，无需安装 requests）
python scripts/demo.py                       # 默认 http://127.0.0.1:8000
python scripts/demo.py http://localhost:8000
```

演示脚本覆盖：登记 → 预留 → 幂等重放（200/`replayed`）→ 同 ID 冲突（409）
→ 超预算拒绝（400）→ 预算明细 → **10 线程并发各申请 ε=0.3、总预算 ε=2.0，
断言恰好 6 个 accepted、4 个 rejected**。

## 并发正确性要点

1. 每次预留使用独立连接，进入事务即 `BEGIN IMMEDIATE`（SQLite RESERVED 写锁），
   临界区内完成读取历史、RDP 组合判定、插入 accepted/rejected 记录，
   提交前其他写事务全部排队，杜绝“读旧值 → 双重预留”。
2. 写锁等待经 `PRAGMA busy_timeout=5000` 与应用层指数退避双重兜底。
3. 同一 `request_id` 的并发提交即使同时穿过判定，也由
   `UNIQUE(dataset_id, request_id)` 索引保证只写入一次，其余为重放。
4. 被拒绝的申请同样占用 `request_id` 并可幂等重放，但不参与预算组合。

## 方法学说明

- (ε,δ)-DP → RDP 与 RDP → (ε,δ)-DP 转换依据 Mironov (2017), *Rényi Differential
  Privacy* 的 Proposition 3；RDP 对顺序组合在每个 α 上线性可加。
- orders 集合可在 `app/config.py` 中调整（orders 越密、最大值越大，界越紧）。
- 本服务的组合模型对应**顺序组合 / 子并行不跨数据集复用**的标准账本语义。
