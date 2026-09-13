# 预拌砂浆含水修正 API

雨后砂堆含水变化时，按干配方直接投料会同时偏离骨料量与实际加水量。本服务在开机前根据
各骨料的含水率/吸水率生成**可复算的修正单**：逐项湿投料量、自由水量，以及修正后的最终加水量。

纯后端 API，无数据库；Python 3.12 + FastAPI + Pydantic + Decimal。

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

## 输入约束（端点均包含）

| 字段 | 约束 |
| --- | --- |
| `design_water_kg` | 质量（kg），> 0 |
| `aggregates` | 数组，1 ~ 8 种骨料 |
| `aggregates[i].name` | 非空字符串，≤ 64 字符 |
| `aggregates[i].dry_mass_kg` | 干基目标质量（kg），> 0 |
| `aggregates[i].moisture_pct` | 含水率，质量百分数，0 ~ 40 |
| `aggregates[i].absorption_pct` | 吸水率，质量百分数，0 ~ 15 |

小数位数不限：高精度输入（如 `moisture_pct: "5.1234567890123"`）一律受理，
仅按上表有效范围校验；超出范围（如 `40.0000001`）才返回 422。

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

### `GET /health`

返回 `{"status": "ok"}`，用于容器健康检查。

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
├── schemas.py     # 请求校验：Pydantic 契约（范围、质量>0、1~8 种骨料）
├── calculator.py  # 修正计算：纯 Decimal 公式，中间值完整精度
├── summary.py     # 批次汇总：ROUND_HALF_UP 三位小数舍入与响应组装
└── main.py        # FastAPI 装配：端点与统一 422 错误反馈
tests/             # pytest：公式、舍入、边界、错误定位、整单拒绝
verify.py          # 一次性验收：多骨料样例独立复算并比对
```

## 本地运行

```bash
pip install -r requirements-dev.txt
pytest                                   # 运行测试
uvicorn app.main:app --port 8000         # 启动 API
python verify.py                         # 对 localhost:8000 做一次性验收
# 或指定地址：API_BASE_URL=http://localhost:8123 python verify.py
```

交互式文档：`http://localhost:8000/docs`。

## Docker Compose（仅运行 API）

```bash
docker compose up api                    # 启动 API，宿主端口默认 8000
API_PORT=9000 docker compose up api      # API_PORT 覆盖宿主端口
docker compose up verify                 # 一次性验收：起 API → 跑多骨料样例 → 打印修正单后退出
docker compose run --rm verify           # 等价的一次性运行方式
```

`verify` 服务等待 `api` 健康后提交三骨料样例，独立复算并比对响应，
成功时打印唯一的湿投料清单与三位小数最终加水量，退出码 0；不一致则退出码 1。
