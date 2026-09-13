"""API 端点测试：契约、边界、错误定位与整单拒绝。"""
import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

URL = "/api/v1/correction-sheet"


def _agg(name="砂", dry="100", moisture="5", absorption="1"):
    return {
        "name": name,
        "dry_mass_kg": dry,
        "moisture_pct": moisture,
        "absorption_pct": absorption,
    }


def _payload(aggs, design="180"):
    return {"design_water_kg": design, "aggregates": aggs}


class TestHappyPath:
    def test_multi_aggregate_sheet(self):
        payload = _payload(
            [
                _agg("河砂A", "800", "5.0", "1.0"),
                _agg("机制砂B", "600", "3.5", "0.5"),
                _agg("石粉", "200", "0.5", "0.2"),
            ]
        )
        resp = client.post(URL, json=payload)
        assert resp.status_code == 200
        body = resp.json()
        assert body["item_count"] == 3
        assert body["items"] == [
            {"index": 0, "name": "河砂A", "dry_mass_kg": "800.000",
             "wet_mass_kg": "840.000", "free_water_kg": "32.000"},
            {"index": 1, "name": "机制砂B", "dry_mass_kg": "600.000",
             "wet_mass_kg": "621.000", "free_water_kg": "18.000"},
            {"index": 2, "name": "石粉", "dry_mass_kg": "200.000",
             "wet_mass_kg": "201.000", "free_water_kg": "0.600"},
        ]
        assert body["total_dry_mass_kg"] == "1600.000"
        assert body["total_wet_mass_kg"] == "1662.000"
        assert body["total_free_water_kg"] == "50.600"
        assert body["design_water_kg"] == "180.000"
        assert body["final_water_kg"] == "129.400"

    def test_rounding_half_up_visible_in_response(self):
        # 湿投料量 = 100 × 1.000005 = 100.0005 → HALF_UP 得 100.001
        resp = client.post(URL, json=_payload([_agg(dry="100", moisture="0.0005", absorption="0")]))
        assert resp.status_code == 200
        assert resp.json()["items"][0]["wet_mass_kg"] == "100.001"

    def test_absorption_above_moisture_raises_final_water(self):
        # 自由水量为负 → 最终加水量高于设计加水量
        resp = client.post(URL, json=_payload([_agg(dry="100", moisture="1", absorption="4")], design="10"))
        assert resp.status_code == 200
        body = resp.json()
        assert body["items"][0]["free_water_kg"] == "-3.000"
        assert body["final_water_kg"] == "13.000"

    def test_final_water_exactly_zero_is_legal(self):
        resp = client.post(URL, json=_payload([_agg(dry="1000", moisture="5", absorption="0")], design="50"))
        assert resp.status_code == 200
        assert resp.json()["final_water_kg"] == "0.000"


class TestBoundaries:
    @pytest.mark.parametrize("moisture", ["0", "40"])
    def test_moisture_endpoints_inclusive(self, moisture):
        resp = client.post(URL, json=_payload([_agg(moisture=moisture)]))
        assert resp.status_code == 200

    @pytest.mark.parametrize("moisture", ["-0.001", "40.001", "41"])
    def test_moisture_out_of_range_rejected(self, moisture):
        resp = client.post(URL, json=_payload([_agg(moisture=moisture)]))
        assert resp.status_code == 422
        assert resp.json()["detail"][0]["field"] == "aggregates[0].moisture_pct"

    @pytest.mark.parametrize("absorption", ["0", "15"])
    def test_absorption_endpoints_inclusive(self, absorption):
        resp = client.post(URL, json=_payload([_agg(absorption=absorption)]))
        assert resp.status_code == 200

    @pytest.mark.parametrize("absorption", ["-0.001", "15.001", "16"])
    def test_absorption_out_of_range_rejected(self, absorption):
        resp = client.post(URL, json=_payload([_agg(absorption=absorption)]))
        assert resp.status_code == 422
        assert resp.json()["detail"][0]["field"] == "aggregates[0].absorption_pct"

    @pytest.mark.parametrize("count", [1, 8])
    def test_aggregate_count_bounds_ok(self, count):
        resp = client.post(URL, json=_payload([_agg(name=f"砂{i}") for i in range(count)]))
        assert resp.status_code == 200
        assert resp.json()["item_count"] == count

    @pytest.mark.parametrize("count", [0, 9])
    def test_aggregate_count_out_of_bounds_rejected(self, count):
        resp = client.post(URL, json=_payload([_agg(name=f"砂{i}") for i in range(count)]))
        assert resp.status_code == 422
        assert resp.json()["detail"][0]["field"] == "aggregates"

    @pytest.mark.parametrize("dry", ["0", "-1"])
    def test_dry_mass_must_be_positive(self, dry):
        resp = client.post(URL, json=_payload([_agg(dry=dry)]))
        assert resp.status_code == 422
        assert resp.json()["detail"][0]["field"] == "aggregates[0].dry_mass_kg"

    @pytest.mark.parametrize("design", ["0", "-10"])
    def test_design_water_must_be_positive(self, design):
        resp = client.post(URL, json=_payload([_agg()], design=design))
        assert resp.status_code == 422
        assert resp.json()["detail"][0]["field"] == "design_water_kg"

    def test_empty_name_rejected(self):
        resp = client.post(URL, json=_payload([_agg(name="   ")]))
        assert resp.status_code == 422
        assert resp.json()["detail"][0]["field"] == "aggregates[0].name"


class TestErrorFeedback:
    def test_error_locates_aggregate_index(self):
        payload = _payload([_agg("好砂"), _agg("坏砂", moisture="99"), _agg("好砂2")])
        resp = client.post(URL, json=payload)
        assert resp.status_code == 422
        fields = [e["field"] for e in resp.json()["detail"]]
        assert fields == ["aggregates[1].moisture_pct"]

    def test_multiple_errors_all_located(self):
        payload = _payload([_agg(moisture="-1"), _agg(absorption="20")])
        resp = client.post(URL, json=payload)
        assert resp.status_code == 422
        fields = sorted(e["field"] for e in resp.json()["detail"])
        assert fields == ["aggregates[0].moisture_pct", "aggregates[1].absorption_pct"]

    def test_missing_field_located(self):
        agg = _agg()
        del agg["moisture_pct"]
        resp = client.post(URL, json=_payload([_agg(), agg]))
        assert resp.status_code == 422
        assert resp.json()["detail"][0]["field"] == "aggregates[1].moisture_pct"


class TestNegativeFinalWater:
    def test_rejected_with_422_and_no_partial_sheet(self):
        # 自由水量合计 50.6 > 设计加水量 50 → 最终加水量 -0.6
        payload = _payload(
            [
                _agg("河砂A", "800", "5.0", "1.0"),
                _agg("机制砂B", "600", "3.5", "0.5"),
                _agg("石粉", "200", "0.5", "0.2"),
            ],
            design="50",
        )
        resp = client.post(URL, json=payload)
        assert resp.status_code == 422
        body = resp.json()
        assert "items" not in body  # 不返回部分修正单
        assert "final_water_kg" in body["detail"][0]["field"]
        assert body["detail"][0]["type"] == "negative_final_water"

    def test_tiny_negative_also_rejected(self):
        # -0.001 也拒绝：判定基于完整精度而非舍入后展示值
        resp = client.post(URL, json=_payload([_agg(dry="1000", moisture="5", absorption="0")], design="49.999"))
        assert resp.status_code == 422


class TestHighPrecisionInputs:
    """高精度输入按有效范围受理，不再因小数位数被拒。"""

    def test_high_precision_percent_accepted(self):
        resp = client.post(URL, json=_payload([_agg(dry="100", moisture="5.1234567890123", absorption="0")]))
        assert resp.status_code == 200
        # 湿投料量 = 100 × 1.051234567890123 = 105.1234567890123 → 105.123
        assert resp.json()["items"][0]["wet_mass_kg"] == "105.123"

    def test_high_precision_masses_accepted(self):
        resp = client.post(
            URL,
            json=_payload(
                [_agg(dry="0.123456789012345678", moisture="0", absorption="0")],
                design="0.000000001",
            ),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["items"][0]["wet_mass_kg"] == "0.123"
        assert body["final_water_kg"] == "0.000"  # 0.000000001 舍入后显示，本身合法

    def test_many_decimal_places_regression(self):
        # 回归：此前 decimal_places=6 护栏会把这类输入误判为 422
        resp = client.post(
            URL,
            json=_payload([_agg(dry="10.123456789", moisture="1.111111111", absorption="0.000000001")]),
        )
        assert resp.status_code == 200

    @pytest.mark.parametrize("moisture", ["40.0000001", "40.0000000000001"])
    def test_moisture_beyond_range_still_rejected(self, moisture):
        resp = client.post(URL, json=_payload([_agg(moisture=moisture)]))
        assert resp.status_code == 422
        assert resp.json()["detail"][0]["field"] == "aggregates[0].moisture_pct"

    @pytest.mark.parametrize("absorption", ["15.0000001", "15.0000000000001"])
    def test_absorption_beyond_range_still_rejected(self, absorption):
        resp = client.post(URL, json=_payload([_agg(absorption=absorption)]))
        assert resp.status_code == 422
        assert resp.json()["detail"][0]["field"] == "aggregates[0].absorption_pct"

    def test_boundary_with_high_precision_accepted(self):
        # 端点值本身带高精度小数位也合法
        resp = client.post(URL, json=_payload([_agg(moisture="40.0000000000000", absorption="15.0000000000000")]))
        assert resp.status_code == 200


class TestHealth:
    def test_health(self):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
