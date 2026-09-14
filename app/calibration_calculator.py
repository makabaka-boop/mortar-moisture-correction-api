"""校准曲线领域计算：纯 Decimal 分段线性插值，中间值保持完整精度。

在线含水传感器更换探头后，实验员以标准样把“原始信号 → 参考含水率”的对照点
固化成不可变校准曲线；生产调用方只给曲线编号与单个原始信号，由本模块换算：

- 命中端点（原始信号等于某个对照点信号）：直接返回该点参考含水率，不做插值；
  区间回显约定：首个端点归首段，最后一个端点归末段，其余内部端点归其右侧
  相邻区间（bisect_right 定位，全程自洽，区间端点含水率仍逐位回显）；
- 落在两点之间：以相邻两点做线性插值
      y = y1 + (x − x1) × (y2 − y1) ÷ (x2 − x1)，
  中间值不舍入（调用方在出口处按 ROUND_HALF_UP 保留三位）；
- 落在曲线范围之外：抛 SignalOutOfRangeError，由端点映射为定位到原始信号的
  结构化 422，不返回任何外推值。

计算上下文精度随输入的量级、小数跨度与有效位数自适应：对照点可能带远超
默认 28 位的有效数字，固定精度截断会把紧挨舍入边界的插值结果推过边界而
错误进位（与取样计算模块同一思路）。
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Sequence, SupportsIndex

# 自适应精度下限：覆盖常规仪表信号（至多十多位有效数字）的乘除与减法
_MIN_PRECISION = 50


class SignalOutOfRangeError(LookupError):
    """原始信号落在曲线信号范围之外（端点映射为定位到原始信号的 422）。

    携带（信号下界, 信号上界, 越界原始信号），供错误信息引用，均为定点字符串。
    """


@dataclass(frozen=True)
class CalibrationInterval:
    """命中的相邻区间及插值结果（端点与含水率均为完整精度 Decimal）。"""

    lower_signal: Decimal
    upper_signal: Decimal
    lower_moisture_pct: Decimal
    upper_moisture_pct: Decimal
    moisture_pct: Decimal


def _required_precision(points: Sequence[tuple[Decimal, Decimal]]) -> int:
    """按输入的量级、小数跨度与有效位数估算精确插值所需的上下文精度。

    精度必须同时覆盖：
    - 各输入的最大有效位数：除信号跨度只移动小数点，舍入误差按有效位计，
      超长有效数字输入（> 50 位）必须整体保留，否则紧挨舍入边界的值会被
      截断后越过边界；
    - 加减法的数位跨度：相邻信号极接近时 x − x1 是微小差量，需要逐位保留。

    取 2 倍跨度加 16 位余量，与取样计算模块同一思路。
    """
    values = [value for pair in points for value in pair]
    max_int = max(max(v.adjusted() + 1, 0) for v in values)
    max_frac = max(max(-v.as_tuple().exponent, 0) for v in values)
    max_sig = max(len(v.as_tuple().digits) for v in values)
    return max(_MIN_PRECISION, 2 * (max_int + max_frac + max_sig) + 16)


def _fixed_point(value: Decimal) -> str:
    """定点字符串（如 '12.5000'），避免科学记数法进入错误信息与查询参数。"""
    return format(value, "f")


def locate_interval(
    points: Sequence[tuple[Decimal, Decimal]], signal: Decimal
) -> tuple[SupportsIndex, SupportsIndex]:
    """定位信号命中的相邻两点下标。

    points 必须按原始信号严格递增（契约层已校验，仓储再守一道）。
    命中端点时，返回以该端点为左端（首点取首段、末点取末段）的区间，
    与换算入口“端点输入直接返回对应参考值”的区间回显约定一致；
    越界时抛 SignalOutOfRangeError。
    """
    first_signal = points[0][0]
    last_signal = points[-1][0]
    if signal < first_signal or signal > last_signal:
        raise SignalOutOfRangeError(
            (_fixed_point(first_signal), _fixed_point(last_signal), _fixed_point(signal))
        )
    # bisect_right：信号等于某点信号时插入位在该点之后
    right = bisect_right([point[0] for point in points], signal)
    upper_index = min(right, len(points) - 1)
    lower_index = upper_index - 1
    return lower_index, upper_index


def interpolate(
    points: Sequence[tuple[Decimal, Decimal]], signal: Decimal
) -> CalibrationInterval:
    """按相邻两点做 Decimal 线性插值（端点直接返回参考值，不做除法）。

    points 为按原始信号严格递增的 (信号, 参考含水率) 序列；含水率同样
    严格递增且在 0 ~ 40（契约层已校验）。
    """
    if len(points) < 2:
        raise ValueError("校准曲线至少需要两个对照点")
    lower_index, upper_index = locate_interval(points, signal)
    x1, y1 = points[lower_index]
    x2, y2 = points[upper_index]
    # 端点输入：不做插值运算，直接返回对应参考值（同时自然命中 0 与 40 界点）
    if signal == x1:
        moisture = y1
    elif signal == x2:
        moisture = y2
    else:
        with localcontext() as ctx:
            ctx.prec = _required_precision(points)
            moisture = y1 + (signal - x1) * (y2 - y1) / (x2 - x1)
    return CalibrationInterval(
        lower_signal=x1,
        upper_signal=x2,
        lower_moisture_pct=y1,
        upper_moisture_pct=y2,
        moisture_pct=moisture,
    )
