"""取样批次 API 测试：创建/确认契约、完整精度中位数、错误定位与持久化。"""
from decimal import Decimal, localcontext
from fractions import Fraction

import pytest
from fastapi.testclient import TestClient

from app import main
from app.sampling_repository import SamplingBatchRepository
from app.sampling_schemas import STATUS_CONFIRMED, STATUS_PENDING

client = TestClient(main.app)

CREATE_URL = "/api/v1/moisture-batches"


def _reading(wet="500.00", dry="475.00"):
    return {"wet_sample_mass": wet, "dry_sample_mass": dry}


def _payload(readings=None, pile="雨后1号砂堆"):
    return {"pile_name": pile, "readings": readings or [_reading(), _reading("480", "460")]}


def _confirm_url(batch_no):
    return f"{CREATE_URL}/{batch_no}/confirm"


def _independent_precision(raw_values):
    """独立重写自适应精度估算（与产品公式同数学、不复用代码），供逐位比对。"""
    decimals = [Decimal(v) for v in raw_values]
    max_int = max(max(v.adjusted() + 1, 0) for v in decimals)
    max_frac = max(max(-v.as_tuple().exponent, 0) for v in decimals)
    max_sig = max(len(v.as_tuple().digits) for v in decimals)
    return max(50, 2 * (max_int + max_frac + max_sig) + 16)


def _independently_expected(readings):
    """独立复算：自适应精度的各组含水率 + Fraction 严格三位 HALF_UP 中位数。

    逐组串按与产品相同数学的自适应精度复算（逐位可比）；代表值不依赖
    任何 Decimal 截断精度，以 Fraction 精确有理数判定舍入边界。
    """
    raw = [v for pair in readings for v in pair]
    prec = _independent_precision(raw)
    with localcontext() as ctx:
        ctx.prec = prec
        pcts = [(Decimal(w) - Decimal(d)) / Decimal(d) * 100 for w, d in readings]
        ordered = sorted(pcts)
        n = len(ordered)
        dec_median = (
            ordered[n // 2]
            if n % 2
            else (ordered[n // 2 - 1] + ordered[n // 2]) / 2
        )
    # Fraction 精确中位数：有限小数输入下无任何截断
    frac_pcts = sorted(
        (Fraction(w) - Fraction(d)) / Fraction(d) * 100 for w, d in readings
    )
    frac_median = (
        frac_pcts[n // 2]
        if n % 2
        else (frac_pcts[n // 2 - 1] + frac_pcts[n // 2]) / 2
    )
    # ROUND_HALF_UP：加 1/2 后向零取整（结果恒正），得到精确千分数 k/1000
    thousandths = int(frac_median * 1000 + Fraction(1, 2))
    median3 = (Decimal(thousandths) / Decimal(1000)).quantize(Decimal("0.001"))
    return pcts, dec_median, median3


@pytest.fixture
def sampling_repo(tmp_path):
    db = SamplingBatchRepository(tmp_path / "api.db")
    main.app.dependency_overrides[main.get_sampling_repository] = lambda: db
    yield db
    main.app.dependency_overrides.clear()


def _create(payload):
    return client.post(CREATE_URL, json=payload)


class TestCreate:
    def test_create_returns_201_pending_with_batch_no_and_raw_readings(self, sampling_repo):
        payload = _payload(
            [_reading("500.12", "477.72"), _reading("480.00", "458.50"), _reading("512.345", "480.005")]
        )
        resp = _create(payload)
        assert resp.status_code == 201
        body = resp.json()
        assert body["batch_no"].startswith("MC")
        assert len(body["batch_no"]) == 19
        assert body["pile_name"] == "雨后1号砂堆"
        assert body["status"] == STATUS_PENDING
        assert body["representative_moisture_pct"] is None
        assert body["confirmed_at"] is None
        assert body["created_at"]
        assert body["readings"] == [
            {"index": 0, "wet_sample_mass": "500.12", "dry_sample_mass": "477.72", "moisture_pct": None},
            {"index": 1, "wet_sample_mass": "480.00", "dry_sample_mass": "458.50", "moisture_pct": None},
            {"index": 2, "wet_sample_mass": "512.345", "dry_sample_mass": "480.005", "moisture_pct": None},
        ]

    @pytest.mark.parametrize("count", [2, 5])
    def test_reading_count_bounds_accepted(self, sampling_repo, count):
        resp = _create(_payload([_reading() for _ in range(count)]))
        assert resp.status_code == 201
        assert len(resp.json()["readings"]) == count

    @pytest.mark.parametrize("count", [1, 6])
    def test_reading_count_out_of_bounds_rejected(self, sampling_repo, count):
        resp = _create(_payload([_reading() for _ in range(count)]))
        assert resp.status_code == 422
        assert resp.json()["detail"][0]["field"] == "readings"
        assert sampling_repo.count_batches() == 0

    def test_empty_pile_name_rejected(self, sampling_repo):
        payload = _payload()
        payload["pile_name"] = "   "
        resp = _create(payload)
        assert resp.status_code == 422
        assert resp.json()["detail"][0]["field"] == "pile_name"


class TestReadingValidation:
    @pytest.mark.parametrize(
        ("wet", "dry"),
        [("100", "100"), ("100", "100.001"), ("100", "101")],
    )
    def test_dry_not_below_wet_rejected_at_reading_index(self, sampling_repo, wet, dry):
        payload = _payload([_reading("200", "190"), _reading(wet, dry), _reading("300", "290")])
        resp = _create(payload)
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail[0]["field"] == "readings[1].dry_sample_mass"
        assert detail[0]["type"] == "dry_not_below_wet"
        assert wet in detail[0]["message"] and dry in detail[0]["message"]
        assert sampling_repo.count_batches() == 0  # 非法称量不落库

    @pytest.mark.parametrize("field", ["wet_sample_mass", "dry_sample_mass"])
    @pytest.mark.parametrize("value", ["0", "-1", "-0.0001"])
    def test_non_positive_mass_rejected_at_reading_index(self, sampling_repo, field, value):
        reading = _reading()
        reading[field] = value
        resp = _create(_payload([reading, _reading()]))
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail[0]["field"] == f"readings[0].{field}"
        assert detail[0]["type"] == "greater_than"
        assert sampling_repo.count_batches() == 0

    @pytest.mark.parametrize("value", ["abc", "1.2.3", "", "NaN", "Infinity"])
    def test_unparseable_mass_rejected(self, sampling_repo, value):
        reading = _reading()
        reading["wet_sample_mass"] = value
        resp = _create(_payload([reading, _reading()]))
        assert resp.status_code == 422
        assert resp.json()["detail"][0]["field"] == "readings[0].wet_sample_mass"

    def test_unknown_field_at_top_level_rejected(self, sampling_repo):
        payload = _payload()
        payload["operator"] = "张工"
        resp = _create(payload)
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail[0]["field"] == "operator"
        assert detail[0]["type"] == "extra_forbidden"
        assert sampling_repo.count_batches() == 0

    def test_unknown_field_in_reading_rejected_at_index(self, sampling_repo):
        reading = _reading()
        reading["tare_mass"] = "5"
        resp = _create(_payload([_reading(), reading]))
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail[0]["field"] == "readings[1].tare_mass"
        assert detail[0]["type"] == "extra_forbidden"

    def test_missing_mass_field_located(self, sampling_repo):
        reading = _reading()
        del reading["dry_sample_mass"]
        resp = _create(_payload([_reading(), reading]))
        assert resp.status_code == 422
        assert resp.json()["detail"][0]["field"] == "readings[1].dry_sample_mass"

    def test_multiple_invalid_readings_all_located(self, sampling_repo):
        resp = _create(
            _payload(
                [
                    _reading("100", "100"),          # readings[0] 干不小于湿
                    _reading("0", "1"),              # readings[1] 湿非正
                    _reading("200", "190"),
                ]
            )
        )
        assert resp.status_code == 422
        fields = sorted(e["field"] for e in resp.json()["detail"])
        assert fields == ["readings[0].dry_sample_mass", "readings[1].wet_sample_mass"]


class TestConfirm:
    THREE = [("500.12", "477.72"), ("480.00", "458.50"), ("512.345", "480.005")]

    def _create_batch(self, sampling_repo, readings=None):
        reading_objs = [_reading(wet, dry) for wet, dry in (readings or self.THREE)]
        resp = _create(_payload(reading_objs))
        assert resp.status_code == 201, resp.text
        return resp.json()["batch_no"]

    def test_confirm_full_precision_then_three_place_half_up_median(self, sampling_repo):
        batch_no = self._create_batch(sampling_repo)
        resp = client.post(_confirm_url(batch_no))
        assert resp.status_code == 200
        body = resp.json()
        pcts, _dec_median, median3 = _independently_expected(self.THREE)
        assert body["status"] == STATUS_CONFIRMED
        assert body["representative_moisture_pct"] == f"{median3:f}"
        assert body["confirmed_at"]
        # 各组结果为完整精度，与独立复算逐位一致（不是三位小数值）
        assert [r["moisture_pct"] for r in body["readings"]] == [format(p, "f") for p in pcts]
        assert body["readings"][0]["moisture_pct"] != "4.689"
        # 原始读数仍原样保留
        assert [
            (r["wet_sample_mass"], r["dry_sample_mass"]) for r in body["readings"]
        ] == self.THREE

    def test_even_group_count_median_averaged(self, sampling_repo):
        readings = [("210", "200"), ("206", "200"), ("310", "300"), ("400", "380")]
        # 5、3、3.333…、5.263… → 中位两项 3.333… 与 5 的平均
        batch_no = self._create_batch(sampling_repo, readings)
        body = client.post(_confirm_url(batch_no)).json()
        _, _, median3 = _independently_expected(readings)
        assert body["representative_moisture_pct"] == f"{median3:f}"

    def test_median_round_half_up_at_fourth_decimal(self, sampling_repo):
        # 构造奇数组，其中位数恰为 5.1235：第四位为 5 → HALF_UP 得 5.124
        # （HALF_EVEN 会得 5.123 或 5.124 视前位奇偶，此处锁定 HALF_UP 语义）
        readings = [
            ("100", "99"),            # 约 1.0101…
            ("210.247", "200"),       # 10.247 / 200 × 100 = 5.1235
            ("110", "100"),           # 10
        ]
        batch_no = self._create_batch(sampling_repo, readings)
        body = client.post(_confirm_url(batch_no)).json()
        assert body["representative_moisture_pct"] == "5.124"
        _, _, median3 = _independently_expected(readings)
        assert str(median3) == "5.124"

    @pytest.mark.parametrize("gap_exp", [52, 100, 200])
    def test_high_precision_value_just_below_rounding_boundary(self, sampling_repo, gap_exp):
        # 回归：真实中位数 = 1.2345 − 1e-gap_exp，固定 50 位截断会越过边界误进位为 1.235
        with localcontext() as ctx:
            ctx.prec = gap_exp + 200
            target = Decimal("1.2345") - Decimal(10) ** -gap_exp

            def wet(pct: Decimal) -> str:
                return format(Decimal("1") + pct / 100, "f")

            readings = [
                (wet(Decimal("0.5")), "1"),  # 0.5%
                (wet(target), "1"),          # 1.2344999…%（边界下侧）
                (wet(Decimal("2")), "1"),    # 2%
            ]
        batch_no = self._create_batch(sampling_repo, readings)
        body = client.post(_confirm_url(batch_no)).json()
        _, dec_median, median3 = _independently_expected(readings)
        assert Fraction(dec_median) < Fraction("1.2345")
        assert str(median3) == "1.234"
        assert body["representative_moisture_pct"] == "1.234"
        # 完整精度结果保留了超过 50 位的尾数位（没有被截断成 1.2345）
        middle_pct = body["readings"][1]["moisture_pct"]
        assert Decimal(middle_pct) < Decimal("1.2345")
        assert "4999" in middle_pct

    def test_high_precision_value_just_above_rounding_boundary_rounds_up(self, sampling_repo):
        with localcontext() as ctx:
            ctx.prec = 300
            target = Decimal("1.2345") + Decimal(10) ** -100

            def wet(pct: Decimal) -> str:
                return format(Decimal("1") + pct / 100, "f")

            readings = [
                (wet(Decimal("0.5")), "1"),
                (wet(target), "1"),
                (wet(Decimal("2")), "1"),
            ]
        batch_no = self._create_batch(sampling_repo, readings)
        body = client.post(_confirm_url(batch_no)).json()
        assert body["representative_moisture_pct"] == "1.235"

    def test_unknown_batch_returns_structured_404(self, sampling_repo):
        resp = client.post(_confirm_url("MC19990101-00000000"))
        assert resp.status_code == 404
        detail = resp.json()["detail"]
        assert isinstance(detail, list)
        assert detail[0]["field"] == "batch_no"
        assert detail[0]["type"] == "batch_not_found"
        assert "MC19990101-00000000" in detail[0]["message"]

    def test_repeat_confirm_returns_409_and_keeps_result(self, sampling_repo):
        batch_no = self._create_batch(sampling_repo)
        first = client.post(_confirm_url(batch_no))
        assert first.status_code == 200
        expected_body = first.json()

        resp = client.post(_confirm_url(batch_no))
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert detail[0]["field"] == "batch_no"
        assert detail[0]["type"] == "batch_already_confirmed"

        # 再读一次：结果与首次确认完全一致，重复确认没有改动任何数据
        again = client.post(_confirm_url(batch_no))
        assert again.status_code == 409
        stored = sampling_repo.get(batch_no)
        assert stored.status == STATUS_CONFIRMED
        assert stored.representative_moisture_pct == expected_body["representative_moisture_pct"]
        assert [r.moisture_pct for r in stored.readings] == [
            r["moisture_pct"] for r in expected_body["readings"]
        ]
        assert stored.confirmed_at == expected_body["confirmed_at"]

    def test_confirmed_readings_match_independent_full_precision(self, sampling_repo):
        readings = [
            ("500.1234567890123456789", "477.72"),
            ("480.00", "458.5000000000000000001"),
            ("512.345", "480.005"),
            ("300", "299.5"),
            ("600.25", "580"),
        ]
        batch_no = self._create_batch(sampling_repo, readings)
        body = client.post(_confirm_url(batch_no)).json()
        pcts, _dec_median, median3 = _independently_expected(readings)
        assert [r["moisture_pct"] for r in body["readings"]] == [format(p, "f") for p in pcts]
        assert body["representative_moisture_pct"] == f"{median3:f}"

    def test_rebuild_repository_still_reads_same_confirmed_batch(self, sampling_repo, tmp_path):
        batch_no = self._create_batch(sampling_repo)
        first = client.post(_confirm_url(batch_no)).json()
        assert first["status"] == STATUS_CONFIRMED

        # 重建仓储指向同一 SQLite 文件：待确认/已确认状态与结果都仍可读取
        rebuilt = SamplingBatchRepository(tmp_path / "api.db")
        rec = rebuilt.get(batch_no)
        assert rec.status == STATUS_CONFIRMED
        assert rec.representative_moisture_pct == first["representative_moisture_pct"]
        assert rec.pile_name == "雨后1号砂堆"
        assert [(r.wet_sample_mass, r.dry_sample_mass, r.moisture_pct) for r in rec.readings] == [
            (r["wet_sample_mass"], r["dry_sample_mass"], r["moisture_pct"]) for r in first["readings"]
        ]
        # 重建后重复确认依旧 409
        with pytest.raises(main.BatchAlreadyConfirmedError):
            rebuilt.confirm(batch_no, ["1"] * 3, "1.000")


class TestCorrectionSheetContractUnchanged:
    """新增取样模块后，修正单契约保持兼容。"""

    def test_health_still_ok(self):
        assert client.get("/health").json() == {"status": "ok"}

    def test_correction_sheet_response_unchanged(self):
        payload = {
            "design_water_kg": "180",
            "aggregates": [
                {"name": "河砂A", "dry_mass_kg": "800", "moisture_pct": "5.0", "absorption_pct": "1.0"},
                {"name": "机制砂B", "dry_mass_kg": "600", "moisture_pct": "3.5", "absorption_pct": "0.5"},
            ],
        }
        resp = client.post("/api/v1/correction-sheet", json=payload)
        assert resp.status_code == 200
        assert resp.json() == {
            "items": [
                {"index": 0, "name": "河砂A", "dry_mass_kg": "800.000",
                 "wet_mass_kg": "840.000", "free_water_kg": "32.000"},
                {"index": 1, "name": "机制砂B", "dry_mass_kg": "600.000",
                 "wet_mass_kg": "621.000", "free_water_kg": "18.000"},
            ],
            "item_count": 2,
            "total_dry_mass_kg": "1400.000",
            "total_wet_mass_kg": "1461.000",
            "total_free_water_kg": "50.000",
            "design_water_kg": "180.000",
            "final_water_kg": "130.000",
        }
