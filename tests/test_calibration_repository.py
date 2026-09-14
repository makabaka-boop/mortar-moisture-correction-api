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

# 引入 sealed 之前的线上版本：四触发器命名，且 UPDATE 触发器无条件 ABORT
LEGACY_V1_TRIGGERS = """
CREATE TRIGGER trg_calibration_curves_no_update
BEFORE UPDATE ON calibration_curves
BEGIN
    SELECT RAISE(ABORT, '校准曲线为不可变记录，禁止更新');
END;
CREATE TRIGGER trg_calibration_curves_no_delete
BEFORE DELETE ON calibration_curves
BEGIN
    SELECT RAISE(ABORT, '校准曲线为不可变记录，禁止删除');
END;
CREATE TRIGGER trg_calibration_points_no_update
BEFORE UPDATE ON calibration_points
BEGIN
    SELECT RAISE(ABORT, '校准曲线为不可变记录，对照点禁止更新');
END;
CREATE TRIGGER trg_calibration_points_no_delete
BEFORE DELETE ON calibration_points
BEGIN
    SELECT RAISE(ABORT, '校准曲线为不可变记录，对照点禁止删除');
END;
"""


def _create_v1_database(db_path, curves):
    """按引入 sealed 之前的线上版本造库：旧两表 + 四个旧触发器 + 给定曲线。"""
    from app.calibration_repository import LEGACY_SCHEMA

    with sqlite3.connect(db_path) as raw:
        raw.executescript(LEGACY_SCHEMA)
        raw.executescript(LEGACY_V1_TRIGGERS)
        for index, (curve_no, sensor_id, points) in enumerate(curves):
            raw.execute(
                "INSERT INTO calibration_curves (curve_no, sensor_id, created_at)"
                " VALUES (?, ?, ?)",
                (curve_no, sensor_id, f"2026-09-13T0{index}:00:00+00:00"),
            )
            raw.executemany(
                "INSERT INTO calibration_points"
                " (curve_no, ordinal, raw_signal, reference_moisture_pct)"
                " VALUES (?, ?, ?, ?)",
                [(curve_no, i, signal, moisture) for i, (signal, moisture) in enumerate(points)],
            )
        raw.commit()

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
            "UPDATE calibration_curves SET sealed=0",
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

    def test_direct_append_after_seal_blocked_and_conversion_basis_unchanged(
        self, repo, created, tmp_path
    ):
        """绕过仓储向已固化曲线追加下一个 ordinal 的点：触发器拒绝，点数不变。

        这是本次漏洞：外键满足、主键不冲突的新点此前可以直接 INSERT，
        固化的换算依据随之改变。
        """
        with sqlite3.connect(tmp_path / "calibration.db") as raw:  # 默认不启用外键
            with pytest.raises(sqlite3.IntegrityError):
                raw.execute(
                    "INSERT INTO calibration_points"
                    " (curve_no, ordinal, raw_signal, reference_moisture_pct)"
                    " VALUES (?, 3, '999.0', '40')",
                    (created.curve_no,),
                )
        assert repo.count_points(created.curve_no) == len(created.points)
        # 换算依据逐位不变
        assert repo.get(created.curve_no) == created

    def test_orphan_point_insert_blocked_when_foreign_keys_off(
        self, repo, created, tmp_path
    ):
        """外键未启用时，向不存在的曲线插点同样被触发器拒绝（COALESCE → 已固化）。"""
        with sqlite3.connect(tmp_path / "calibration.db") as raw:
            with pytest.raises(sqlite3.IntegrityError):
                raw.execute(
                    "INSERT INTO calibration_points"
                    " (curve_no, ordinal, raw_signal, reference_moisture_pct)"
                    " VALUES ('CC-NOT-EXISTS', 0, '1', '2')"
                )

    def test_seal_update_failure_rolls_back_entire_curve(self, tmp_path):
        """固化更新（sealed 0 → 1）失败：未固化主行与已插入点随事务整体回滚。"""
        class _FailingConnection:
            """仅拦截固化 UPDATE 的连接代理，其余方法委托真实 sqlite 连接。"""

            def __init__(self, real):
                self._real = real

            def execute(self, sql, params=()):
                if sql.startswith("UPDATE calibration_curves SET sealed=1"):
                    raise RuntimeError("模拟固化阶段失败")
                return self._real.execute(sql, params)

            def __getattr__(self, name):
                return getattr(self._real, name)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return self._real.__exit__(*exc)

        class FailOnSeal(CalibrationCurveRepository):
            def _connect(self):
                return _FailingConnection(super()._connect())

        failing_repo = FailOnSeal(tmp_path / "sealfail.db")
        with pytest.raises(RuntimeError):
            failing_repo.create("封不住曲线", POINTS_3)
        with sqlite3.connect(tmp_path / "sealfail.db") as raw:
            assert raw.execute("SELECT COUNT(*) FROM calibration_curves").fetchone()[0] == 0
            assert raw.execute("SELECT COUNT(*) FROM calibration_points").fetchone()[0] == 0

    def test_record_intact_after_blocked_mutations(self, repo, created, tmp_path):
        with sqlite3.connect(tmp_path / "calibration.db") as raw:
            statements = [
                "UPDATE calibration_curves SET sensor_id='X'",
                "UPDATE calibration_curves SET sealed=0",
                "DELETE FROM calibration_curves",
                "UPDATE calibration_points SET raw_signal='9'",
                "DELETE FROM calibration_points",
                (
                    "INSERT INTO calibration_points"
                    " (curve_no, ordinal, raw_signal, reference_moisture_pct)"
                    f" VALUES ('{created.curve_no}', 3, '9', '40')"
                ),
            ]
            for sql in statements:
                with pytest.raises(sqlite3.IntegrityError):
                    raw.execute(sql)
                raw.rollback()
        record = repo.get(created.curve_no)
        assert record == created


class TestPersistence:
    def test_curve_is_sealed_immediately_after_commit(self, repo, created, tmp_path):
        with sqlite3.connect(tmp_path / "calibration.db") as raw:
            sealed = raw.execute(
                "SELECT sealed FROM calibration_curves WHERE curve_no=?",
                (created.curve_no,),
            ).fetchone()[0]
        assert sealed == 1

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


class TestLegacyDatabaseMigration:
    """引入 sealed 之前的校准库：补 sealed=1，既有曲线立即不可追加。"""

    def test_legacy_curve_gets_sealed_and_cannot_be_appended(self, tmp_path):
        from app.calibration_repository import LEGACY_SCHEMA

        db_path = tmp_path / "legacy_cal.db"
        curve_no = "CC20260913-LEGACY01"
        with sqlite3.connect(db_path) as raw:
            raw.executescript(LEGACY_SCHEMA)
            raw.execute(
                "INSERT INTO calibration_curves (curve_no, sensor_id, created_at)"
                " VALUES (?, '旧探头', '2026-09-13T00:00:00+00:00')",
                (curve_no,),
            )
            raw.executemany(
                "INSERT INTO calibration_points"
                " (curve_no, ordinal, raw_signal, reference_moisture_pct)"
                " VALUES (?, ?, ?, ?)",
                [
                    (curve_no, 0, "1", "0"),
                    (curve_no, 1, "2", "5"),
                    (curve_no, 2, "3", "40"),
                ],
            )
            raw.commit()

        repo = CalibrationCurveRepository(db_path)  # 触发迁移与触发器重建
        with sqlite3.connect(db_path) as raw:
            assert raw.execute(
                "SELECT sealed FROM calibration_curves WHERE curve_no=?", (curve_no,)
            ).fetchone()[0] == 1
            # 外键默认关闭，触发器也必须拦住对旧曲线的追加
            with pytest.raises(sqlite3.IntegrityError):
                raw.execute(
                    "INSERT INTO calibration_points"
                    " (curve_no, ordinal, raw_signal, reference_moisture_pct)"
                    " VALUES (?, 3, '4', '40')",
                    (curve_no,),
                )
        record = repo.get(curve_no)
        assert record.sensor_id == "旧探头"
        assert len(record.points) == 3
        # 迁移后新建曲线仍走 0 → 1 固化流程，正常可读
        new_curve = repo.create("新探头", POINTS_3)
        assert new_curve.points[0].raw_signal == "4.0"


class TestV1DatabaseUpgradeWithTriggers:
    """带旧触发器（无条件拒绝 UPDATE）的线上 v1 库：升级必须先清旧触发器再回填。"""

    def test_upgrade_opens_existing_curves_and_backfills_sealed(self, tmp_path):
        db_path = tmp_path / "v1.db"
        curve_no = "CC20260913-OLD00001"
        _create_v1_database(
            db_path,
            [(curve_no, "旧探头A", [("1", "0"), ("2", "5"), ("3", "40")])],
        )

        # 回归：旧顺序（先回填 sealed）会被旧 UPDATE 触发器 ABORT，仓储构造失败
        repo = CalibrationCurveRepository(db_path)
        record = repo.get(curve_no)
        assert record.sensor_id == "旧探头A"
        assert [(p.raw_signal, p.reference_moisture_pct) for p in record.points] == [
            ("1", "0"), ("2", "5"), ("3", "40"),
        ]
        with sqlite3.connect(db_path) as raw:
            assert raw.execute(
                "SELECT sealed FROM calibration_curves WHERE curve_no=?", (curve_no,)
            ).fetchone()[0] == 1

    def test_upgraded_v1_curves_remain_immutable(self, tmp_path):
        db_path = tmp_path / "v1_immutable.db"
        curve_no = "CC20260913-OLD00002"
        _create_v1_database(
            db_path,
            [(curve_no, "旧探头B", [("1", "0"), ("2", "5"), ("3", "40")])],
        )
        repo = CalibrationCurveRepository(db_path)

        with sqlite3.connect(db_path) as raw:  # 默认不启用外键
            blocked_statements = [
                ("UPDATE calibration_curves SET sensor_id='X'", ()),
                ("UPDATE calibration_curves SET sealed=0 WHERE curve_no=?", (curve_no,)),
                ("DELETE FROM calibration_curves WHERE curve_no=?", (curve_no,)),
                ("UPDATE calibration_points SET raw_signal='9'", ()),
                ("DELETE FROM calibration_points", ()),
                (
                    "INSERT INTO calibration_points"
                    " (curve_no, ordinal, raw_signal, reference_moisture_pct)"
                    " VALUES (?, 3, '9', '40')",
                    (curve_no,),
                ),
            ]
            for sql, params in blocked_statements:
                with pytest.raises(sqlite3.IntegrityError):
                    raw.execute(sql, params)
                raw.rollback()
        assert len(repo.get(curve_no).points) == 3

    def test_upgrade_multiple_curves_then_create_and_reopen(self, tmp_path):
        db_path = tmp_path / "v1_multi.db"
        curves = [
            ("CC20260913-OLD00001", "旧探头1", [("1", "0"), ("2", "5"), ("3", "40")]),
            ("CC20260913-OLD00002", "旧探头2", [("4", "0"), ("8", "5"), ("20", "40")]),
        ]
        _create_v1_database(db_path, curves)
        repo = CalibrationCurveRepository(db_path)
        assert repo.count_curves() == 2
        for curve_no, sensor_id, points in curves:
            record = repo.get(curve_no)
            assert record.sensor_id == sensor_id
            assert [(p.raw_signal, p.reference_moisture_pct) for p in record.points] == points

        # 升级后新建曲线走 0 → 1 固化流程，正常写入且立即固化
        new_curve = repo.create("升级后新探头", POINTS_3)
        assert repo.count_points(new_curve.curve_no) == len(POINTS_3)
        with sqlite3.connect(db_path) as raw:
            assert raw.execute(
                "SELECT sealed FROM calibration_curves WHERE curve_no=?",
                (new_curve.curve_no,),
            ).fetchone()[0] == 1

        # 重复打开（初始化再跑一遍）幂等，旧曲线仍可读
        CalibrationCurveRepository(db_path)
        assert repo.get("CC20260913-OLD00001").sensor_id == "旧探头1"
