"""批次汇总模块的舍入与组装测试。"""
from decimal import Decimal

import pytest

from app.calculator import correct_batch
from app.summary import build_summary, round3, round6


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


class TestRound6:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("2", "2.000000"),
            ("0.5", "0.500000"),
            ("1.2345674", "1.234567"),
            ("1.2345675", "1.234568"),  # 第七位恰为 5：HALF_UP 进位
            ("0.0000005", "0.000001"),  # HALF_EVEN 会得到 0.000000
            ("0.0000004", "0.000000"),
            ("-1.2345675", "-1.234568"),  # 负数按绝对值远离零方向进位
            ("123456789.1234565", "123456789.123457"),  # 大数值不溢出
        ],
    )
    def test_half_up(self, raw, expected):
        assert round6(Decimal(raw)) == Decimal(expected)

    def test_always_six_decimal_exponent(self):
        assert round6(Decimal("2")).as_tuple().exponent == -6


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


class TestBuildSummaryWithTarget:
    """传入目标干料总量时，响应补充三位小数目标与六位缩放系数。"""

    def test_target_and_factor_appended(self):
        aggs = [
            _Agg("砂A", "800", "5.0", "1.0"),
            _Agg("砂B", "600", "3.5", "0.5"),
            _Agg("石粉", "200", "0.5", "0.2"),
        ]
        sheet = build_summary(
            correct_batch(Decimal("180"), aggs, target_dry_total_kg=Decimal("3200"))
        )
        assert sheet.target_dry_total_kg == Decimal("3200.000")
        assert sheet.scale_factor == Decimal("2.000000")
        # 全单同比放大 2 倍
        assert sheet.total_dry_mass_kg == Decimal("3200.000")
        assert sheet.total_wet_mass_kg == Decimal("3324.000")
        assert sheet.total_free_water_kg == Decimal("101.200")
        assert sheet.design_water_kg == Decimal("360.000")
        assert sheet.final_water_kg == Decimal("258.800")

    def test_new_fields_serialized_as_strings(self):
        sheet = build_summary(
            correct_batch(
                Decimal("180"), [_Agg("砂", "800", "5", "1")],
                target_dry_total_kg=Decimal("1600"),
            )
        )
        payload = sheet.model_dump(mode="json")
        assert payload["target_dry_total_kg"] == "1600.000"
        assert payload["scale_factor"] == "2.000000"

    def test_fields_none_without_target(self):
        sheet = build_summary(correct_batch(Decimal("180"), [_Agg("砂", "800", "5", "1")]))
        assert sheet.target_dry_total_kg is None
        assert sheet.scale_factor is None
        # 未传目标时序列化响应不得出现新字段（端点以 exclude_none 保证）
        payload = sheet.model_dump(mode="json", exclude_none=True)
        assert "target_dry_total_kg" not in payload
        assert "scale_factor" not in payload

    def test_scale_factor_half_up_at_seventh_place(self):
        # k = 0.0008/1600 = 0.0000005：HALF_UP → 0.000001（HALF_EVEN 会得到 0.000000）
        sheet = build_summary(
            correct_batch(
                Decimal("180"), [_Agg("砂", "1600", "5", "1")],
                target_dry_total_kg=Decimal("0.0008"),
            )
        )
        assert sheet.scale_factor == Decimal("0.000001")

    def test_repeating_factor_rounded_for_display_only(self):
        # k = 1/3 无限循环：展示舍入为 0.333333，内部完整精度使干基合计仍归位
        aggs = [_Agg("砂", "1500", "5", "1"), _Agg("粉", "1500", "0.5", "0.2")]
        sheet = build_summary(
            correct_batch(Decimal("180"), aggs, target_dry_total_kg=Decimal("1000"))
        )
        assert sheet.scale_factor == Decimal("0.333333")
        assert sheet.total_dry_mass_kg == Decimal("1000.000")

    def test_target_echoed_at_three_places(self):
        # 目标本身带高精度小数：响应按 ROUND_HALF_UP 保留三位
        sheet = build_summary(
            correct_batch(
                Decimal("180"), [_Agg("砂", "800", "5", "1")],
                target_dry_total_kg=Decimal("1000.0005"),
            )
        )
        assert sheet.target_dry_total_kg == Decimal("1000.001")
