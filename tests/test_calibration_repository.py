"""校准曲线仓储测试：SQLite 整批事务、原样落库、重建读取、不可变触发器与共库迁移。"""
import sqlite3

import pytest

from app.calibration_repository import (
    CalibrationCurveRepository,
    CalibrationCurveRecord,
    CurveNotFoundError,
    StoredCalibrationPoint,
    default_curve_no_factory,
)

POINTS_3 = [("4.0", "0"), ("12.0", "10"), ("20.0", "40")]


@pytest.fixture
def repo(tmp_path):
    return CalibrationCurveRepository(tmp_path / "calibration.db")


@pytest.fixture
def created(repo):
    return repo.create("SENSOR-A1", POINTS_3)


class TestCurveNoFactory:
    def test_default_format(self):
        curve_no = default_curve_no_factory()
        # "CC" + 8 位日期 + "-" + 8 位大写十六进制
        assert len(curve_no) == 2 + 8 + 1 + 8
        assert curve_no.startswith("CC")
        assert curve_no[10] == "-"
        hex_part = curve_no[11:]
        assert hex_part == hex_part.upper()
        int(hex_part, 16)

    def test_collision_retries_with_another_id(self, tmp_path):
        sequence = iter(["CC20260914-AAAAAAAA", "CC20260914-BBBBBBBB"])
        r = CalibrationCurveRepository(
            tmp_path / "m.db", curve_no_factory=lambda: next(sequence)
        )
        first = r.create("A", POINTS_3)
        second = r.create("B", POINTS_3)
        assert first.curve_no == "CC20260914-AAAAAAAA"
        assert second.curve_no == "CC20260914-BBBBBBBB"
        assert r.count_curves() == 2


class TestCreate:
    def test_persists_curve_with_points(self, created):
        assert created.curve_no.startswith("CC")
        assert created.sensor_id == "SENSOR-A1"
        assert created.created_at
        assert [
            (p.index, p.raw_signal, p.reference_moisture_pct) for p in created.points
        ] == [(0, "4.0", "0"), (1, "12.0", "10"), (2, "20.0", "40")]

    @pytest.mark.parametrize("count", [3, 8])
    def test_point_count_bounds_accepted(self, repo, count):
        points = [(str(i), str(i)) for i in range(count)]
        record = repo.create("S", points)
        assert len(record.points) == count

    @pytest.mark.parametrize("count", [2, 9])
    def test_point_count_guard(self, repo, count):
        with pytest.raises(ValueError):
            repo.create("S", [(str(i), str(i)) for i in range(count)])
        assert repo.count_curves() == 0

    def test_original_strings_stored_verbatim(self, repo):
        points = [
            ("1.000000000000000000001", "0"),
            ("2", "10.0000"),
            ("300", "40"),
        ]
        record = repo.create("高精度探头", points)
        assert [(p.raw_signal, p.reference_moisture_pct) for p in record.points] == points

    def test_curve_and_points_inserted_atomically(self, tmp_path):
        # 以子类在对照点插入中途注入失败（与取样仓储测试同一手法）：
        # 第一条对照点成功后第二条抛错，主行与已插入的点必须随事务整体回滚
        real_insert = CalibrationCurveRepository._insert_points

        def failing_insert(conn, curve_no, points):
            real_insert(conn, curve_no, points[:1])  # 先成功写第一行
            raise RuntimeError("模拟对照点写入中途失败")

        class FailOnSecondPoint(CalibrationCurveRepository):
            _insert_points = staticmethod(failing_insert)

        failing_repo = FailOnSecondPoint(tmp_path / "fail.db")
        with pytest.raises(RuntimeError):
            failing_repo.create("半条曲线", POINTS_3)
        assert failing_repo.count_curves() == 0
        with sqlite3.connect(tmp_path / "fail.db") as raw:
            assert raw.execute("SELECT COUNT(*) FROM calibration_points").fetchone()[0] == 0
            assert raw.execute("SELECT COUNT(*) FROM calibration_curves").fetchone()[0] == 0

    def test_failed_create_leaves_prior_curve_intact(self, tmp_path):
        repo = CalibrationCurveRepository(tmp_path / "atomic2.db")
        good = repo.create("好曲线", POINTS_3)

        def boom(*args, **kwargs):
            raise RuntimeError("炸")

        class FailOnPoints(CalibrationCurveRepository):
            _insert_points = staticmethod(boom)

        failing_repo = FailOnPoints(tmp_path / "atomic2.db")
        with pytest.raises(RuntimeError):
            failing_repo.create("坏曲线", POINTS_3)
        assert repo.get(good.curve_no).sensor_id == "好曲线"
        assert repo.count_curves() == 1


class TestGet:
    def test_get_missing_raises_404(self, repo):
        with pytest.raises(CurveNotFoundError):
            repo.get("CC19990101-DEADBEEF")

    def test_count_points_missing_raises_404(self, repo):
        with pytest.raises(CurveNotFoundError):
            repo.count_points("CC19990101-DEADBEEF")

    def test_hydrated_record_types(self, created):
        assert isinstance(created, CalibrationCurveRecord)
        assert all(isinstance(p, StoredCalibrationPoint) for p in created.points)


class TestImmutability:
    def test_repository_exposes_no_mutation_api(self, repo):
        mutating = [
            name
            for name in dir(repo)
            if not name.startswith("_") and name in {"update", "delete", "save", "confirm"}
        ]
        assert mutating == []

    @pytest.mark.parametrize(
        "sql",
        [
            "UPDATE calibration_curves SET sensor_id='X'",
            "DELETE FROM calibration_curves",
            "UPDATE calibration_points SET raw_signal='9'",
            "DELETE FROM calibration_points",
        ],
    )
    def test_sqlite_triggers_block_direct_mutation(self, repo, created, tmp_path, sql):
        with sqlite3.connect(tmp_path / "calibration.db") as raw:
            with pytest.raises(sqlite3.IntegrityError):
                raw.execute(sql)
            raw.rollback()
        # 触发器 ABORT 后记录原样可读
        assert repo.get(created.curve_no) == created

    def test_record_intact_after_blocked_mutations(self, repo, created, tmp_path):
        with sqlite3.connect(tmp_path / "calibration.db") as raw:
            for sql in [
                "UPDATE calibration_curves SET sensor_id='X'",
                "DELETE FROM calibration_curves",
                "UPDATE calibration_points SET raw_signal='9'",
                "DELETE FROM calibration_points",
            ]:
                with pytest.raises(sqlite3.IntegrityError):
                    raw.execute(sql)
                raw.rollback()
        record = repo.get(created.curve_no)
        assert record == created


class TestPersistence:
    def test_rebuilt_repository_reads_same_curve(self, repo, created, tmp_path):
        rebuilt = CalibrationCurveRepository(tmp_path / "calibration.db")
        record = rebuilt.get(created.curve_no)
        assert record == created

    def test_close_is_idempotent(self, repo):
        repo.close()
        repo.close()


class TestCoexistWithSamplingDatabase:
    def test_migrates_into_existing_sampling_file_without_touching_batches(
        self, tmp_path
    ):
        from app.sampling_repository import SamplingBatchRepository

        db_path = tmp_path / "shared.db"
        sampling = SamplingBatchRepository(db_path)
        batch = sampling.create(
            "取样堆", [("210", "200"), ("206", "200")]
        )
        calibration = CalibrationCurveRepository(db_path)
        curve = calibration.create("共库传感器", POINTS_3)

        # 两边仓储在同一文件上互不干扰
        assert sampling.get(batch.batch_no).pile_name == "取样堆"
        assert calibration.get(curve.curve_no).sensor_id == "共库传感器"
        # 再次初始化（两个仓储都重建）幂等
        SamplingBatchRepository(db_path)
        CalibrationCurveRepository(db_path)
        assert sampling.count_batches() == 1
        assert calibration.count_curves() == 1
