"""修正计算模块的公式与精度测试。"""
from decimal import Decimal, localcontext

import pytest

from app.calculator import (
    correct_aggregate,
    correct_batch,
    free_water_mass,
    wet_batch_mass,
)


class _Agg:
    """满足 AggregateLike 协议的最小测试桩。"""

    def __init__(self, name, dry_mass_kg, moisture_pct, absorption_pct):
        self.name = name
        self.dry_mass_kg = Decimal(dry_mass_kg)
        self.moisture_pct = Decimal(moisture_pct)
        self.absorption_pct = Decimal(absorption_pct)


class TestWetBatchMass:
    def test_basic(self):
        # 800 × (1 + 5/100) = 840
        assert wet_batch_mass(Decimal("800"), Decimal("5")) == Decimal("840")

    def test_zero_moisture_equals_dry_mass(self):
        assert wet_batch_mass(Decimal("123.456"), Decimal("0")) == Decimal("123.456")

    def test_max_moisture_40(self):
        # 100 × 1.40 = 140
        assert wet_batch_mass(Decimal("100"), Decimal("40")) == Decimal("140")

    def test_full_precision_no_rounding(self):
        # 100 × (1 + 3.333/100) = 103.333，中间值保持完整精度
        assert wet_batch_mass(Decimal("100"), Decimal("3.333")) == Decimal("103.333")
        # 1/3 类无限循环在 Decimal 下按上下文精度截断，但绝不做三位小数舍入
        result = wet_batch_mass(Decimal("1"), Decimal("0.0001"))
        assert result == Decimal("1.000001")


class TestFreeWaterMass:
    def test_basic(self):
        # 800 × (5 − 1)/100 = 32
        assert free_water_mass(Decimal("800"), Decimal("5"), Decimal("1")) == Decimal("32")

    def test_negative_when_absorption_exceeds_moisture(self):
        # 吸水率高于含水率 → 自由水量为负（骨料反而吸水）
        assert free_water_mass(Decimal("100"), Decimal("1"), Decimal("4")) == Decimal("-3")

    def test_zero_when_equal(self):
        assert free_water_mass(Decimal("250"), Decimal("2.5"), Decimal("2.5")) == Decimal("0")

    def test_boundary_rates(self):
        # 含水率 40、吸水率 15 的极端组合
        assert free_water_mass(Decimal("1000"), Decimal("40"), Decimal("15")) == Decimal("250")
        assert free_water_mass(Decimal("1000"), Decimal("0"), Decimal("15")) == Decimal("-150")


class TestCorrectBatch:
    def test_multi_aggregate_sum(self):
        aggs = [
            _Agg("砂A", "800", "5.0", "1.0"),   # free = 32
            _Agg("砂B", "600", "3.5", "0.5"),   # free = 18
            _Agg("石粉", "200", "0.5", "0.2"),  # free = 0.6
        ]
        batch = correct_batch(Decimal("180"), aggs)
        assert batch.total_free_water_kg == Decimal("50.6")
        assert batch.final_water_kg == Decimal("129.4")
        assert [it.wet_mass_kg for it in batch.items] == [
            Decimal("840"), Decimal("621"), Decimal("201"),
        ]
        assert [it.index for it in batch.items] == [0, 1, 2]
        assert [it.name for it in batch.items] == ["砂A", "砂B", "石粉"]

    def test_final_water_exactly_zero_is_legal(self):
        aggs = [_Agg("砂", "1000", "5", "0")]  # free = 50
        batch = correct_batch(Decimal("50"), aggs)
        assert batch.final_water_kg == Decimal("0")

    def test_final_water_negative_detected_exactly(self):
        aggs = [_Agg("砂", "1000", "5", "0")]  # free = 50
        batch = correct_batch(Decimal("49.999"), aggs)
        assert batch.final_water_kg == Decimal("-0.001") < 0

    def test_intermediate_precision_preserved_in_batch(self):
        aggs = [_Agg("砂", "333", "3.333", "1.111")]
        batch = correct_batch(Decimal("10"), aggs)
        # free = 333 × 2.222/100 = 7.39926，完整保留
        assert batch.items[0].free_water_kg == Decimal("7.39926")
        assert batch.final_water_kg == Decimal("2.60074")

    def test_single_aggregate(self):
        batch = correct_batch(Decimal("10"), [_Agg("砂", "100", "2", "1")])
        assert len(batch.items) == 1
        assert batch.final_water_kg == Decimal("9")

    @pytest.mark.parametrize("dry", ["0.001", "0.000001"])
    def test_tiny_masses_keep_precision(self, dry):
        batch = correct_batch(Decimal("1"), [_Agg("粉", dry, "40", "15")])
        expected_free = Decimal(dry) * Decimal("25") / Decimal("100")
        assert batch.items[0].free_water_kg == expected_free

    def test_precision_beyond_28_digits_not_truncated(self):
        # 31 位有效数字：默认 28 位上下文会丢尾数，批量计算必须完整保留
        dry = "1.0000000000000000000000000001"
        batch = correct_batch(Decimal("1"), [_Agg("砂", dry, "0", "0")])
        assert batch.items[0].wet_mass_kg == Decimal(dry)

    def test_high_precision_product_exact(self):
        dry = Decimal("3.141592653589793238462643383")
        with localcontext() as ctx:
            ctx.prec = 60
            expected_wet = dry * Decimal("1.025")
            expected_free = dry * Decimal("0.025")
        batch = correct_batch(Decimal("0.5"), [_Agg("砂", str(dry), "2.5", "0")])
        assert batch.items[0].wet_mass_kg == expected_wet
        assert batch.items[0].free_water_kg == expected_free

    def test_many_decimal_place_rates_accepted(self):
        # 高精度百分率按有效范围受理并精确计算
        batch = correct_batch(
            Decimal("10"), [_Agg("砂", "100", "5.1234567890123", "0.0000000000001")]
        )
        assert batch.items[0].wet_mass_kg == Decimal("105.1234567890123")
        assert batch.items[0].free_water_kg == Decimal("5.1234567890122")

    def test_huge_design_water_minus_tiny_free_water(self):
        # 设计加水量 1e28、自由水 0.6：28 位默认精度会吞掉应扣除的 0.6
        batch = correct_batch(Decimal("1e28"), [_Agg("砂", "60", "1", "0")])
        assert batch.final_water_kg == Decimal("9999999999999999999999999999.4")

    def test_huge_design_water_deduction_not_rounded_away(self):
        # 自由水 0.4：精度不足时会进位回 1e28，最终加水量偏大
        batch = correct_batch(Decimal("1e28"), [_Agg("砂", "40", "1", "0")])
        assert batch.final_water_kg == Decimal("9999999999999999999999999999.6")

    def test_huge_design_water_multi_aggregate(self):
        # 巨大设计加水量 + 8 种骨料：合计自由水仍被精确扣除
        aggs = [_Agg(f"砂{i}", "100", "1.5", "0.5") for i in range(8)]  # 各 free = 1
        batch = correct_batch(Decimal("1e30"), aggs)
        assert batch.total_free_water_kg == Decimal("8")
        assert batch.final_water_kg == Decimal("999999999999999999999999999992")
