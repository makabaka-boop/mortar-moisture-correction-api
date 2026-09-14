"""取样批次含水率计算：公式、完整精度、中位数与偶数平均。"""
from decimal import Decimal, localcontext

import pytest

from app.sampling_calculator import (
    BatchEvaluation,
    ReadingResult,
    evaluate_readings,
    median,
    moisture_percent,
)


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
        with localcontext() as ctx:
            ctx.prec = 50
            expected = (wet - dry) / dry * 100
        assert result == expected
        # 中间值绝不舍入到三位，且按 50 位上下文保留高精度尾数位
        assert result.as_tuple().exponent < -3
        assert len(result.as_tuple().digits) > 28


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
