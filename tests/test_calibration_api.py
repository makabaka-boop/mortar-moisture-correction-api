"""校准曲线 API 测试：创建/换算契约、精确端点、插值三位舍入、非法点集不落库、404/422。"""
from decimal import Decimal
from fractions import Fraction

import pytest
from fastapi.testclient import TestClient

from app import main
from app.calibration_repository import CalibrationCurveRepository

client = TestClient(main.app)

CURVES_URL = "/api/v1/moisture-calibration-curves"
CONVERT_URL = f"{CURVES_URL}/convert"


def _points(signals=("4.0", "8.0", "12.0", "16.0", "20.0"),
            moisture=("0", "5", "10", "20", "40")):
    return list(zip(signals, moisture))


def _payload(sensor_id="SENSOR-A1", points=None):
    points = points or _points()
    return {
        "sensor_id": sensor_id,
        "raw_signals": [p[0] for p in points],
        "reference_moisture_pct": [p[1] for p in points],
    }


@pytest.fixture
def calibration_repo(tmp_path):
    db = CalibrationCurveRepository(tmp_path / "calibration.db")
    main.app.dependency_overrides[main.get_calibration_repository] = lambda: db
    yield db
    main.app.dependency_overrides.clear()


def _create(payload=None):
    return client.post(CURVES_URL, json=payload or _payload())


def _create_curve(points=None, sensor_id="SENSOR-A1"):
    resp = _create(_payload(sensor_id=sensor_id, points=points or _points()))
    assert resp.status_code == 201, resp.text
    return resp.json()


def _independent_half_up_3(value: Fraction) -> str:
    """独立的三位 ROUND_HALF_UP（值恒为非负含水率）。"""
    thousandths = int(value * 1000 + Fraction(1, 2))
    return f"{Decimal(thousandths) / Decimal(1000):.3f}"


def _independently_interpolate(points, signal: str) -> tuple[str, tuple[str, str]]:
    """Fraction 严格复算：返回（三位 HALF_UP 含水率, 命中区间两端信号）。"""
    xs = [Fraction(s) for s, _ in points]
    ys = [Fraction(m) for _, m in points]
    x = Fraction(signal)
    if x == xs[0]:
        i, j = 0, 1
        y = ys[0]
    elif x == xs[-1]:
        i, j = len(xs) - 2, len(xs) - 1
        y = ys[-1]
    elif x in xs:
        # 内部端点归其右侧相邻区间（与 bisect_right 定位一致），含水率取端点参考值
        k = xs.index(x)
        i, j = k, k + 1
        y = ys[k]
    else:
        k = next(k for k in range(len(xs) - 1) if xs[k] < x < xs[k + 1])
        i, j = k, k + 1
        y = ys[i] + (x - xs[i]) * (ys[j] - ys[i]) / (xs[j] - xs[i])
    return _independent_half_up_3(y), (points[i][0], points[j][0])


class TestCreateCurve:
    def test_create_returns_201_immutable_curve_with_points(self, calibration_repo):
        points = _points()
        resp = _create()
        assert resp.status_code == 201
        body = resp.json()
        assert body["curve_no"].startswith("CC")
        assert len(body["curve_no"]) == 19
        assert body["sensor_id"] == "SENSOR-A1"
        assert body["created_at"]
        assert body["points"] == [
            {"index": i, "raw_signal": s, "reference_moisture_pct": m}
            for i, (s, m) in enumerate(points)
        ]
        # 落库数量与创建响应一致
        assert calibration_repo.count_curves() == 1
        assert calibration_repo.count_points(body["curve_no"]) == 5

    @pytest.mark.parametrize("count", [3, 8])
    def test_point_count_bounds_accepted(self, calibration_repo, count):
        points = [(str(4 + 2 * i), str(i)) for i in range(count)]
        resp = _create(_payload(points=points))
        assert resp.status_code == 201
        assert len(resp.json()["points"]) == count

    def test_original_strings_echoed_verbatim(self, calibration_repo):
        points = [("4.000", "0"), ("8", "5.0000"), ("12.00000", "10")]
        body = _create_curve(points=points)
        assert [(p["raw_signal"], p["reference_moisture_pct"]) for p in body["points"]] == points

    def test_sensor_id_whitespace_stripped(self, calibration_repo):
        body = _create_curve(sensor_id="  S-1  ")
        assert body["sensor_id"] == "S-1"

    def test_curve_rebuild_still_readable_from_same_file(self, calibration_repo, tmp_path):
        body = _create_curve()
        rebuilt = CalibrationCurveRepository(tmp_path / "calibration.db")
        record = rebuilt.get(body["curve_no"])
        assert record.sensor_id == "SENSOR-A1"
        assert [(p.raw_signal, p.reference_moisture_pct) for p in record.points] == _points()


class TestCreateValidation:
    @pytest.mark.parametrize("count", [2, 9])
    def test_point_count_out_of_bounds_returns_422_and_persists_nothing(
        self, calibration_repo, count
    ):
        points = [(str(i), str(i)) for i in range(count)]
        resp = _create(_payload(points=points))
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail[0]["field"] in {"raw_signals", "reference_moisture_pct"}
        assert calibration_repo.count_curves() == 0

    @pytest.mark.parametrize(
        ("signals", "bad_index"),
        [
            (("4.0", "4.0", "12.0"), 1),
            (("4.0", "8.0", "8.0"), 2),
            (("12.0", "8.0", "4.0"), 1),
        ],
    )
    def test_duplicate_or_descending_signal_rejected_and_not_persisted(
        self, calibration_repo, signals, bad_index
    ):
        points = list(zip(signals, ("0", "5", "10")))
        resp = _create(_payload(points=points))
        assert resp.status_code == 422
        detail = resp.json()["detail"][0]
        assert detail["field"] == "raw_signals"
        assert detail["type"] == "raw_signals_not_strictly_increasing"
        assert str(bad_index) in detail["message"]
        assert calibration_repo.count_curves() == 0

    @pytest.mark.parametrize(
        ("moisture", "err_type"),
        [
            (("0", "5", "5"), "reference_moisture_not_strictly_increasing"),
            (("10", "5", "0"), "reference_moisture_not_strictly_increasing"),
            (("0", "5", "40.0001"), "reference_moisture_out_of_range"),
            (("-0.0001", "5", "10"), "reference_moisture_out_of_range"),
            (("0", "5", "40.0"), None),  # 端点 40 合法
        ],
    )
    def test_reference_moisture_validation(self, calibration_repo, moisture, err_type):
        signals = ("4.0", "8.0", "12.0")
        resp = _create(_payload(points=list(zip(signals, moisture))))
        if err_type is None:
            assert resp.status_code == 201
            return
        assert resp.status_code == 422
        detail = resp.json()["detail"][0]
        assert detail["field"] == "reference_moisture_pct"
        assert detail["type"] == err_type
        assert calibration_repo.count_curves() == 0

    def test_reference_zero_endpoint_accepted(self, calibration_repo):
        body = _create_curve(points=[("4", "0"), ("8", "5"), ("12", "10")])
        assert body["points"][0]["reference_moisture_pct"] == "0"

    def test_point_count_mismatch_returns_422_and_persists_nothing(self, calibration_repo):
        payload = {
            "sensor_id": "S",
            "raw_signals": ["4.0", "8.0", "12.0", "16.0"],
            "reference_moisture_pct": ["0", "5", "10"],
        }
        resp = _create(payload)
        assert resp.status_code == 422
        detail = resp.json()["detail"][0]
        assert detail["field"] == "reference_moisture_pct"
        assert detail["type"] == "point_count_mismatch"
        assert calibration_repo.count_curves() == 0

    @pytest.mark.parametrize("extra", ["operator", "coefficients", "points"])
    def test_unknown_field_rejected_at_top_level(self, calibration_repo, extra):
        payload = _payload()
        payload[extra] = "x"
        resp = _create(payload)
        assert resp.status_code == 422
        detail = resp.json()["detail"][0]
        assert detail["field"] == extra
        assert detail["type"] == "extra_forbidden"
        assert calibration_repo.count_curves() == 0

    @pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
    def test_non_finite_signal_rejected(self, calibration_repo, value):
        payload = _payload(points=[("4.0", "0"), ("8.0", "5"), ("12.0", "10")])
        payload["raw_signals"][1] = value
        resp = _create(payload)
        assert resp.status_code == 422
        detail = resp.json()["detail"][0]
        assert detail["field"] == "raw_signals[1]"
        assert detail["type"] == "finite_number"
        assert calibration_repo.count_curves() == 0

    def test_empty_sensor_id_rejected(self, calibration_repo):
        resp = _create(_payload(sensor_id="   "))
        assert resp.status_code == 422
        assert resp.json()["detail"][0]["field"] == "sensor_id"


class TestConvert:
    @pytest.mark.parametrize(
        "signal",
        ["4.0", "8.0", "12.0", "16.0", "20.0", "5.0", "10.0", "15.1234", "19.9995"],
    )
    def test_interpolation_matches_independent_fraction_recalculation(
        self, calibration_repo, signal
    ):
        points = _points()
        curve = _create_curve(points=points)
        resp = client.post(CONVERT_URL, json={"curve_no": curve["curve_no"], "raw_signal": signal})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        expected_moisture, (lower, upper) = _independently_interpolate(points, signal)
        assert body["moisture_pct"] == expected_moisture
        assert body["raw_signal"] == signal
        assert body["curve_no"] == curve["curve_no"]
        interval = body["interval"]
        assert interval["lower_signal"] == lower
        assert interval["upper_signal"] == upper
        # 区间端点参考含水率回显
        point_map = dict(points)
        assert interval["lower_moisture_pct"] == point_map[lower]
        assert interval["upper_moisture_pct"] == point_map[upper]

    @pytest.mark.parametrize(
        ("signal", "expected"),
        [("4.0", "0.000"), ("8.0", "5.000"), ("12.0", "10.000"), ("20.0", "40.000")],
    )
    def test_endpoint_input_returns_reference_directly(
        self, calibration_repo, signal, expected
    ):
        curve = _create_curve()
        resp = client.post(CONVERT_URL, json={"curve_no": curve["curve_no"], "raw_signal": signal})
        assert resp.status_code == 200
        assert resp.json()["moisture_pct"] == expected

    def test_first_endpoint_interval_is_first_pair(self, calibration_repo):
        curve = _create_curve()
        resp = client.post(
            CONVERT_URL, json={"curve_no": curve["curve_no"], "raw_signal": "4.0"}
        )
        interval = resp.json()["interval"]
        assert (interval["lower_signal"], interval["upper_signal"]) == ("4.0", "8.0")

    def test_interior_endpoint_interval_is_right_pair(self, calibration_repo):
        curve = _create_curve()
        resp = client.post(
            CONVERT_URL, json={"curve_no": curve["curve_no"], "raw_signal": "8.0"}
        )
        interval = resp.json()["interval"]
        assert (interval["lower_signal"], interval["upper_signal"]) == ("8.0", "12.0")

    def test_half_up_rounding_at_fourth_decimal(self, calibration_repo):
        # 构造使插值恰为 1.2345 的信号：区间 (0,0)–(2,2.469)，x=1 → 1.2345 → 1.235
        points = [("0", "0"), ("2", "2.469"), ("4", "4")]
        curve = _create_curve(points=points)
        resp = client.post(CONVERT_URL, json={"curve_no": curve["curve_no"], "raw_signal": "1"})
        assert resp.status_code == 200
        assert resp.json()["moisture_pct"] == "1.235"

    def test_unknown_curve_returns_structured_404(self, calibration_repo):
        resp = client.post(
            CONVERT_URL, json={"curve_no": "CC19990101-00000000", "raw_signal": "10"}
        )
        assert resp.status_code == 404
        detail = resp.json()["detail"]
        assert isinstance(detail, list)
        assert detail[0]["field"] == "curve_no"
        assert detail[0]["type"] == "curve_not_found"
        assert "CC19990101-00000000" in detail[0]["message"]
        # 失败不产生记录
        assert calibration_repo.count_curves() == 0

    @pytest.mark.parametrize("signal", ["3.9999", "20.0001", "-1", "100"])
    def test_out_of_range_signal_returns_422_located_at_raw_signal(
        self, calibration_repo, signal
    ):
        curve = _create_curve()
        count_before = calibration_repo.count_points(curve["curve_no"])
        resp = client.post(
            CONVERT_URL, json={"curve_no": curve["curve_no"], "raw_signal": signal}
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"][0]
        assert detail["field"] == "raw_signal"
        assert detail["type"] == "raw_signal_out_of_range"
        assert signal in detail["message"]
        # 换算为只读：点数/曲线数不变
        assert calibration_repo.count_points(curve["curve_no"]) == count_before
        assert calibration_repo.count_curves() == 1

    def test_unknown_curve_takes_404_priority_over_range(self, calibration_repo):
        # 曲线不存在先于信号越界判断：即使信号超出该样例范围，仍返回 404
        resp = client.post(
            CONVERT_URL, json={"curve_no": "CC19990101-00000000", "raw_signal": "999"}
        )
        assert resp.status_code == 404
        assert resp.json()["detail"][0]["type"] == "curve_not_found"

    def test_unknown_field_in_convert_rejected(self, calibration_repo):
        curve = _create_curve()
        resp = client.post(
            CONVERT_URL,
            json={"curve_no": curve["curve_no"], "raw_signal": "10", "extrapolate": True},
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"][0]
        assert detail["field"] == "extrapolate"
        assert detail["type"] == "extra_forbidden"

    def test_non_finite_convert_signal_returns_422(self, calibration_repo):
        curve = _create_curve()
        resp = client.post(
            CONVERT_URL, json={"curve_no": curve["curve_no"], "raw_signal": "Infinity"}
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"][0]
        assert detail["field"] == "raw_signal"
        assert detail["type"] == "finite_number"


class TestExistingContractsUnchanged:
    """新增校准模块后，修正单与取样批次契约保持兼容。"""

    def test_health_still_ok(self):
        assert client.get("/health").json() == {"status": "ok"}

    def test_sampling_batch_still_created_in_shared_repository(self, tmp_path):
        # 同一 SQLite 文件上两个仓储并存，互不干扰
        from app.sampling_repository import SamplingBatchRepository

        db_path = tmp_path / "shared.db"
        sampling = SamplingBatchRepository(db_path)
        calibration = CalibrationCurveRepository(db_path)
        main.app.dependency_overrides[main.get_sampling_repository] = lambda: sampling
        main.app.dependency_overrides[main.get_calibration_repository] = lambda: calibration
        try:
            batch_resp = client.post(
                "/api/v1/moisture-batches",
                json={
                    "pile_name": "兼容砂堆",
                    "readings": [
                        {"wet_sample_mass": "210", "dry_sample_mass": "200"},
                        {"wet_sample_mass": "206", "dry_sample_mass": "200"},
                    ],
                },
            )
            assert batch_resp.status_code == 201
            curve_resp = _create()
            assert curve_resp.status_code == 201
            assert sampling.count_batches() == 1
            assert calibration.count_curves() == 1
        finally:
            main.app.dependency_overrides.clear()
