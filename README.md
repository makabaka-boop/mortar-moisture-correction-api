# 预拌砂浆含水修正 API

雨后砂堆含水变化时，按干配方直接投料会同时偏离骨料量与实际加水量。本服务在开机前根据
各骨料的含水率/吸水率生成**可复算的修正单**：逐项湿投料量、自由水量，以及修正后的最终加水量。

此外提供**取样批次模块**：实验员把烘干法原始称量（湿样/干样质量）直接以批次留痕，
而不是先在表外算出百分率再录入修正单。批次创建为“待确认”，确认时才计算各组含水率与
中位数代表值并转为“已确认”，以标准库 **SQLite** 文件持久化（无独立数据库服务）。

**校准曲线模块**：在线含水传感器更换探头后，实验员把标准样对照数据固化为独立、不可变的
校准曲线（而不是在设备侧保存零散系数）；生产调用方只给曲线编号与单个原始信号，由服务按
相邻两点做 Decimal 线性插值换算含水率。曲线与取样批次保存在同一个 SQLite 文件中。

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
- 待确认批次可按 `{批次编号, 读数下标}` 修订单组称量：成功时在同一事务内
  **原子替换读数、`revision_no` 加一、写入修改前后质量与时间的审计记录**；
- 修订请求携带客户端已见 `revision_no` 做乐观并发控制：新建批次与旧库迁移批次均为
  `0`，旧修订号并发写入返回 409，读数、修订号和审计记录均不变；
- 状态只有“待确认 → 已确认”，**已确认数据不可改动**（重复确认或修订均返回 409）；
  确认始终从库内最新读数计算中位数。

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

### `PATCH /api/v1/moisture-batches/{batch_no}/readings/{index}`

确认前修订一组录错的称量。路径中的 `index` 从 0 开始；请求体携带新湿样、新干样
质量和客户端已见修订号：

```json
{
  "wet_sample_mass": "210.00",
  "dry_sample_mass": "200.00",
  "revision_no": 0
}
```

成功返回更新后的待确认批次；响应在原有批次字段基础上增加 `revision_no`，其值递增为 1，
其他读数不变。库内同时追加
一条审计记录，保存批次编号、读数下标、修改前后湿样/干样质量、旧/新修订号和 UTC
修订时间。创建和确认响应仍保持原字段集合，不额外输出 `revision_no`。确认时从修订后的
最新读数重新计算各组含水率与中位数。

修订请求沿用创建时的质量校验：湿样/干样均为正数，干样必须严格小于湿样，未知字段
拒绝。`revision_no` 只接收 JSON 整数：布尔值、小数（如 `1.0`）与数字字符串
一律以 422 拒绝（`field` 为 `revision_no`），不做隐式类型转换。编号不存在返回
404；`index` 越界（含超出 SQLite 整数范围的超大下标）返回 422 且 `field` 为
`index`；批次已确认或 `revision_no` 已过期返回 409。校验失败与冲突均整体回滚，
不改变读数、修订号，也不追加审计记录。

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
- 读数下标越界 → `422`：`{"detail": [{"field": "index", "type": "reading_index_out_of_bounds", ...}]}`
- 重复确认或修订已确认批次 → `409`：`{"detail": [{"field": "batch_no", "type": "batch_already_confirmed", ...}]}`，结果不变
- 修订号过期 → `409`：`{"detail": [{"field": "revision_no", "type": "revision_conflict", ...}]}`，读数与审计不变
- 非法称量 → `422`，定位到读数下标，如 `readings[1].dry_sample_mass`；修订请求则定位到请求字段（如 `dry_sample_mass`）；**非法称量不落库**

持久化：SQLite 文件路径由 `SAMPLING_DB_PATH` 控制（默认 `data/moisture_batches.db`）。
仓储初始化会自动迁移旧版库：给既有批次补 `revision_no = 0`，并建立修订审计表。
仓储可以随时销毁并按同一文件重建，待确认/已确认批次与结果都能重新读出。

## 校准曲线（换探头后的标准样对照固化）

在线含水传感器更换探头后，实验员把 3 ~ 8 个标准样“原始信号 → 参考含水率”对照点
一次性固化为独立曲线；生产侧只持曲线编号，换算时由服务查曲线，**不在设备侧保存
零散系数**。曲线一经创建即为**不可变记录**：服务没有任何更新/删除/追加入口，库内
触发器拒绝绕过仓储直接 `UPDATE`/`DELETE`，也拒绝向已固化曲线 `INSERT` 追加新对照点
（追加一个点同样会改变换算依据）。实现上主行带 `sealed` 标志：创建事务先以
`sealed=0` 落主行并插入对照点，同事务末尾才翻转为 `1`；触发器只允许向 `sealed=0`
的曲线插点，且只放行这一次 0 → 1 固化更新。旧库（无 `sealed` 列）初始化时既有曲线
一律补 `sealed=1`，迁移后同样不可追加。

```
含水率（中间点）= y1 + (x − x1) × (y2 − y1) ÷ (x2 − x1)   （相邻两点线性插值）
```

- `raw_signals`：有限十进制数，**3 ~ 8 个且严格递增**（重复点或倒序以 422 拒绝，
  定位到 `raw_signals`）；信号单位由传感器约定，小数位数不限，不另设数值范围；
- `reference_moisture_pct`：参考含水率质量百分数，**0 ~ 40（端点包含）**，点数必须
  与原始信号一致且严格递增，错误定位到 `reference_moisture_pct`；
- 无法识别的字段一律 422 拒绝（`extra="forbid"`）；非法点集**不落任何数据**；
- 换算以相邻两点做 **Decimal** 线性插值，**中间值不舍入**，含水率仅在出口按
  **ROUND_HALF_UP 保留三位**；端点输入直接返回该点参考值，并回显命中区间
  （首端点取首段、末端点取末段、内部端点取其右侧相邻区间）的四个端点值；
- 计算上下文精度随输入的量级、小数跨度与有效位数自适应（下限 50 位），紧挨三位
  舍入边界的高精度对照点不会被固定精度截断误进位；
- 曲线不存在 → 结构化 **404**（`type: curve_not_found`）；信号落在曲线范围外 →
  定位到 `raw_signal` 的 **422**（`type: raw_signal_out_of_range`，不做外推）；
- 曲线主行与全部对照点在**同一事务**内写入，失败整体回滚；两表以
  `CREATE TABLE IF NOT EXISTS` 追加到既有 SQLite 文件，取样批次等既有表不受影响。

### `POST /api/v1/moisture-calibration-curves`

创建曲线，返回 `201` 与曲线编号（`CC` + 日期 + 随机段，形如 `CC20260914-7F3A9C21`）：

```json
{
  "sensor_id": "MOIST-SENSOR-A1",
  "raw_signals": ["4.0", "8.0", "12.0", "16.0", "20.0"],
  "reference_moisture_pct": ["0", "5", "10", "20", "40"]
}
```

响应（原始读数原样回显，带创建时间）：

```json
{
  "curve_no": "CC20260914-FE298FEE",
  "sensor_id": "MOIST-SENSOR-A1",
  "points": [
    {"index": 0, "raw_signal": "4.0", "reference_moisture_pct": "0"},
    {"index": 1, "raw_signal": "8.0", "reference_moisture_pct": "5"},
    {"index": 2, "raw_signal": "12.0", "reference_moisture_pct": "10"},
    {"index": 3, "raw_signal": "16.0", "reference_moisture_pct": "20"},
    {"index": 4, "raw_signal": "20.0", "reference_moisture_pct": "40"}
  ],
  "created_at": "2026-09-14T16:10:00.123456+00:00"
}
```

### `POST /api/v1/moisture-calibration-curves/convert`

按曲线把单个原始信号换算为含水率，返回命中区间端点与三位 HALF_UP 含水率：

```json
{"curve_no": "CC20260914-FE298FEE", "raw_signal": "15.1234"}
```

```json
{
  "curve_no": "CC20260914-FE298FEE",
  "raw_signal": "15.1234",
  "interval": {
    "lower_signal": "12.0",
    "upper_signal": "16.0",
    "lower_moisture_pct": "10",
    "upper_moisture_pct": "20"
  },
  "moisture_pct": "17.809"
}
```

端点输入（如 `"8.0"`）直接返回该点参考值 `"5.000"`，不做插值除法；`NaN`/`Infinity`
等非有限信号以 422 拒绝。曲线与取样批次共用同一 SQLite 文件，路径可用
`CALIBRATION_DB_PATH` 独立覆盖，未设置时回退 `SAMPLING_DB_PATH`。

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
├── sampling_repository.py  # SQLite 仓储：整批事务、乐观锁修订/审计、条件确认、旧库迁移、重建可读
├── sampling_service.py     # 取样编排：创建/修订单组读数（待确认）/确认（计算并置已确认）/响应组装
├── calibration_calculator.py  # 校准曲线计算：相邻两点 Decimal 线性插值（中间值不舍入）、端点直返、越界拒绝
├── calibration_schemas.py     # 校准曲线契约：3~8 个严格递增对照点、参考含水率 0~40、extra=forbid
├── calibration_repository.py  # SQLite 仓储：曲线/对照点整批事务、不可变触发器、追加到既有库
├── calibration_service.py     # 校准编排：创建不可变曲线/换算（插值 + 三位 HALF_UP）/响应组装
└── main.py                 # FastAPI 装配：修正单端点 + 取样批次端点 + 校准曲线创建/换算端点与统一错误反馈
tests/                      # pytest：修正单既有覆盖 + 取样模块 + 校准曲线计算/仓储/API 覆盖
verify.py                   # 一次性验收：修正单 + 取样批次全流程 + 校准曲线创建/插值端点/非法不落库/404/422/不可变
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
# 校准曲线默认与取样批次同库；如需独立文件可单独覆盖（未设置时回退 SAMPLING_DB_PATH）：
CALIBRATION_DB_PATH=/tmp/calibration.db uvicorn app.main:app --port 8000
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
HALF_UP 中位数独立复算比对、重复确认核对 409、不存在编号核对 404，在共享卷的
同一 SQLite 文件上**重建仓储**读取同一批次；最后创建 5 点校准曲线，按 Fraction
独立复算逐位比对插值与精确端点，核对非法点集不落库、未知曲线 404、越界信号 422、
触发器拒绝直接改写/删除/**追加**对照点与解封（换算依据逐位不变），全部通过退出码 0，否则退出码 1。
