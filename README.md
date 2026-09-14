# 预拌砂浆含水修正 API

雨后砂堆含水变化时，按干配方直接投料会同时偏离骨料量与实际加水量。本服务在开机前根据
各骨料的含水率/吸水率生成**可复算的修正单**：逐项湿投料量、自由水量，以及修正后的最终加水量。

此外提供**取样批次模块**：实验员把烘干法原始称量（湿样/干样质量）直接以批次留痕，
而不是先在表外算出百分率再录入修正单。批次创建为“待确认”，确认时才计算各组含水率与
中位数代表值并转为“已确认”，以标准库 **SQLite** 文件持久化（无独立数据库服务）。

纯后端 API；Python 3.12 + FastAPI + Pydantic + Decimal（SQLite 用标准库 sqlite3）。

## 计算契约（固定公式）

对每种骨料：

```
湿投料量 = 干基目标质量 × (1 + 含水率/100)
自由水量 = 干基目标质量 × (含水率 − 吸水率)/100
```

批次汇总：

```
最终加水量 = 设计加水量 − 各项自由水量之和
```

- 所有中间值以 `Decimal` 保持完整精度（计算上下文精度随输入位数与量级自适应，
  巨大设计加水量减去微小自由水量也不会丢失小数），不做任何舍入；
- 响应中的逐项质量与总量统一按 **ROUND_HALF_UP 保留三位小数**；
- 合计值由**未舍入的中间值**求和后再舍入（可能与逐项展示值之和有 0.001 级差）；
- 自由水量可为负（吸水率高于含水率时骨料反而吸水，最终加水量因此上调）；
- 最终加水量为 **0 合法**；**小于 0 则以 422 整体拒绝，不返回部分修正单**（判定基于完整精度）。

### 可选：目标干料总量同比缩放

请求传入可选的 `target_dry_total_kg` 时，以原骨料干基合计为基准求唯一缩放系数，
同比缩放各项干基质量与设计加水量，缩放值保持完整精度进入上述固定公式：

```
缩放系数 = 目标干料总量 / 原骨料干基合计
各项干基质量′ = 各项干基质量 × 缩放系数
设计加水量′  = 设计加水量 × 缩放系数
```

响应补充 `target_dry_total_kg`（三位小数）与 `scale_factor`
（**ROUND_HALF_UP 保留六位小数**）；未传该参数时响应不含这两个字段，计算与原样完全一致。
缩放后的最终加水量小于零时同样以 422 整单拒绝。

## 输入约束（端点均包含）

| 字段 | 约束 |
| --- | --- |
| `design_water_kg` | 质量（kg），> 0 |
| `target_dry_total_kg` | 可选；传入时为目标干料总量（kg），> 0 且量级在 1e-28 ~ 1e28 之间（超出可计算范围以 422 拒绝）；显式提交 `null` 视为非法输入，只有省略该字段才按不缩放处理 |
| `aggregates` | 数组，1 ~ 8 种骨料 |
| `aggregates[i].name` | 非空字符串，≤ 64 字符 |
| `aggregates[i].dry_mass_kg` | 干基目标质量（kg），> 0 |
| `aggregates[i].moisture_pct` | 含水率，质量百分数，0 ~ 40 |
| `aggregates[i].absorption_pct` | 吸水率，质量百分数，0 ~ 15 |

小数位数不限：高精度输入（如 `moisture_pct: "5.1234567890123"`）一律受理，
仅按上表有效范围校验；超出范围（如 `40.0000001`）才返回 422。
无法识别的请求字段（如误写的 `target_dry_total_kgg`）一律以 422 拒绝，
不静默忽略。

## 端点

### `POST /api/v1/correction-sheet`

请求示例：

```json
{
  "design_water_kg": "180",
  "aggregates": [
    {"name": "河砂A", "dry_mass_kg": "800", "moisture_pct": "5.0", "absorption_pct": "1.0"},
    {"name": "机制砂B", "dry_mass_kg": "600", "moisture_pct": "3.5", "absorption_pct": "0.5"},
    {"name": "石粉", "dry_mass_kg": "200", "moisture_pct": "0.5", "absorption_pct": "0.2"}
  ]
}
```

`200 OK` 响应（质量均为三位小数字符串，保留尾随零以避免浮点误差）：

```json
{
  "items": [
    {"index": 0, "name": "河砂A", "dry_mass_kg": "800.000", "wet_mass_kg": "840.000", "free_water_kg": "32.000"},
    {"index": 1, "name": "机制砂B", "dry_mass_kg": "600.000", "wet_mass_kg": "621.000", "free_water_kg": "18.000"},
    {"index": 2, "name": "石粉", "dry_mass_kg": "200.000", "wet_mass_kg": "201.000", "free_water_kg": "0.600"}
  ],
  "item_count": 3,
  "total_dry_mass_kg": "1600.000",
  "total_wet_mass_kg": "1662.000",
  "total_free_water_kg": "50.600",
  "design_water_kg": "180.000",
  "final_water_kg": "129.400"
}
```

传入 `target_dry_total_kg` 的缩放请求示例：

```json
{
  "design_water_kg": "180",
  "target_dry_total_kg": "3200",
  "aggregates": [
    {"name": "河砂A", "dry_mass_kg": "800", "moisture_pct": "5.0", "absorption_pct": "1.0"},
    {"name": "机制砂B", "dry_mass_kg": "600", "moisture_pct": "3.5", "absorption_pct": "0.5"},
    {"name": "石粉", "dry_mass_kg": "200", "moisture_pct": "0.5", "absorption_pct": "0.2"}
  ]
}
```

对应响应在原字段基础上补充目标与缩放系数（全单同比放大 2 倍）：

```json
{
  "items": [
    {"index": 0, "name": "河砂A", "dry_mass_kg": "1600.000", "wet_mass_kg": "1680.000", "free_water_kg": "64.000"},
    {"index": 1, "name": "机制砂B", "dry_mass_kg": "1200.000", "wet_mass_kg": "1242.000", "free_water_kg": "36.000"},
    {"index": 2, "name": "石粉", "dry_mass_kg": "400.000", "wet_mass_kg": "402.000", "free_water_kg": "1.200"}
  ],
  "item_count": 3,
  "total_dry_mass_kg": "3200.000",
  "total_wet_mass_kg": "3324.000",
  "total_free_water_kg": "101.200",
  "design_water_kg": "360.000",
  "final_water_kg": "258.800",
  "target_dry_total_kg": "3200.000",
  "scale_factor": "2.000000"
}
```

### `GET /health`

返回 `{"status": "ok"}`，用于容器健康检查。

## 取样批次（烘干法原始称量留痕）

实验员把原始称量直接成批留档，创建时不做任何百分率换算：

```
组含水率（%）=（湿样质量 − 干样质量）÷ 干样质量 × 100
代表含水率   = 各组含水率的中位数（偶数个组取中间两项的算术平均）
```

- 每批 **2 ~ 5 组**称量；湿样/干样质量均须 **> 0**，且**干样严格小于湿样**
  （相等即零含水率，视为称量或录入错误），错误定位 `readings[i].dry_sample_mass`；
- 无法识别的字段一律 422 拒绝；
- 各组结果保持**完整精度**，仅代表含水率在确认出口按 **ROUND_HALF_UP 保留三位**；
  计算上下文精度随输入的量级、小数跨度与有效位数**自适应**（下限 50 位）——
  超长有效数字称量（如真实中位数 `1.2345 − 1e-52`）不会因固定精度截断越过
  舍入边界而被错误进位（`1.234` → `1.235`）；
- 批次主行与全部原始读数在**同一事务**内写入，失败整体回滚；
- 状态只有“待确认 → 已确认”，**已确认数据不可改动**（重复确认返回 409）。

### `POST /api/v1/moisture-batches`

创建批次，返回 `201` 与批次编号（`MC` + 日期 + 随机段），状态“待确认”：

```json
{
  "pile_name": "雨后1号砂堆",
  "readings": [
    {"wet_sample_mass": "500.12", "dry_sample_mass": "477.72"},
    {"wet_sample_mass": "480.00", "dry_sample_mass": "458.50"},
    {"wet_sample_mass": "512.345", "dry_sample_mass": "480.005"},
    {"wet_sample_mass": "210.00", "dry_sample_mass": "200.00"}
  ]
}
```

响应（待确认时所有含水率字段为 `null`，原始读数原样回显）：

```json
{
  "batch_no": "MC20260914-7CCE406E",
  "pile_name": "雨后1号砂堆",
  "status": "待确认",
  "readings": [
    {"index": 0, "wet_sample_mass": "500.12", "dry_sample_mass": "477.72", "moisture_pct": null},
    {"index": 1, "wet_sample_mass": "480.00", "dry_sample_mass": "458.50", "moisture_pct": null},
    {"index": 2, "wet_sample_mass": "512.345", "dry_sample_mass": "480.005", "moisture_pct": null},
    {"index": 3, "wet_sample_mass": "210.00", "dry_sample_mass": "200.00", "moisture_pct": null}
  ],
  "representative_moisture_pct": null,
  "created_at": "2026-09-14T02:31:05.123456+00:00",
  "confirmed_at": null
}
```

### `POST /api/v1/moisture-batches/{batch_no}/confirm`

确认批次：逐组完整精度计算，中位数三位 HALF_UP 形成代表值并置“已确认”：

```json
{
  "batch_no": "MC20260914-7CCE406E",
  "pile_name": "雨后1号砂堆",
  "status": "已确认",
  "readings": [
    {"index": 0, "wet_sample_mass": "500.12", "dry_sample_mass": "477.72",
     "moisture_pct": "4.6889391275223980574395043121493762036339278238299"},
    {"index": 1, "wet_sample_mass": "480.00", "dry_sample_mass": "458.50",
     "moisture_pct": "4.6892039258451472191930207197382769901853871319520"},
    {"index": 2, "wet_sample_mass": "512.345", "dry_sample_mass": "480.005",
     "moisture_pct": "6.7374298184393912563410797804189539692294871928417"},
    {"index": 3, "wet_sample_mass": "210.00", "dry_sample_mass": "200.00",
     "moisture_pct": "5.00"}
  ],
  "representative_moisture_pct": "4.845",
  "created_at": "2026-09-14T02:31:05.123456+00:00",
  "confirmed_at": "2026-09-14T02:32:10.987654+00:00"
}
```

结构化错误（同样沿用 `detail` 数组）：

- 编号不存在 → `404`：`{"detail": [{"field": "batch_no", "type": "batch_not_found", ...}]}`
- 重复确认 → `409`：`{"detail": [{"field": "batch_no", "type": "batch_already_confirmed", ...}]}`，结果不变
- 非法称量 → `422`，定位到读数下标，如 `readings[1].dry_sample_mass`；**非法称量不落库**

持久化：SQLite 文件路径由 `SAMPLING_DB_PATH` 控制（默认 `data/moisture_batches.db`）。
仓储可以随时销毁并按同一文件重建，待确认/已确认批次与结果都能重新读出。

## 错误反馈（422）

所有错误统一为 `{"detail": [...]}`，每条错误的 `field` **定位到骨料下标**：

```json
{"detail": [{"field": "aggregates[1].moisture_pct", "message": "必须小于或等于 40", "type": "less_than_equal"}]}
```

最终加水量小于零时整单拒绝（响应中不含 `items` 等任何部分修正单）：

```json
{"detail": [{"field": "final_water_kg", "message": "最终加水量 -0.600 kg 小于零（设计加水量 50.000 kg，自由水量合计 50.600 kg），整单拒绝", "type": "negative_final_water"}]}
```

## 模块结构

```
app/
├── schemas.py              # 修正单请求校验：Pydantic 契约（范围、质量>0、1~8 种骨料、可选目标干料总量）
├── calculator.py           # 修正计算：纯 Decimal 公式，可选同比缩放，中间值完整精度
├── summary.py              # 批次汇总：ROUND_HALF_UP 三位小数（缩放系数六位）舍入与响应组装
├── sampling_schemas.py     # 取样批次契约：2~5 组湿样/干样称量，干样须严格小于湿样，extra=forbid
├── sampling_calculator.py  # 取样计算：（湿样−干样）/干样×100 各组结果与中位数（完整精度）
├── sampling_repository.py  # SQLite 仓储：整批事务写入、条件确认、404/409、重建可读
├── sampling_service.py     # 取样编排：创建（待确认）/确认（计算并置已确认）/响应组装
└── main.py                 # FastAPI 装配：修正单端点 + 取样批次创建/确认端点与统一错误反馈
tests/                      # pytest：修正单既有覆盖 + 取样模块计算/仓储/API 覆盖
verify.py                   # 一次性验收：修正单样例 + 取样批次创建/非法称量/确认/409/404/重建仓储
```

## 本地运行

```bash
pip install -r requirements-dev.txt
pytest                                   # 运行测试
uvicorn app.main:app --port 8000         # 启动 API
python verify.py                         # 对 localhost:8000 做一次性验收
# 或指定地址：API_BASE_URL=http://localhost:8123 python verify.py
# SQLite 库文件位置（默认 data/moisture_batches.db）：
SAMPLING_DB_PATH=/tmp/moisture_batches.db uvicorn app.main:app --port 8000
```

交互式文档：`http://localhost:8000/docs`。

## Docker Compose（仅运行 API，无数据库服务）

取样批次以 SQLite 文件持久化在命名卷 `moisture-data`（容器内 `/data`）上，
compose 不启动任何独立数据库组件：

```bash
docker compose up api                    # 启动 API，宿主端口默认 8000
API_PORT=9000 docker compose up api      # API_PORT 覆盖宿主端口
docker compose up verify                 # 一次性验收：起 API → 修正单 ×2 → 取样批次全流程后退出
docker compose run --rm verify           # 等价的一次性运行方式
```

`verify` 服务等待 `api` 健康后：提交两份修正单样例独立复算比对；再创建 4 组称量的
取样批次、拒绝一宗非法称量（422 定位 readings 下标）、确认并按完整精度与三位
HALF_UP 中位数独立复算比对、重复确认核对 409、不存在编号核对 404，最后在共享卷的
同一 SQLite 文件上**重建仓储**读取同一批次，全部通过退出码 0，否则退出码 1。
