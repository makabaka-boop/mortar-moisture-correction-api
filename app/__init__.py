"""预拌砂浆含水修正 API。

模块划分（修正单）：
- app.schemas    修正单请求校验（Pydantic 契约）
- app.calculator 修正计算（纯 Decimal，中间值完整精度）
- app.summary    批次汇总（ROUND_HALF_UP 三位小数舍入与响应组装）

模块划分（取样批次）：
- app.sampling_schemas     取样批次契约（2~5 组湿样/干样称量）
- app.sampling_calculator  各组含水率与中位数（完整精度）
- app.sampling_repository  SQLite 仓储（整批事务、条件确认、重建可读）
- app.sampling_service     创建/确认编排与响应组装

模块划分（校准曲线）：
- app.calibration_schemas     校准曲线契约（3~8 个严格递增对照点，参考含水率 0~40）
- app.calibration_calculator  相邻两点 Decimal 线性插值（完整精度）、端点直返、越界拒绝
- app.calibration_repository  SQLite 仓储（不可变曲线/对照点、触发器、追加到既有库）
- app.calibration_service     创建曲线/换算编排与响应组装

- app.main       FastAPI 装配与错误反馈
"""
