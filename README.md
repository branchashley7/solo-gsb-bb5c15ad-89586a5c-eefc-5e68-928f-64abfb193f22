# 冷链周转箱事件接收 API

基于 **Python 3.11 + FastAPI + SQLAlchemy 2.0 + SQLite** 的设备温度事件接收服务。
设备会**重试**或**延迟/乱序**上报，服务按以下规则处理：

- **相同箱号 + 相同序号 + 相同内容**（温度、事件时间）→ 判定为设备重试的重复上报，
  幂等返回已有记录（HTTP `200`，`status=duplicate`），**不改变任何状态**。
- **相同箱号 + 相同序号但内容不同** → 拒绝（HTTP `409 Conflict`）。
- **乱序/延迟事件**（序号先到 3 后到 1、中间跳号等）→ 正常入库；
  查询“最新状态”时始终取每个箱子**序号最大**的一条，与到达顺序无关。
- **交接快照**（`POST /handoffs`）：按交接编号在**同一事务**中冻结各箱交接时点的
  最大序号状态（序号、温度、事件时间与缺报/越界/正常结论）。快照一经建立，
  后续乱序补报**不得改写**；同编号同内容重放返回原快照，内容不同返回 `409`。

## 目录结构

```
.
├── app/
│   ├── __init__.py
│   ├── config.py          # 环境变量配置
│   ├── database.py        # 引擎/会话，启动建表
│   ├── models.py          # BoxEvent / Handoff / HandoffItem 模型
│   ├── schemas.py         # 请求/响应模型与时间归一化
│   ├── services.py        # 幂等去重、冲突判定、最新状态查询、保序回归时钟校正、超温积分、时间加权温度中位数、轨迹压缩预览、Merkle 包含证明、交接快照冻结
│   └── main.py            # FastAPI 路由
├── Dockerfile
├── docker-compose.yml
├── docker-entrypoint.sh   # 容器启动：初始化建表 -> 启动服务
└── requirements.txt
```

## 一键启动（Docker Compose）

在仓库根目录执行**一条命令**即可构建镜像、建表并在 `http://localhost:8000` 启动服务：

```bash
docker compose up -d --build
```

随后即可访问：

- 健康检查：`curl http://localhost:8000/health`
- Swagger 交互文档：<http://localhost:8000/docs>

启动过程会自动完成：

1. 基于 `requirements.txt` 在镜像内安装全部依赖；
2. 容器入口脚本 `docker-entrypoint.sh` 创建数据目录并执行建表初始化（幂等）；
3. 启动 uvicorn。

数据持久化在命名卷 `coldchain-data`（容器内 `/app/data`），重启/重建容器不丢数据。

常用命令：

```bash
docker compose logs -f        # 查看日志
docker compose down           # 停止
docker compose down -v        # 停止并删除数据卷（清空数据）
```

## 本地直接运行（不用 Docker）

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

首次请求前/启动时会自动创建 SQLite 表（`./data/coldchain.db`）。

## 配置

通过环境变量配置（均有默认值）：

| 环境变量       | 默认值                          | 说明                                       |
| -------------- | ------------------------------- | ------------------------------------------ |
| `DATABASE_URL` | `sqlite:///./data/coldchain.db` | 数据库连接串（SQLAlchemy 格式）            |
| `APP_HOST`     | `0.0.0.0`                       | 监听地址                                   |
| `APP_PORT`     | `8000`                          | 监听端口（Compose 左侧映射端口同样取它）   |

Compose 下可用 `.env` 或临时变量覆盖，例如改宿主端口：

```bash
APP_PORT=9000 docker compose up -d --build
# 此时宿主访问端口为 9000
```

## API 地址与端口

- 服务地址：`http://localhost:8000`（按上面的端口配置调整）
- Swagger 交互文档：<http://localhost:8000/docs>
- ReDoc：<http://localhost:8000/redoc>

## 接口一览

| 方法 | 路径                          | 说明                                   |
| ---- | ----------------------------- | -------------------------------------- |
| GET  | `/health`                     | 健康检查                               |
| POST | `/events`                     | 上报一个事件（幂等，支持设备重试）     |
| POST | `/handoffs`                   | 创建/幂等重放交接快照（同事务冻结各箱最大序号状态） |
| GET  | `/handoffs/{handoff_no}`      | 按交接编号读取冻结快照明细与各类数量   |
| GET  | `/states`                     | 所有箱子的最新状态（按最大序号）       |
| GET  | `/boxes`                      | 所有上报过的箱号                       |
| GET  | `/boxes/{box_code}/state`     | 单个箱子的最新状态                     |
| GET  | `/boxes/{box_code}/events`    | 单个箱子的全部事件（按序号升序）       |
| GET  | `/boxes/{box_code}/clock-audit` | 区间事件的设备时钟回拨校正建议（保序回归，不改原始记录） |
| GET  | `/boxes/{box_code}/heat-exposure` | 区间事件的超温积分（℃·秒，线性插值 + 保序校正时间，不改原始记录） |
| GET  | `/boxes/{box_code}/temperature-median` | 区间按持续时间加权的温度中位数（线性插值 + 保序校正时间，不改原始记录） |
| GET  | `/boxes/{box_code}/trajectory-compression` | 区间温度轨迹压缩预览（最少保留点 + 线性插值误差受限，只读不写回） |
| GET  | `/boxes/{box_code}/merkle-proof` | 区间事件的 Merkle 包含证明（按 seq 升序建树，只读不写回） |

### 上报事件

`POST /events`

请求体：

```json
{
  "box_code": "BOX-001",
  "seq": 0,
  "temperature": -4.5,
  "event_time": "2026-09-23T10:00:00+08:00"
}
```

字段说明：

- `box_code`：箱号，非空字符串（≤64 字符）。
- `seq`：该箱递增的事件序号，非负整数；不要求连续，乱序/补报均可。
- `temperature`：摄氏温度，范围 `-100 ~ 100`。
- `event_time`：ISO 8601 时间；**建议带时区**。服务统一换算为 UTC 存储与比较，
  不带时区的时间按 UTC 处理。

响应：

- 首次入库：`201 Created`，`status = "accepted"`
- 重复上报（同序号且温度、事件时间完全一致）：`200 OK`，`status = "duplicate"`
- 同序号内容不一致：`409 Conflict`，响应体带库中已有值与本次值，便于排查
- 参数非法：`422 Unprocessable Entity`
- 查询不存在的箱子：`404 Not Found`

## 使用示例

```bash
# 上报 seq=1
curl -X POST http://localhost:8000/events \
  -H 'Content-Type: application/json' \
  -d '{"box_code":"BOX-001","seq":1,"temperature":-4.5,
       "event_time":"2026-09-23T10:01:00+08:00"}'

# 设备重试：原样重发 -> 200 duplicate，状态不变
curl -X POST http://localhost:8000/events \
  -H 'Content-Type: application/json' \
  -d '{"box_code":"BOX-001","seq":1,"temperature":-4.5,
       "event_time":"2026-09-23T10:01:00+08:00"}'

# 同序号改温度 -> 409 拒绝
curl -X POST http://localhost:8000/events \
  -H 'Content-Type: application/json' \
  -d '{"box_code":"BOX-001","seq":1,"temperature":9.9,
       "event_time":"2026-09-23T10:01:00+08:00"}'

# 乱序补报 seq=0 / seq=2 ... 最新状态始终取最大 seq
curl http://localhost:8000/boxes/BOX-001/state
curl http://localhost:8000/states
```

## 交接快照（handoffs）

交接（如冷库入库、承运商换车）时需要一份**冻结**的当时状态清单。
`POST /handoffs` 接收交接编号、箱号列表及温度上下限，在**同一数据库事务**中：

1. 校验：箱号列表**不能为空、不能重复**；`temp_lower <= temp_upper`
   （上下限必须是有限数值），不满足返回 `422`。
2. 按各箱**当前最大 seq** 读取状态（乱序安全，缺号不影响），逐箱冻结
   **序号、温度、事件时间**与结论：
   - 库中从无该箱事件 → `missing`（缺报，序号/温度/时间为 `null`）；
   - 温度**严格**低于下限或高于上限 → `out_of_range`（越界）；
   - 温度在闭区间 `[temp_lower, temp_upper]` 内 → `normal`，
     **恰等下限/上限的边界值算正常**。
3. 主表 + 明细一次提交。快照只写一次：**后续乱序补报进入事件表，不回写快照**。

交接编号的幂等与冲突语义：

- 编号不存在 → 创建快照，`201 Created`，`status="accepted"`；
- 编号已存在且**内容一致**（温度上下限相同、箱号集合相同，箱号顺序不影响）
  → 返回**原快照**，`200 OK`，`status="duplicate"`，冻结值与创建时间均不变；
- 编号已存在但**内容不同**（箱号增减或上下限变化）→ `409 Conflict`，
  响应体带库中已有内容与本次内容，且**不会改动原快照**。

`GET /handoffs/{handoff_no}` 返回冻结明细（按请求箱号顺序）与
`summary` 各类数量（`total = missing + out_of_range + normal`）；编号不存在返回 `404`。

```bash
# 先上报几箱（UTC）
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"box_code":"BOX-H1","seq":0,"temperature":-4.5,"event_time":"2026-09-24T02:00:00+00:00"}'
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"box_code":"BOX-H1","seq":1,"temperature":-4.2,"event_time":"2026-09-24T02:01:00+00:00"}'
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"box_code":"BOX-H2","seq":0,"temperature":9.0,"event_time":"2026-09-24T02:00:00+00:00"}'
# BOX-H3 不上报 -> 缺报

# 创建交接快照：下限 0℃、上限 8℃，边界值算正常
curl -X POST http://localhost:8000/handoffs -H 'Content-Type: application/json' \
  -d '{"handoff_no":"HAND-2026-001",
       "box_codes":["BOX-H1","BOX-H2","BOX-H3"],
       "temp_lower":0,"temp_upper":8}'
```

响应（BOX-H1 正常且冻结最大序号 seq=1；BOX-H2 越界；BOX-H3 缺报）：

```json
{
  "status": "accepted",
  "handoff_no": "HAND-2026-001",
  "temp_lower": 0.0,
  "temp_upper": 8.0,
  "created_at": "2026-09-24T02:05:00.123456",
  "items": [
    {"box_code": "BOX-H1", "seq": 1, "temperature": -4.2,
     "event_time": "2026-09-24T02:01:00", "conclusion": "out_of_range"},
    {"box_code": "BOX-H2", "seq": 0, "temperature": 9.0,
     "event_time": "2026-09-24T02:00:00", "conclusion": "out_of_range"},
    {"box_code": "BOX-H3", "seq": null, "temperature": null,
     "event_time": null, "conclusion": "missing"}
  ],
  "summary": {"total": 3, "missing": 1, "out_of_range": 2, "normal": 0}
}
```

> 说明：上例下限取 `0` 仅为演示越界；冷链常见区间如 `-18 ~ -2`，
> 恰等 `-18` 或 `-2` 的箱子均记 `normal`。

```bash
# 同编号同内容重放（即使调整箱号顺序）-> 200 duplicate，返回原快照
curl -X POST http://localhost:8000/handoffs -H 'Content-Type: application/json' \
  -d '{"handoff_no":"HAND-2026-001",
       "box_codes":["BOX-H3","BOX-H2","BOX-H1"],
       "temp_lower":0,"temp_upper":8}'

# 同编号内容不同（改上限/增减箱号）-> 409，原快照不变
curl -i -X POST http://localhost:8000/handoffs -H 'Content-Type: application/json' \
  -d '{"handoff_no":"HAND-2026-001",
       "box_codes":["BOX-H1","BOX-H2","BOX-H3"],
       "temp_lower":0,"temp_upper":7}'

# 读取冻结明细
curl http://localhost:8000/handoffs/HAND-2026-001

# 编号不存在 -> 404
curl -i http://localhost:8000/handoffs/NO-SUCH

# 乱序补报只影响实时状态，不改写已建快照：
# 给 BOX-H3 补报、给 BOX-H1 补 seq=9 后再 GET，HAND-2026-001 仍是上面的冻结值
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"box_code":"BOX-H3","seq":0,"temperature":-5.0,"event_time":"2026-09-24T03:00:00+00:00"}'
curl http://localhost:8000/handoffs/HAND-2026-001

# 箱号重复 / 下限高于上限 / 空箱号列表 / NaN -> 422
curl -i -X POST http://localhost:8000/handoffs -H 'Content-Type: application/json' \
  -d '{"handoff_no":"H-BAD","box_codes":["BOX-H1","BOX-H1"],"temp_lower":0,"temp_upper":8}'
curl -i -X POST http://localhost:8000/handoffs -H 'Content-Type: application/json' \
  -d '{"handoff_no":"H-BAD","box_codes":["BOX-H1"],"temp_lower":8,"temp_upper":0}'
```

## 时钟回拨校正（clock-audit）

设备 RTC 发生回拨时，后发生的事件其 `event_time` 反而更早，物理时间序列会出现“倒退”。
`GET /boxes/{box_code}/clock-audit?seq_from=&seq_to=` 对**指定序号区间（含端点）**
即时生成校正建议，**不会改动任何原始记录**；乱序补报后再次查询会基于最新数据重新计算。

算法：按 `seq` 升序取出区间内事件，将 UTC 事件时间换算为**整数微秒**，做
**等权保序回归**（isotonic regression，PAVA）——在建议时间非递减的约束下，
最小化各事件时间偏移的平方和；实现方式即把相邻的违规块不断合并、块内取算术均值，
均值四舍五入到微秒。例如连续两个时间分别为第 10 秒、第 0 秒时，两条都会建议为
第 5 秒（前面的往后拨、后面的往前调），而不是只把后者改成 10 秒。

查询参数：

- `seq_from` / `seq_to`：序号区间，**均为整数且含端点**，必须 `>= 0` 且 `seq_from <= seq_to`。

返回：数组，每个元素对应一条区间内事件（按 seq 升序）；区间内无事件返回 `[]`。

| 字段             | 说明                                                       |
| ---------------- | ---------------------------------------------------------- |
| `seq`            | 事件序号                                                   |
| `original_time`  | 原始 UTC 事件时间（库中记录，未被修改）                    |
| `suggested_time` | 保序回归后的建议 UTC 时间                                  |
| `offset_micros`  | 建议时间 − 原时间，单位微秒（正=向前修正，负=回拨修正）    |
| `block_seq_from` / `block_seq_to` | 该事件所属合并块的序号范围（同一块内建议时间相同） |

错误情况：箱号不存在返回 `404`；缺少参数、参数非整数、负数或 `seq_from > seq_to`
返回 `422`。

```bash
# 先造一组带时钟回拨的事件（UTC）：seq=0 在第 10 秒，seq=1 倒退到第 0 秒
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"box_code":"BOX-AUDIT","seq":0,"temperature":-4.5,
       "event_time":"2026-09-23T02:00:10+00:00"}'
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"box_code":"BOX-AUDIT","seq":1,"temperature":-4.6,
       "event_time":"2026-09-23T02:00:00+00:00"}'
# 再补一条正常推进的 seq=2
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"box_code":"BOX-AUDIT","seq":2,"temperature":-4.7,
       "event_time":"2026-09-23T02:00:20+00:00"}'

# 查询 0..2 的校正建议
curl 'http://localhost:8000/boxes/BOX-AUDIT/clock-audit?seq_from=0&seq_to=2'
```

响应（seq=0、seq=1 合并为一个块，都建议为 02:00:05；seq=2 独立成块）：

```json
[
  {
    "seq": 0,
    "original_time": "2026-09-23T02:00:10",
    "suggested_time": "2026-09-23T02:00:05",
    "offset_micros": -5000000,
    "block_seq_from": 0,
    "block_seq_to": 1
  },
  {
    "seq": 1,
    "original_time": "2026-09-23T02:00:00",
    "suggested_time": "2026-09-23T02:00:05",
    "offset_micros": 5000000,
    "block_seq_from": 0,
    "block_seq_to": 1
  },
  {
    "seq": 2,
    "original_time": "2026-09-23T02:00:20",
    "suggested_time": "2026-09-23T02:00:20",
    "offset_micros": 0,
    "block_seq_from": 2,
    "block_seq_to": 2
  }
]
```

```bash
# 箱号存在但区间内无事件 -> 200，空数组
curl 'http://localhost:8000/boxes/BOX-AUDIT/clock-audit?seq_from=100&seq_to=200'
# []

# 箱号不存在 -> 404
curl -i 'http://localhost:8000/boxes/NO-SUCH-BOX/clock-audit?seq_from=0&seq_to=1'

# 范围非法（seq_from > seq_to）-> 422
curl -i 'http://localhost:8000/boxes/BOX-AUDIT/clock-audit?seq_from=5&seq_to=2'
```

## 超温积分（heat-exposure）

`GET /boxes/{box_code}/heat-exposure`（默认访问端口 **8000**）计算指定序号区间内
温度超过上限的**累计暴露量**，单位为 **℃·秒**：

- 按 `seq` 升序取区间内事件，事件时间沿用 clock-audit 的**等权保序回归校正**
  （只读即时计算，不改原始记录；乱序补报后重查即自动重算）。
- 相邻两条事件之间温度视为**线性变化**，对超过 `upper_limit` 的部分做**精确积分**
  （两端都超为梯形、一端超出为三角形、都不超为 0）。
- **缺号不自动补点**：相邻指区间内实际存在的相邻两条事件，序号可以跳号。
- 校正后间隔**为零**（同一合并块）或**超过 `max_gap_seconds`** 的段**不计入积分**，
  也不出现在明细中（以 `skipped_segment_count` 计数）。
- 箱号不存在返回 `404`；区间内无事件返回零值与空明细；参数非法返回 `422`。

查询参数：

- `seq_from` / `seq_to`：序号区间，**均为整数且含端点**，必须 `>= 0` 且 `seq_from <= seq_to`。
- `upper_limit`：温度上限（℃），超过部分计入积分，须为有限数值。
- `max_gap_seconds`：允许计入的最大相邻校正间隔（秒），**必须为正数**（有限数值）。

返回字段：

| 字段                     | 说明                                                       |
| ------------------------ | ---------------------------------------------------------- |
| `event_count`            | 区间内事件条数                                             |
| `segment_count`          | 计入积分的段数（即 `segments` 长度）                       |
| `skipped_segment_count`  | 因校正后间隔为零或超过 `max_gap_seconds` 而未计入的段数    |
| `total_celsius_seconds`  | 各段积分合计（℃·秒）                                       |
| `segments[]`             | 计入积分的逐段明细                                         |

`segments[]` 中每段包含：`start_seq` / `end_seq`（端点事件序号）、
`start_time` / `end_time`（**校正后**的 UTC 时间）、`gap_seconds`（校正后段时长）、
`start_temperature` / `end_temperature`、`celsius_seconds`（该段积分）。

```bash
# 先上报一组事件（UTC）：-5℃ -> 1℃ -> 3℃ -> -1℃，每分钟一条
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"box_code":"BOX-HEAT","seq":0,"temperature":-5.0,
       "event_time":"2026-09-23T02:00:00+00:00"}'
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"box_code":"BOX-HEAT","seq":1,"temperature":1.0,
       "event_time":"2026-09-23T02:01:00+00:00"}'
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"box_code":"BOX-HEAT","seq":2,"temperature":3.0,
       "event_time":"2026-09-23T02:02:00+00:00"}'
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"box_code":"BOX-HEAT","seq":3,"temperature":-1.0,
       "event_time":"2026-09-23T02:03:00+00:00"}'

# 查询 0..3 区间、上限 -2℃、最大间隔 120 秒的超温积分
curl 'http://localhost:8000/boxes/BOX-HEAT/heat-exposure?seq_from=0&seq_to=3&upper_limit=-2&max_gap_seconds=120'
```

响应（段 0→1 线性穿越上限，超出部分为三角形 45；段 1→2、2→3 全程超限，梯形 240、180）：

```json
{
  "box_code": "BOX-HEAT",
  "seq_from": 0,
  "seq_to": 3,
  "upper_limit": -2.0,
  "max_gap_seconds": 120.0,
  "event_count": 4,
  "segment_count": 3,
  "skipped_segment_count": 0,
  "total_celsius_seconds": 465.0,
  "segments": [
    {
      "start_seq": 0,
      "end_seq": 1,
      "start_time": "2026-09-23T02:00:00",
      "end_time": "2026-09-23T02:01:00",
      "gap_seconds": 60.0,
      "start_temperature": -5.0,
      "end_temperature": 1.0,
      "celsius_seconds": 45.0
    },
    {
      "start_seq": 1,
      "end_seq": 2,
      "start_time": "2026-09-23T02:01:00",
      "end_time": "2026-09-23T02:02:00",
      "gap_seconds": 60.0,
      "start_temperature": 1.0,
      "end_temperature": 3.0,
      "celsius_seconds": 240.0
    },
    {
      "start_seq": 2,
      "end_seq": 3,
      "start_time": "2026-09-23T02:02:00",
      "end_time": "2026-09-23T02:03:00",
      "gap_seconds": 60.0,
      "start_temperature": 3.0,
      "end_temperature": -1.0,
      "celsius_seconds": 180.0
    }
  ]
}
```

```bash
# 箱号存在但区间内无事件 -> 200，零值 + 空明细
curl 'http://localhost:8000/boxes/BOX-HEAT/heat-exposure?seq_from=100&seq_to=200&upper_limit=-2&max_gap_seconds=120'

# 箱号不存在 -> 404
curl -i 'http://localhost:8000/boxes/NO-SUCH-BOX/heat-exposure?seq_from=0&seq_to=1&upper_limit=-2&max_gap_seconds=120'

# 参数非法（seq_from > seq_to、max_gap_seconds <= 0 等）-> 422
curl -i 'http://localhost:8000/boxes/BOX-HEAT/heat-exposure?seq_from=5&seq_to=2&upper_limit=-2&max_gap_seconds=120'
curl -i 'http://localhost:8000/boxes/BOX-HEAT/heat-exposure?seq_from=0&seq_to=3&upper_limit=-2&max_gap_seconds=0'
```

## 时间加权温度中位数（temperature-median）

`GET /boxes/{box_code}/temperature-median`（默认访问端口 **8000**，
如 `http://localhost:8000/boxes/BOX-MED/temperature-median`）返回指定序号区间内
**按持续时间加权的温度中位数**与有效总秒数：

- 按 `seq` 升序取闭区间 `[seq_from, seq_to]` 内事件，事件时间沿用 clock-audit 的
  **等权保序回归校正**（只读即时计算，不改原始记录；乱序补报后重查即自动重算）。
- 相邻两条事件（**缺号不补点**，序号可以跳号）之间温度视为**随时间线性变化**，
  把每个有效段的**每一瞬间按其持续时间**计入温度分布：
  - 两端温度不同的段，时长在该温度区间上**均匀分布**；
  - **恒温段**的整段时长作为该温度上的**点质量**（离散原子）。
- 仅纳入校正后时长**大于 0 且不超过 `max_gap_seconds`** 的段；间隔为零
  （同一合并块）或超过上限的段不计入（以 `skipped_segment_count` 计数）。
- `median_temperature` 取**累计时长占比首次达到一半（≥ 1/2）的最低温度**
  （即分布函数的下侧广义逆；并列时取低温侧）。全程用精确有理数计算，
  半总时长边界上的并列不会因浮点误差选错点。
- **没有任何有效段**（区间无事件、只有一条事件、或全部段都被过滤）时
  `median_temperature = null`、`total_seconds = 0.0`。
- 箱号不存在返回 `404`；参数非法返回 `422`。

查询参数：

- `seq_from` / `seq_to`：序号区间，**均为整数且含端点**，必须 `>= 0` 且 `seq_from <= seq_to`。
- `max_gap_seconds`：允许计入的最大相邻校正间隔（秒），**必须为正数**（有限数值）。

返回字段：

| 字段                      | 说明                                                       |
| ------------------------- | ---------------------------------------------------------- |
| `event_count`             | 区间内事件条数                                             |
| `segment_count`           | 计入分布的有效段数（校正后时长 > 0 且 ≤ `max_gap_seconds`） |
| `skipped_segment_count`   | 因校正后间隔为零或超过 `max_gap_seconds` 而未计入的段数    |
| `total_seconds`           | 各有效段校正后时长合计（秒）；无有效段为 `0.0`             |
| `median_temperature`      | 累计占比首次达到一半的最低温度（℃）；无有效段为 `null`     |

```bash
# 先上报：seq=0/1 设备时钟回拨（00:10、00:00，校正后同为 00:05 -> 该段零间隔，不计入）；
# seq=2 在 00:01:40 为 0℃；seq=3 在 00:03:40 为 10℃（缺号不补点，直接与 seq=5 相邻）；
# seq=5 在 00:05:40 恒温 10℃
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"box_code":"BOX-MED","seq":0,"temperature":0.0,"event_time":"2026-09-24T00:00:10+00:00"}'
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"box_code":"BOX-MED","seq":1,"temperature":4.0,"event_time":"2026-09-24T00:00:00+00:00"}'
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"box_code":"BOX-MED","seq":2,"temperature":0.0,"event_time":"2026-09-24T00:01:40+00:00"}'
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"box_code":"BOX-MED","seq":3,"temperature":10.0,"event_time":"2026-09-24T00:03:40+00:00"}'
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"box_code":"BOX-MED","seq":5,"temperature":10.0,"event_time":"2026-09-24T00:05:40+00:00"}'

# 查询 0..5、最大间隔 120 秒
curl 'http://localhost:8000/boxes/BOX-MED/temperature-median?seq_from=0&seq_to=5&max_gap_seconds=120'
```

响应（seq=0、1 校正后同刻，段 0→1 零间隔跳过；有效段为 95 秒 4→0、
120 秒 0→10、120 秒恒温 10℃，合计 335 秒；累计一半落在升温段内的 6.0417℃）：

```json
{
  "box_code": "BOX-MED",
  "seq_from": 0,
  "seq_to": 5,
  "max_gap_seconds": 120.0,
  "event_count": 5,
  "segment_count": 3,
  "skipped_segment_count": 1,
  "total_seconds": 335.0,
  "median_temperature": 6.041666666666667
}
```

恒温段作为点质量的简化示例（100 秒恒温 0℃ + 200 秒线性 0→10℃，共 300 秒，
一半即 150 秒：0℃ 点质量占 100 秒，剩余 50 秒在升温段内，对应 2.5℃）：

```bash
# 无有效段（区间无事件 / 只有一条事件 / 段全被过滤）-> median 为 null、总秒数为 0
curl 'http://localhost:8000/boxes/BOX-MED/temperature-median?seq_from=100&seq_to=200&max_gap_seconds=120'
# {"event_count":0,"segment_count":0,"skipped_segment_count":0,
#  "total_seconds":0.0,"median_temperature":null, ...}

# 间隔恰好等于 max_gap_seconds 计入；调小到 94 秒后所有段被过滤 -> null / 0
curl 'http://localhost:8000/boxes/BOX-MED/temperature-median?seq_from=0&seq_to=5&max_gap_seconds=94'

# 箱号不存在 -> 404
curl -i 'http://localhost:8000/boxes/NO-SUCH-BOX/temperature-median?seq_from=0&seq_to=1&max_gap_seconds=120'

# 参数非法（seq_from > seq_to、max_gap_seconds <= 0、NaN/Infinity、缺参数等）-> 422
curl -i 'http://localhost:8000/boxes/BOX-MED/temperature-median?seq_from=5&seq_to=2&max_gap_seconds=120'
curl -i 'http://localhost:8000/boxes/BOX-MED/temperature-median?seq_from=0&seq_to=3&max_gap_seconds=0'
```

## 温度轨迹压缩预览（trajectory-compression）

`GET /boxes/{box_code}/trajectory-compression`（默认访问端口 **8000**，
如 `http://localhost:8000/boxes/BOX-CMP/trajectory-compression`）对指定序号区间内的
温度轨迹做**有损压缩预览**：以**序号为横坐标**，在保留点之间按温度**线性插值**
重建被省略的事件，**预览结果只在查询时即时计算，不写回任何原始记录**。

- 按 `seq` 升序取闭区间 `[seq_from, seq_to]` 内事件（缺号不补点，只读库中实际事件），
  **单次至多 120 条**，区间内超过 120 条返回 `422`。
- 给定非负且有限的允许最大重建误差 `max_error`（℃），寻找**保留事件数最少**的子序列：
  **首末事件必须保留**；每个被省略事件的重建温度与其原值之差不得超过 `max_error`。
- 保留数量相同时，选择**保留序号序列字典序最小**的方案
  （即更早的事件优先被保留；用 DAG 最短路 DP 精确求解，而非简单贪心跳最远）。
- 区间内**只有一条事件时直接保留**；箱号存在但**区间内无事件返回空结果**
  （`kept_events=[]`、`actual_max_error=null`）；箱号不存在返回 `404`。

查询参数：

- `seq_from` / `seq_to`：序号区间，**均为整数且含端点**，必须 `>= 0` 且 `seq_from <= seq_to`。
- `max_error`：允许的最大重建误差（℃），**非负、有限**数值（传负数、`NaN`、`Infinity` 均为 `422`）。

返回字段：

| 字段                | 说明                                                          |
| ------------------- | ------------------------------------------------------------- |
| `event_count`       | 区间内事件条数                                                |
| `kept_count`        | 保留事件数（满足误差约束的最少值，同数量取字典序最小方案）    |
| `omitted_count`     | 被省略事件数（`event_count - kept_count`）                    |
| `actual_max_error`  | 被省略事件重建温度与原值之差的最大绝对值（℃）；无省略点为 `0.0`，区间无事件为 `null` |
| `kept_events[]`     | 保留事件的原始 `seq` / `event_time` / `temperature`（按序号升序） |

```bash
# 先上报 8 条事件（UTC，每分钟一条）：前 5 条在 -5℃ 附近小幅波动，随后升温
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"box_code":"BOX-CMP","seq":0,"temperature":-5.0,"event_time":"2026-09-23T02:00:00+00:00"}'
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"box_code":"BOX-CMP","seq":1,"temperature":-4.8,"event_time":"2026-09-23T02:01:00+00:00"}'
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"box_code":"BOX-CMP","seq":2,"temperature":-5.2,"event_time":"2026-09-23T02:02:00+00:00"}'
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"box_code":"BOX-CMP","seq":3,"temperature":-4.9,"event_time":"2026-09-23T02:03:00+00:00"}'
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"box_code":"BOX-CMP","seq":4,"temperature":-5.0,"event_time":"2026-09-23T02:04:00+00:00"}'
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"box_code":"BOX-CMP","seq":5,"temperature":-3.0,"event_time":"2026-09-23T02:05:00+00:00"}'
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"box_code":"BOX-CMP","seq":6,"temperature":-2.6,"event_time":"2026-09-23T02:06:00+00:00"}'
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"box_code":"BOX-CMP","seq":7,"temperature":-2.0,"event_time":"2026-09-23T02:07:00+00:00"}'

# 以 0.3℃ 的最大重建误差预览 0..7 的压缩结果
curl 'http://localhost:8000/boxes/BOX-CMP/trajectory-compression?seq_from=0&seq_to=7&max_error=0.3'
```

响应（seq 1/2/3 由弦 (0,4) 重建，最大偏差 0.2℃；seq 6 由弦 (5,7) 重建，偏差 0.1℃；
seq 5 温度跳变必须保留，共省略 4 条）：

```json
{
  "box_code": "BOX-CMP",
  "seq_from": 0,
  "seq_to": 7,
  "max_error": 0.3,
  "event_count": 8,
  "kept_count": 4,
  "omitted_count": 4,
  "actual_max_error": 0.20000000000000018,
  "kept_events": [
    {"seq": 0, "event_time": "2026-09-23T02:00:00", "temperature": -5.0},
    {"seq": 4, "event_time": "2026-09-23T02:04:00", "temperature": -5.0},
    {"seq": 5, "event_time": "2026-09-23T02:05:00", "temperature": -3.0},
    {"seq": 7, "event_time": "2026-09-23T02:07:00", "temperature": -2.0}
  ]
}
```

同数量方案的字典序 tie-break 示例（温度 `-5,-4,-4,-5`，`max_error=0.5`）：
保留 `{0,1,3}`（省略 seq=2，弦 (1,3) 在 seq=2 重建为 -4.5，误差恰为 0.5）
与保留 `{0,2,3}`（省略 seq=1，误差同样为 0.5）都是 3 个保留点的可行最优方案，
按序号序列字典序最小选择前者：

```bash
# 仅一条事件 -> 直接保留，actual_max_error=0.0
curl 'http://localhost:8000/boxes/BOX-CMP/trajectory-compression?seq_from=2&seq_to=2&max_error=0.3'

# 箱号存在但区间内无事件 -> 200，空结果（actual_max_error=null）
curl 'http://localhost:8000/boxes/BOX-CMP/trajectory-compression?seq_from=100&seq_to=200&max_error=0.3'

# 箱号不存在 -> 404
curl -i 'http://localhost:8000/boxes/NO-SUCH-BOX/trajectory-compression?seq_from=0&seq_to=1&max_error=0.3'

# 区间内超过 120 条 -> 422
curl -i 'http://localhost:8000/boxes/BOX-CMP/trajectory-compression?seq_from=0&seq_to=1000&max_error=0.3'

# 参数非法（seq_from > seq_to、负误差、NaN/Infinity 等）-> 422
curl -i 'http://localhost:8000/boxes/BOX-CMP/trajectory-compression?seq_from=5&seq_to=2&max_error=0.3'
curl -i 'http://localhost:8000/boxes/BOX-CMP/trajectory-compression?seq_from=0&seq_to=3&max_error=-0.1'
```

## 事件区间包含证明（merkle-proof）

`GET /boxes/{box_code}/merkle-proof`（默认访问端口 **8000**，
如 `http://localhost:8000/boxes/BOX-MK/merkle-proof`）对指定序号区间内的实际已存事件
构造 **Merkle 树**，并返回目标事件的**包含证明**（inclusion proof），供调用方离线重算根哈希、
核对事件未被篡改。**只在查询时即时计算，不补缺号、不写回任何记录**。

建树规则（叶子顺序即查询顺序：按 `seq` 升序的实际事件，缺号不补）：

- **叶子原文**：由箱号、序号、UTC 时间字符串（`YYYY-MM-DDTHH:MM:SS.ffffffZ`）和
  `.17g` 格式的温度字符串组成的**无空格 JSON 数组**，中文等非 ASCII 字符**原样保留**
  （不按 `\uXXXX` 转义），按 UTF-8 编码。例如
  `["BOX-MK",2,"2026-09-23T02:02:30.123456Z","-4.125"]`。
- **叶哈希**：`SHA-256(0x00 || 叶子原文的 UTF-8 字节)`。
- **父节点哈希**：`SHA-256(0x01 || 左哈希 || 右哈希)`；某层节点数为奇数时
  **复制末节点**再两两配对。
- **单事件**时叶即根，证明路径为空数组。

查询参数：

- `seq_from` / `seq_to`：序号区间，**均为整数且含端点**，必须 `>= 0` 且 `seq_from <= seq_to`。
- `target_seq`：目标事件序号，**须为区间内实际存在的事件**（缺号不算）。

返回字段：

| 字段                | 说明                                                          |
| ------------------- | ------------------------------------------------------------- |
| `event_count`       | 区间内实际事件条数（即叶子数，缺号不补）                      |
| `leaf_data`         | 目标叶子原文（无空格 JSON 数组，可直接哈希核对）              |
| `target_leaf_hash`  | 目标叶哈希（十六进制）                                        |
| `root_hash`         | Merkle 根哈希（十六进制）                                     |
| `proof[]`           | 从叶层到根层逐层的 `sibling_hash` 与 `sibling_side`（`left`/`right` 表示兄弟在当前节点的左/右）；单事件时为空 |

错误情况：箱号不存在、目标序号不在区间实际事件中（含区间为空）返回 `404`；
缺少参数、参数非整数、负数或 `seq_from > seq_to` 返回 `422`。

```bash
# 先上报 5 条事件（UTC；注意跳过 seq=3，缺号不补）
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"box_code":"BOX-MK","seq":0,"temperature":-5.0,"event_time":"2026-09-23T02:00:00+00:00"}'
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"box_code":"BOX-MK","seq":1,"temperature":-4.5,"event_time":"2026-09-23T02:01:00+00:00"}'
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"box_code":"BOX-MK","seq":2,"temperature":-4.125,"event_time":"2026-09-23T02:02:30.123456+00:00"}'
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"box_code":"BOX-MK","seq":4,"temperature":0.1,"event_time":"2026-09-23T02:04:00+00:00"}'
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"box_code":"BOX-MK","seq":5,"temperature":-3.0,"event_time":"2026-09-23T02:05:00+00:00"}'

# 取 seq=2 在区间 [0,5] 上的包含证明（5 个叶子，奇数层复制末节点）
curl 'http://localhost:8000/boxes/BOX-MK/merkle-proof?seq_from=0&seq_to=5&target_seq=2'
```

响应：

```json
{
  "box_code": "BOX-MK",
  "seq_from": 0,
  "seq_to": 5,
  "target_seq": 2,
  "event_count": 5,
  "leaf_data": "[\"BOX-MK\",2,\"2026-09-23T02:02:30.123456Z\",\"-4.125\"]",
  "target_leaf_hash": "fd39e4b65b44f6b44dd7d5fa1f5bacf7201db3eb19a637d7d62571ce41f33aaf",
  "root_hash": "9129c6bc27889ec02bec4ce2e390850dbcf21d2dc80b315538dbc3d0e6aa7c8a",
  "proof": [
    {"sibling_hash": "52b0998366034c294b23a9b92b03b57ae42a83ff949927d888de653b07178a25", "sibling_side": "right"},
    {"sibling_hash": "3f375bfaa74beb9431f4b8369abe3ed72b3e80800874b4254cd753b66acfdafd", "sibling_side": "left"},
    {"sibling_hash": "e58746fe5459714a8852f6e302a845c4252d40d3be8d17e8c715d98728be18da", "sibling_side": "right"}
  ]
}
```

### 验证示例（调用方重算根）

把上面响应保存为 `proof.json`，用以下脚本（仅标准库）重算叶哈希与根哈希并比对：

```python
import hashlib, json

resp = json.load(open("proof.json"))

# 1) 由 leaf_data 重算目标叶哈希：SHA-256(0x00 || UTF-8 字节)
leaf = hashlib.sha256(b"\x00" + resp["leaf_data"].encode("utf-8")).hexdigest()
assert leaf == resp["target_leaf_hash"], "叶哈希不匹配"

# 2) 沿证明路径逐层重算：兄弟在左则 H(0x01||兄弟||当前)，在右则 H(0x01||当前||兄弟)
cur = bytes.fromhex(resp["target_leaf_hash"])
for step in resp["proof"]:
    sib = bytes.fromhex(step["sibling_hash"])
    pair = (sib + cur) if step["sibling_side"] == "left" else (cur + sib)
    cur = hashlib.sha256(b"\x01" + pair).digest()
assert cur.hex() == resp["root_hash"], "根哈希不匹配"
print("验证通过：根哈希 =", resp["root_hash"])
```

```bash
curl -s 'http://localhost:8000/boxes/BOX-MK/merkle-proof?seq_from=0&seq_to=5&target_seq=2' > proof.json
python3 verify_proof.py
# 验证通过：根哈希 = 9129c6bc27889ec02bec4ce2e390850dbcf21d2dc80b315538dbc3d0e6aa7c8a
```

```bash
# 单事件 -> 证明路径为空，根哈希即叶哈希
curl 'http://localhost:8000/boxes/BOX-MK/merkle-proof?seq_from=2&seq_to=2&target_seq=2'

# 目标序号不在区间实际事件中（缺号 seq=3 / 超出区间 / 区间无事件）-> 404
curl -i 'http://localhost:8000/boxes/BOX-MK/merkle-proof?seq_from=0&seq_to=5&target_seq=3'

# 箱号不存在 -> 404
curl -i 'http://localhost:8000/boxes/NO-SUCH-BOX/merkle-proof?seq_from=0&seq_to=1&target_seq=0'

# 范围非法（seq_from > seq_to、负数、缺参数等）-> 422
curl -i 'http://localhost:8000/boxes/BOX-MK/merkle-proof?seq_from=5&seq_to=2&target_seq=3'
```

## 设计说明

- **去重键**：数据库对 `(box_code, seq)` 建唯一约束（`uq_box_seq`）。
  应用层先查后插，且捕获并发下的 `IntegrityError` 重新判定，
  因此设备并发重试也不会产生重复行或错误覆盖。
- **内容相等**：比较 `temperature` 与已归一化为 UTC 的 `event_time`。
  重试通常原样重发 JSON，浮点解析结果一致，可直接判等。
- **乱序安全**：最新状态通过“每箱 `MAX(seq)` 关联回原行”的子查询得到，
  不依赖插入先后，也不要求序号连续。
- **时钟校正只读**：`clock-audit` 在查询时即时计算，不写库、不改动原始事件；
  乱序补报后重查即自动重算。时间先精确换算为整数微秒（整数运算，无浮点误差），
  再用 PAVA 做等权保序回归：相邻块出现“前块均值 > 后块均值”时向前合并、
  块内取算术均值并四舍五入到微秒，天然得到非递减的建议序列，
  且最小化各事件偏移的平方和。
- **超温积分只读**：`heat-exposure` 复用同一套保序回归校正时间，把相邻事件间的
  温度视为线性变化，对超过 `upper_limit` 的部分解析求积（梯形/三角形公式），
  逐段积分用 `math.fsum` 精确求和；校正后间隔为零或超过 `max_gap_seconds` 的段
  直接跳过，缺号不补点。同样不写库，补报后重查即重算。
- **时间加权中位数只读**：`temperature-median` 复用区间事件读取与同一套保序校正
  时间，相邻事件间温度线性变化、每一瞬间按持续时间计入温度分布。变温段时长在其
  温度区间上均匀（密度 = 段时长 / 温度跨度），恒温段时长是该温度的点质量；
  实现上汇总各温度断点的点质量与密度净变化后从低到高扫描，累计时长首次达到
  总时长一半时的最低温度即中位数。温度与微秒时长全程用 `Fraction` 精确计算，
  占半并列时稳定落在低温侧；校正后间隔为零或超过 `max_gap_seconds` 的段不计入，
  缺号不补点。查询不写库，乱序补报后重查即自动重算。
- **轨迹压缩只读**：`trajectory-compression` 复用区间事件查询（按 `seq` 升序、
  缺号不补点），以序号为横坐标做最少保留点压缩。段 `(i,j)` 可行仅取决于两端点，
  因此问题等价于在“可行段”构成的 DAG 上求 0→n-1 的最短路径：用 DP 精确求解
  保留数最少、同数量时保留序号字典序最小的方案（贪心跳最远无法保证字典序最小）。
  误差比较用 `Fraction` 精确计算（float 温度与整数序号均可无损转为有理数），
  避免浮点误差在 `max_error` 边界上误判；单次限 120 条，O(n³) 开销可控。
  预览不写库，乱序补报后重查即自动重算。
- **包含证明只读**：`merkle-proof` 复用区间事件查询（按 `seq` 升序、缺号不补点），
  以实际事件为叶子即时建树：叶子是箱号、序号、`YYYY-MM-DDTHH:MM:SS.ffffffZ` UTC
  时间字符串与 `.17g` 温度字符串组成的无空格 JSON 数组（中文原样 UTF-8 编码），
  叶哈希 `SHA-256(0x00||数据)`、父哈希 `SHA-256(0x01||左||右)`、奇数层复制末节点；
  证明路径逐层给出兄弟哈希与左右方位，调用方可离线重算根。不写库，补报后重查即重算。
- **持久化**：默认 SQLite + WAL，卷挂载于 `/app/data`；
  如需 PostgreSQL/MySQL，只需将 `DATABASE_URL` 换成对应 SQLAlchemy 连接串
  （并安装相应驱动），建表逻辑通用。
- **交接快照冻结**：`handoffs`（交接编号唯一）与 `handoffs_items`
  （每箱一行，外键级联）在**同一事务**内完成“读各箱最大序号状态 + 写主表/明细”，
  读到的就是该事务时点的状态；缺报箱三列状态字段为 NULL、结论 `missing`，
  越界按严格越出闭区间判定（边界值正常）。快照表此后不再被任何事件写路径触碰，
  因此乱序/延迟补报只会改变实时最新状态，**天然无法改写已建快照**。
  同编号重放按“上下限相同且箱号集合相同”判为内容一致（箱号顺序无关），
  命中即返回原快照；内容冲突返回 409。`handoff_no` 的唯一约束同时兜住并发创建：
  捕获 `IntegrityError` 后以库中快照为准重新判定（一致→幂等返回，不一致→409）。
