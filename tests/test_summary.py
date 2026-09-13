"""批次汇总模块的舍入与组装测试。"""
from decimal import Decimal

import pytest

from app.calculator import correct_batch
from app.summary import build_summary, round3


class _Agg:
    def __init__(self, name, dry_mass_kg, moisture_pct, absorption_pct):
        self.name = name
        self.dry_mass_kg = Decimal(dry_mass_kg)
        self.moisture_pct = Decimal(moisture_pct)
        self.absorption_pct = Decimal(absorption_pct)


class TestRound3:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("840", "840.000"),
            ("129.4", "129.400"),
            ("0", "0.000"),
            ("1.0004", "1.000"),
            ("1.0005", "1.001"),   # 恰好在第四位为 5：HALF_UP 进位（HALF_EVEN 会得到 1.000）
            ("2.6749", "2.675"),
            ("2.6755", "2.676"),
            ("0.0005", "0.001"),
            ("0.0004", "0.000"),
            ("-1.0005", "-1.001"),  # 负数按绝对值远离零方向进位
            ("-0.0004", "-0.000"),
            ("123456789.1235", "123456789.124"),  # 大数值不溢出
            ("1E+28", "10000000000000000000000000000.000"),  # 科学记数法大数按全量数位舍入
            ("9999999999999999999999999999.4", "9999999999999999999999999999.400"),
        ],
    )
    def test_half_up(self, raw, expected):
        assert round3(Decimal(raw)) == Decimal(expected)

    def test_always_three_decimal_exponent(self):
        assert round3(Decimal("7")).as_tuple().exponent == -3
        assert round3(Decimal("7.1")).as_tuple().exponent == -3


class TestBuildSummary:
    def test_totals_and_items_rounded(self):
        aggs = [
            _Agg("砂A", "800", "5.0", "1.0"),
            _Agg("砂B", "600", "3.5", "0.5"),
            _Agg("石粉", "200", "0.5", "0.2"),
        ]
        sheet = build_summary(correct_batch(Decimal("180"), aggs))
        assert sheet.item_count == 3
        assert sheet.items[0].wet_mass_kg == Decimal("840.000")
        assert sheet.items[2].free_water_kg == Decimal("0.600")
        assert sheet.total_dry_mass_kg == Decimal("1600.000")
        assert sheet.total_wet_mass_kg == Decimal("1662.000")
        assert sheet.total_free_water_kg == Decimal("50.600")
        assert sheet.design_water_kg == Decimal("180.000")
        assert sheet.final_water_kg == Decimal("129.400")

    def test_totals_computed_from_unrounded_intermediates(self):
        # 两项自由水量各为 0.0004：逐项显示 0.000，但合计应按完整精度 0.0008 → 0.001
        aggs = [
            _Agg("甲", "0.08", "0.5", "0"),
            _Agg("乙", "0.08", "0.5", "0"),
        ]
        sheet = build_summary(correct_batch(Decimal("1"), aggs))
        assert sheet.items[0].free_water_kg == Decimal("0.000")
        assert sheet.total_free_water_kg == Decimal("0.001")

    def test_serialized_as_three_decimal_strings(self):
        sheet = build_summary(
            correct_batch(Decimal("180"), [_Agg("砂", "800", "5", "1")])
        )
        payload = sheet.model_dump(mode="json")
        assert payload["items"][0]["wet_mass_kg"] == "840.000"
        assert payload["final_water_kg"] == "148.000"
        assert payload["total_free_water_kg"] == "32.000"
