"""预拌砂浆含水修正 API。

模块划分：
- app.schemas    请求校验（Pydantic 契约）
- app.calculator 修正计算（纯 Decimal，中间值完整精度）
- app.summary    批次汇总（ROUND_HALF_UP 三位小数舍入与响应组装）
- app.main       FastAPI 装配与错误反馈
"""
