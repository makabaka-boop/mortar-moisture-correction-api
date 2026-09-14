"""校准曲线领域计算测试：Decimal 线性插值、精确端点、越界拒绝与自适应精度。"""
from decimal import Decimal, localcontext
from fractions import Fraction

import pytest

from app.calibration_calculator import (
    CalibrationInterval,
    SignalOutOfRangeError,
    interpolate,
    locate_interval,
)

POINTS_3 = [
    (Decimal("4"), Decimal("0")),
    (Decimal("12"), Decimal("10")),
    (Decimal("20"), Decimal("40")),
]


def _expected(points, signal: Fraction) -> Fraction:
    """以 Fraction 独立复算严格有理数插值（端点直接取参考值）。"""
    xs = [Fraction(format(p[0], "f")) for p in points]
    ys = [Fraction(format(p[1], "f")) for p in points]
    x = Fraction(signal)
    if x in xs:
        return ys[xs.index(x)]
    i = next(i for i in range(len(xs) - 1) if xs[i] < x < xs[i + 1])
    return ys[i] + (x - xs[i]) * (ys[i + 1] - ys[i]) / (xs[i + 1] - xs[i])


class TestInterpolate:
    def test_midpoint_full_precision_decimal(self):
        result = interpolate(POINTS_3, Decimal("8"))
        assert isinstance(result, CalibrationInterval)
        # (4,0)–(12,10) 中点 → 5，区间端点逐位回显
        assert result.moisture_pct == Decimal("5")
        assert (
            result.lower_signal,
            result.upper_signal,
            result.lower_moisture_pct,
            result.upper_moisture_pct,
        ) == (Decimal("4"), Decimal("12"), Decimal("0"), Decimal("10"))

    def test_interpolated_value_not_rounded_midstream(self):
        # 1/3 位置：x = 4 + 8/3，非终止小数须保持完整精度（不舍入成 3.333）
        result = interpolate(POINTS_3, Decimal("6.666666666666666666666666667"))
        expected = _expected(
            POINTS_3, Fraction("6.666666666666666666666666667")
        )
        assert Fraction(format(result.moisture_pct, "f")) == expected
        assert result.moisture_pct != Decimal(result.moisture_pct).quantize(
            Decimal("0.001")
        ) or len(format(result.moisture_pct, "f").split(".")[1]) > 3

    @pytest.mark.parametrize(
        ("signal", "moisture", "interval"),
        [
            ("4", "0", (Decimal("4"), Decimal("12"))),
            # 内部端点归其右侧相邻区间（换算入口直接返回该点参考值，不做插值）
            ("12", "10", (Decimal("12"), Decimal("20"))),
            ("20", "40", (Decimal("12"), Decimal("20"))),
        ],
    )
    def test_endpoint_returns_reference_directly(self, signal, moisture, interval):
        result = interpolate(POINTS_3, Decimal(signal))
        assert result.moisture_pct == Decimal(moisture)
        assert (result.lower_signal, result.upper_signal) == interval

    def test_second_interval_interpolation(self):
        # (12,10)–(20,40)：x=14（1/4 跨度）→ 10 + 30/4 = 17.5
        result = interpolate(POINTS_3, Decimal("14"))
        assert result.moisture_pct == Decimal("17.5")
        assert result.lower_signal == Decimal("12")
        assert result.upper_signal == Decimal("20")

    @pytest.mark.parametrize("bad_signal", ["3.9999", "20.0001", "-1", "100"])
    def test_out_of_range_raises_with_bounds_and_signal(self, bad_signal):
        with pytest.raises(SignalOutOfRangeError) as exc_info:
            interpolate(POINTS_3, Decimal(bad_signal))
        low, high, signal = exc_info.value.args[0]
        assert (low, high, signal) == ("4", "20", bad_signal)

    @pytest.mark.parametrize(
        ("signal", "expected_indices"),
        [("4", (0, 1)), ("12", (1, 2)), ("20", (1, 2)), ("8", (0, 1))],
    )
    def test_locate_interval_indices(self, signal, expected_indices):
        assert locate_interval(POINTS_3, Decimal(signal)) == expected_indices

    def test_single_or_empty_points_rejected(self):
        with pytest.raises(ValueError):
            interpolate([(Decimal("1"), Decimal("2"))], Decimal("1"))


class TestAdaptivePrecision:
    def test_high_precision_value_just_below_half_up_boundary(self):
        """真实插值 = 1.2345 − 1e-60 时须保持 < 边界（固定 28 位截断会误进位）。"""
        with localcontext() as ctx:
            ctx.prec = 300
            target_y = Decimal("1.2345") - Decimal(10) ** -60
            # 三点线性插值：x = 0,1,2；y1 = 0，中点信号命中 target_y
            points = [
                (Decimal("0"), Decimal("0")),
                (Decimal("1"), target_y),
                (Decimal("2"), target_y * 2),
            ]
        result = interpolate(points, Decimal("1"))
        assert result.moisture_pct == target_y
        assert result.moisture_pct < Decimal("1.2345")
        assert Fraction(format(result.moisture_pct, "f")) < Fraction("1.2345")

    def test_many_decimal_places_input_preserved(self):
        x = Decimal("1.000000000000000000000000000000000000000000000000001")
        points = [
            (Decimal("1"), Decimal("0")),
            (Decimal("2"), Decimal("10")),
            (Decimal("3"), Decimal("20")),
        ]
        result = interpolate(points, x)
        expected = _expected(points, Fraction(format(x, "f")))
        assert Fraction(format(result.moisture_pct, "f")) == expected
