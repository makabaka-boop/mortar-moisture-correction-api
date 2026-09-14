"""取样批次含水率计算：公式、完整精度、中位数与偶数平均。"""
from decimal import Decimal, localcontext
from fractions import Fraction

import pytest

from app.sampling_calculator import (
    BatchEvaluation,
    ReadingResult,
    evaluate_readings,
    median,
    moisture_percent,
)


def strict_median3(pairs):
    """用 Fraction 精确链复算三位 HALF_UP 中位数，与 Decimal 实现互不复用。

    任何固定精度的 Decimal 复算都会与产品代码共享同一截断风险；
    Fraction 对有限小数输入是精确有理数，可作为边界判定的基准。
    """
    pcts = sorted(
        (Fraction(w) - Fraction(d)) / Fraction(d) * 100 for w, d in pairs
    )
    n = len(pcts)
    med = pcts[n // 2] if n % 2 else (pcts[n // 2 - 1] + pcts[n // 2]) / 2
    scaled = med * 1000 + Fraction(1, 2)  # ROUND_HALF_UP：半数恰进位
    return Fraction(int(scaled), 1000)


class TestMoisturePercent:
    def test_basic(self):
        # (500 − 475) / 475 × 100 = 5.263157…（按模块固定 50 位精度）
        with localcontext() as ctx:
            ctx.prec = 50
            expected = Decimal("25") / Decimal("475") * 100
        assert moisture_percent(Decimal("500"), Decimal("475")) == expected

    def test_exact_terminating(self):
        # (210 − 200) / 200 × 100 = 5
        assert moisture_percent(Decimal("210"), Decimal("200")) == Decimal("5")

    def test_small_gap(self):
        assert moisture_percent(Decimal("100.0001"), Decimal("100")) == Decimal("0.0001")

    def test_high_precision_not_rounded(self):
        wet = Decimal("500.123456789012345678901234567")
        dry = Decimal("477.72")
        result = moisture_percent(wet, dry)
        # 除不尽产生无限循环小数：以 Fraction 精确值为准，产品自适应精度
        # 至少保留 50 位，逐位等于精确值的前 50 位（不是三位截断，也不在
        # 旧的固定 50 位边界上提前停住）。
        strict = (Fraction(wet) - Fraction(dry)) / Fraction(dry) * 100
        assert Fraction(result) == Fraction(Decimal(format(result, "f")))
        assert abs(Fraction(result) - strict) < Fraction(1, 10 ** 45)
        # 结果串的有效位数不少于 50（自适应精度覆盖，而非只留三位）
        assert len(result.as_tuple().digits) >= 50
        assert result.as_tuple().exponent < -3
        # 与 200 位高基准的前 50 位有效数字逐位一致
        with localcontext() as ctx:
            ctx.prec = 200
            high = (wet - dry) / dry * 100
        assert format(result, "f")[:52] == format(high, "f")[:52]


class TestAdaptivePrecisionBoundary:
    """超长有效数字且紧挨三位舍入边界时，固定 50 位截断会把 1.234 误进位为 1.235。"""

    @staticmethod
    def _wet(pct: Decimal) -> Decimal:
        with localcontext() as ctx:
            ctx.prec = 400
            return Decimal("1") + pct / 100

    def _odd_pairs(self, target: Decimal):
        return [
            (self._wet(Decimal("0.5")), Decimal("1")),  # 0.5%（下侧）
            (self._wet(target), Decimal("1")),          # 目标中位数
            (self._wet(Decimal("2")), Decimal("1")),    # 2%（上侧）
        ]

    def _even_pairs(self, target: Decimal):
        # 中位数 =（0.5 + b）/2 = target
        with localcontext() as ctx:
            ctx.prec = 400
            b_pct = 2 * target - Decimal("0.5")
        return [
            (self._wet(Decimal("0.5")), Decimal("1")),
            (self._wet(b_pct), Decimal("1")),
        ]

    @pytest.mark.parametrize("gap_exp", [52, 100, 200])
    def test_odd_groups_just_below_boundary_does_not_round_up(self, gap_exp):
        with localcontext() as ctx:
            ctx.prec = gap_exp + 200
            target = Decimal("1.2345") - Decimal(10) ** -gap_exp
        pairs = self._odd_pairs(target)
        ev = evaluate_readings(pairs)
        # 旧固定 50 位实现会把中位数截成 1.2345 而错误进位
        assert Fraction(ev.median_moisture_pct) < Fraction("1.2345")
        assert strict_median3(pairs) == Fraction("1.234")
        from app.summary import round3
        assert round3(ev.median_moisture_pct) == Decimal("1.234")

    @pytest.mark.parametrize("gap_exp", [50, 100, 200])
    def test_even_groups_just_below_boundary_average_not_round_up(self, gap_exp):
        with localcontext() as ctx:
            ctx.prec = gap_exp + 200
            target = Decimal("1.2345") - Decimal(10) ** -gap_exp
        pairs = self._even_pairs(target)
        ev = evaluate_readings(pairs)
        assert Fraction(ev.median_moisture_pct) < Fraction("1.2345")
        assert strict_median3(pairs) == Fraction("1.234")
        from app.summary import round3
        assert round3(ev.median_moisture_pct) == Decimal("1.234")

    def test_just_above_boundary_still_rounds_up(self):
        with localcontext() as ctx:
            ctx.prec = 300
            target = Decimal("1.2345") + Decimal(10) ** -100
        pairs = self._odd_pairs(target)
        ev = evaluate_readings(pairs)
        assert strict_median3(pairs) == Fraction("1.235")
        from app.summary import round3
        assert round3(ev.median_moisture_pct) == Decimal("1.235")

    def test_exactly_on_boundary_half_up(self):
        pairs = self._odd_pairs(Decimal("1.2345"))
        ev = evaluate_readings(pairs)
        assert strict_median3(pairs) == Fraction("1.235")
        from app.summary import round3
        assert round3(ev.median_moisture_pct) == Decimal("1.235")

    def test_individual_reading_also_uses_adaptive_precision(self):
        # 单组结果同样不得在 50 位处被截断
        with localcontext() as ctx:
            ctx.prec = 250
            target = Decimal("1.2345") - Decimal(10) ** -100
            wet = self._wet(target)
        result = moisture_percent(wet, Decimal("1"))
        assert Fraction(result) < Fraction("1.2345")
        assert len(result.as_tuple().digits) > 50


class TestMedian:
    def test_odd_count(self):
        assert median([Decimal("3"), Decimal("1"), Decimal("2")]) == Decimal("2")

    def test_even_count_averages_middle_pair(self):
        # 中间两项 2、4 → 平均 3
        assert median([Decimal("1"), Decimal("4"), Decimal("2"), Decimal("9")]) == Decimal("3")

    def test_even_count_average_may_introduce_fraction(self):
        # 中间两项 1、2 → 1.5
        assert median([Decimal("1"), Decimal("2")]) == Decimal("1.5")

    def test_does_not_mutate_input_order(self):
        values = [Decimal("5"), Decimal("1"), Decimal("3")]
        assert median(values) == Decimal("3")
        assert values == [Decimal("5"), Decimal("1"), Decimal("3")]

    @pytest.mark.parametrize("n", [2, 3, 4, 5])
    def test_supports_two_to_five_groups(self, n):
        values = [Decimal(i) + Decimal(i) / 10 for i in range(1, n + 1)]
        ordered = sorted(values)
        mid = n // 2
        if n % 2:
            assert median(values) == ordered[mid]
        else:
            assert median(values) == (ordered[mid - 1] + ordered[mid]) / 2


class TestEvaluateReadings:
    def test_multi_group_preserves_order_and_indices(self):
        ev = evaluate_readings([(Decimal("210"), Decimal("200")), (Decimal("105"), Decimal("100"))])
        assert isinstance(ev, BatchEvaluation)
        assert [r.index for r in ev.readings] == [0, 1]
        assert all(isinstance(r, ReadingResult) for r in ev.readings)
        assert ev.readings[0].moisture_pct == Decimal("5")
        assert ev.readings[1].moisture_pct == Decimal("5")
        assert ev.median_moisture_pct == Decimal("5")

    def test_odd_median_full_precision(self):
        # 三组：5、4.6875、6.75 → 中位数 5（不被代表值舍入影响）
        ev = evaluate_readings(
            [
                (Decimal("210"), Decimal("200")),       # 5
                (Decimal("335"), Decimal("320")),       # 4.6875
                (Decimal("427"), Decimal("400")),       # 6.75
            ]
        )
        assert ev.median_moisture_pct == Decimal("5")

    def test_even_median_full_precision_average(self):
        ev = evaluate_readings(
            [
                (Decimal("210"), Decimal("200")),  # 5
                (Decimal("206"), Decimal("200")),  # 3
            ]
        )
        assert ev.median_moisture_pct == Decimal("4")

    def test_median_does_not_round_to_three_places(self):
        # 中位数值 5.12345… 在计算出口仍保持完整精度（三位舍入由 service 负责）
        ev = evaluate_readings(
            [
                (Decimal("100.05"), Decimal("100")),   # 0.05
                (Decimal("105.12345"), Decimal("100")),  # 5.12345
                (Decimal("110"), Decimal("100")),      # 10
            ]
        )
        assert ev.median_moisture_pct == Decimal("5.12345")
