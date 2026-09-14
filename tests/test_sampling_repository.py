"""取样批次仓储测试：SQLite 持久化、整批事务、条件确认、重建读取。"""
import sqlite3

import pytest

from app.sampling_repository import (
    BatchAlreadyConfirmedError,
    BatchNotFoundError,
    InvalidReadingIndexError,
    LEGACY_SCHEMA,
    ReadingIndexOutOfBoundsError,
    RevisionConflictError,
    SamplingBatchRepository,
    SamplingBatchRecord,
    StoredReading,
    default_batch_no_factory,
    parse_reading_index,
)

READINGS_3 = [("500.12", "477.72"), ("480.00", "458.50"), ("512.345", "480.005")]


@pytest.fixture
def repo(tmp_path):
    return SamplingBatchRepository(tmp_path / "moisture.db")


@pytest.fixture
def created(repo):
    return repo.create("雨后1号砂堆", READINGS_3)


class TestBatchNoFactory:
    def test_default_format(self):
        batch_no = default_batch_no_factory()
        # "MC" + 8 位日期 + "-" + 8 位大写十六进制
        assert len(batch_no) == 2 + 8 + 1 + 8
        assert batch_no.startswith("MC")
        assert batch_no[10] == "-"
        hex_part = batch_no[11:]
        assert hex_part == hex_part.upper()
        int(hex_part, 16)  # 合法十六进制

    def test_collision_retries_with_another_id(self, tmp_path):
        sequence = iter(["MC20260914-AAAAAAAA", "MC20260914-BBBBBBBB"])
        r = SamplingBatchRepository(tmp_path / "m.db", batch_no_factory=lambda: next(sequence))
        first = r.create("堆A", READINGS_3[:2])
        second = r.create("堆B", READINGS_3[:2])
        assert first.batch_no == "MC20260914-AAAAAAAA"
        assert second.batch_no == "MC20260914-BBBBBBBB"
        assert r.count_batches() == 2


class TestCreate:
    def test_persists_pending_with_original_readings(self, created):
        assert created.status == "待确认"
        assert created.revision_no == 0
        assert created.pile_name == "雨后1号砂堆"
        assert created.representative_moisture_pct is None
        assert created.confirmed_at is None
        assert created.created_at  # 非空时间戳
        assert [
            (r.wet_sample_mass, r.dry_sample_mass, r.moisture_pct) for r in created.readings
        ] == [(w, d, None) for w, d in READINGS_3]
        assert [r.index for r in created.readings] == [0, 1, 2]

    def test_original_mass_strings_stored_verbatim(self, repo):
        precise = [("100.000000000000000000001", "99.9"), ("100", "98.0000")]
        rec = repo.create("高精度堆", precise)
        got = [(r.wet_sample_mass, r.dry_sample_mass) for r in rec.readings]
        assert got == precise  # 原样字符串，不规范化

    def test_reading_count_guard(self, repo):
        with pytest.raises(ValueError):
            repo.create("堆", [("100", "90")])
        with pytest.raises(ValueError):
            repo.create("堆", [("100", "90")] * 6)
        assert repo.count_batches() == 0

    def test_batch_and_readings_inserted_atomically(self, repo, tmp_path, monkeypatch):
        real_insert = SamplingBatchRepository._insert_readings

        def failing_insert(self, conn, batch_no, readings):
            real_insert(conn, batch_no, readings[:1])  # 先成功写第一行
            raise RuntimeError("模拟写入中途失败")

        monkeypatch.setattr(SamplingBatchRepository, "_insert_readings", failing_insert)
        with pytest.raises(RuntimeError):
            repo.create("半批堆", READINGS_3)
        assert repo.count_batches() == 0  # 主行随事务回滚
        with sqlite3.connect(tmp_path / "moisture.db") as raw:
            assert raw.execute("SELECT COUNT(*) FROM sampling_readings").fetchone()[0] == 0

    def test_failed_create_leaves_prior_batch_intact(self, tmp_path, monkeypatch):
        repo = SamplingBatchRepository(tmp_path / "atomic2.db")
        good = repo.create("好堆", READINGS_3[:2])
        monkeypatch.setattr(
            SamplingBatchRepository,
            "_insert_readings",
            staticmethod(lambda *a, **k: (_ for _ in ()).throw(RuntimeError("炸"))),
        )
        with pytest.raises(RuntimeError):
            repo.create("坏堆", READINGS_3)
        assert repo.get(good.batch_no).pile_name == "好堆"  # 已提交批次不受影响


class TestConfirm:
    PCTS = ["4.68893912752239805744", "4.68920392584514721919", "6.73742981843939125634"]

    def test_confirm_writes_status_results_and_timestamp(self, repo, created):
        confirmed = repo.confirm(created.batch_no, self.PCTS, "4.689")
        assert confirmed.status == "已确认"
        assert confirmed.representative_moisture_pct == "4.689"
        assert confirmed.confirmed_at
        assert confirmed.confirmed_at >= created.created_at
        assert confirmed.revision_no == 0
        assert [r.moisture_pct for r in confirmed.readings] == self.PCTS
        # 原始读数不动
        assert [(r.wet_sample_mass, r.dry_sample_mass) for r in confirmed.readings] == READINGS_3

    def test_repeat_confirm_raises_409_and_does_not_overwrite(self, repo, created):
        repo.confirm(created.batch_no, self.PCTS, "4.689")
        with pytest.raises(BatchAlreadyConfirmedError):
            repo.confirm(created.batch_no, ["9"] * 3, "9.000")
        rec = repo.get(created.batch_no)
        assert rec.status == "已确认"
        assert rec.representative_moisture_pct == "4.689"  # 旧结果原封不动
        assert [r.moisture_pct for r in rec.readings] == self.PCTS

    def test_result_provider_accepts_revised_latest_readings(self, repo, created):
        repo.revise_reading(created.batch_no, 0, "510", "500", expected_revision_no=0)

        def provider(readings):
            assert readings[0].wet_sample_mass == "510"
            return ["2", "5", "7"], "5.000"

        confirmed = repo.confirm(
            created.batch_no, result_provider=provider
        )
        assert confirmed.status == "已确认"
        assert [r.moisture_pct for r in confirmed.readings] == ["2", "5", "7"]
        assert confirmed.representative_moisture_pct == "5.000"
        assert confirmed.revision_no == 1

    def test_confirm_missing_batch_raises_404(self, repo):
        with pytest.raises(BatchNotFoundError):
            repo.confirm("MC19990101-DEADBEEF", ["1", "2"], "1.500")
        assert repo.count_batches() == 0

    def test_get_missing_raises_404(self, repo):
        with pytest.raises(BatchNotFoundError):
            repo.get("不存在")


class TestReadingIndexParsing:
    @pytest.mark.parametrize(("value", "expected"), [("0", 0), ("-1", -1), ("12", 12)])
    def test_decimal_integer_path_value(self, value, expected):
        assert parse_reading_index(value) == expected

    @pytest.mark.parametrize("value", ["abc", "1.0", "0x1", "+1", " 1"])
    def test_invalid_path_value(self, value):
        with pytest.raises(InvalidReadingIndexError):
            parse_reading_index(value)


class TestReviseReading:
    def test_replaces_reading_increments_revision_and_writes_audit(self, repo, created):
        revised = repo.revise_reading(
            created.batch_no, 1, "490.00", "470.00", expected_revision_no=0
        )

        assert revised.status == "待确认"
        assert revised.revision_no == 1
        assert [(r.wet_sample_mass, r.dry_sample_mass) for r in revised.readings] == [
            ("500.12", "477.72"),
            ("490.00", "470.00"),
            ("512.345", "480.005"),
        ]
        assert all(r.moisture_pct is None for r in revised.readings)

        audits = repo.list_reading_revisions(created.batch_no)
        assert len(audits) == 1
        audit = audits[0]
        assert audit.index == 1
        assert (
            audit.old_wet_sample_mass,
            audit.old_dry_sample_mass,
            audit.new_wet_sample_mass,
            audit.new_dry_sample_mass,
        ) == ("480.00", "458.50", "490.00", "470.00")
        assert (audit.previous_revision_no, audit.new_revision_no) == (0, 1)
        assert audit.revised_at

    def test_second_revision_requires_latest_revision_number(self, repo, created):
        repo.revise_reading(created.batch_no, 0, "501", "478", expected_revision_no=0)
        revised = repo.revise_reading(
            created.batch_no, 2, "513", "481", expected_revision_no=1
        )
        assert revised.revision_no == 2
        assert [
            (a.previous_revision_no, a.new_revision_no, a.index)
            for a in repo.list_reading_revisions(created.batch_no)
        ] == [(0, 1, 0), (1, 2, 2)]

    def test_stale_revision_conflicts_and_changes_nothing(self, repo, created):
        before = repo.get(created.batch_no)
        with pytest.raises(RevisionConflictError):
            repo.revise_reading(created.batch_no, 0, "501", "478", expected_revision_no=1)

        after = repo.get(created.batch_no)
        assert after == before
        assert repo.list_reading_revisions(created.batch_no) == ()

    def test_confirmed_batch_cannot_be_revised(self, repo, created):
        repo.confirm(created.batch_no, TestConfirm.PCTS, "4.689")
        with pytest.raises(BatchAlreadyConfirmedError):
            repo.revise_reading(created.batch_no, 0, "501", "478", expected_revision_no=0)
        assert repo.get(created.batch_no).revision_no == 0
        assert repo.list_reading_revisions(created.batch_no) == ()

    @pytest.mark.parametrize("bad_index", [-1, 3, 99])
    def test_index_out_of_bounds_is_422_domain_error_and_changes_nothing(
        self, repo, created, bad_index
    ):
        before = repo.get(created.batch_no)
        with pytest.raises(ReadingIndexOutOfBoundsError):
            repo.revise_reading(
                created.batch_no, bad_index, "501", "478", expected_revision_no=0
            )
        assert repo.get(created.batch_no) == before
        assert repo.list_reading_revisions(created.batch_no) == ()

    @pytest.mark.parametrize("bad_index", [2**63, -(2**63) - 1, 10**30])
    def test_index_beyond_sqlite_integer_range_is_domain_error_not_overflow(
        self, repo, created, bad_index
    ):
        before = repo.get(created.batch_no)
        with pytest.raises(ReadingIndexOutOfBoundsError):
            repo.revise_reading(
                created.batch_no, bad_index, "501", "478", expected_revision_no=0
            )
        assert repo.get(created.batch_no) == before
        assert repo.list_reading_revisions(created.batch_no) == ()

    def test_missing_batch_revision_raises_404(self, repo):
        with pytest.raises(BatchNotFoundError):
            repo.revise_reading("MC19990101-DEADBEEF", 0, "501", "478", 0)

    def test_audit_insert_failure_rolls_back_reading_and_revision(self, repo, created):
        class _FailingConnection:
            """仅拦截审计表 INSERT 的连接代理，其余方法委托真实 sqlite 连接。"""

            def __init__(self, real):
                self._real = real

            def execute(self, sql, params=()):
                if "INSERT INTO sampling_reading_revisions" in sql:
                    raise RuntimeError("模拟审计写入失败")
                return self._real.execute(sql, params)

            def __getattr__(self, name):
                return getattr(self._real, name)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return self._real.__exit__(*exc)

        class AuditFailRepository(SamplingBatchRepository):
            def _connect(self):
                return _FailingConnection(super()._connect())

        # sqlite3.Connection 是不可变 C 类型，不能 monkeypatch 其 execute；
        # 改为以同一库文件构造子类仓储，在连接层注入审计写入失败
        failing_repo = AuditFailRepository(repo._db_path)
        with pytest.raises(RuntimeError):
            failing_repo.revise_reading(created.batch_no, 0, "501", "478", 0)

        stored = repo.get(created.batch_no)
        assert stored.revision_no == 0
        assert (
            stored.readings[0].wet_sample_mass,
            stored.readings[0].dry_sample_mass,
        ) == READINGS_3[0]
        assert repo.list_reading_revisions(created.batch_no) == ()


class TestLegacyDatabaseMigration:
    def test_legacy_pending_batch_gets_revision_zero_and_can_be_confirmed(self, tmp_path):
        db_path = tmp_path / "legacy.db"
        with sqlite3.connect(db_path) as raw:
            raw.executescript(LEGACY_SCHEMA)
            raw.execute(
                "INSERT INTO sampling_batches"
                " (batch_no, pile_name, status, created_at)"
                " VALUES (?, ?, '待确认', ?)",
                ("MC20260914-11111111", "旧库砂堆", "2026-09-14T00:00:00+00:00"),
            )
            raw.executemany(
                "INSERT INTO sampling_readings"
                " (batch_no, ordinal, wet_sample_mass, dry_sample_mass)"
                " VALUES (?, ?, ?, ?)",
                [
                    ("MC20260914-11111111", 0, "210", "200"),
                    ("MC20260914-11111111", 1, "206", "200"),
                ],
            )
            raw.commit()

        repo = SamplingBatchRepository(db_path)
        record = repo.get("MC20260914-11111111")
        assert record.status == "待确认"
        assert record.revision_no == 0
        original_confirmed = repo.confirm(
            "MC20260914-11111111", ["5", "3"], "4.000"
        )
        assert original_confirmed.status == "已确认"
        assert original_confirmed.revision_no == 0
        assert [r.moisture_pct for r in original_confirmed.readings] == ["5", "3"]

    def test_legacy_pending_batch_can_be_revised_then_confirmed_with_latest_reading(
        self, tmp_path
    ):
        db_path = tmp_path / "legacy_revised.db"
        with sqlite3.connect(db_path) as raw:
            raw.executescript(LEGACY_SCHEMA)
            raw.execute(
                "INSERT INTO sampling_batches"
                " (batch_no, pile_name, status, created_at)"
                " VALUES (?, ?, '待确认', ?)",
                ("MC20260914-22222222", "旧库砂堆", "2026-09-14T00:00:00+00:00"),
            )
            raw.executemany(
                "INSERT INTO sampling_readings"
                " (batch_no, ordinal, wet_sample_mass, dry_sample_mass)"
                " VALUES (?, ?, ?, ?)",
                [
                    ("MC20260914-22222222", 0, "210", "200"),
                    ("MC20260914-22222222", 1, "206", "200"),
                ],
            )
            raw.commit()

        repo = SamplingBatchRepository(db_path)
        record = repo.get("MC20260914-22222222")
        assert record.status == "待确认"
        assert record.revision_no == 0
        revised = repo.revise_reading(
            "MC20260914-22222222", 0, "220", "200", expected_revision_no=0
        )
        assert revised.revision_no == 1
        confirmed = repo.confirm(
            "MC20260914-22222222", ["10", "3"], "6.500"
        )
        assert confirmed.status == "已确认"
        assert confirmed.revision_no == 1
        assert confirmed.readings[0].wet_sample_mass == "220"
        assert [r.moisture_pct for r in confirmed.readings] == ["10", "3"]


class TestPersistence:
    def test_rebuilt_repository_reads_same_batch(self, repo, tmp_path, created):
        repo.confirm(created.batch_no, TestConfirm.PCTS, "4.689")
        rebuilt = SamplingBatchRepository(tmp_path / "moisture.db")
        rec = rebuilt.get(created.batch_no)
        assert isinstance(rec, SamplingBatchRecord)
        assert rec.status == "已确认"
        assert rec.representative_moisture_pct == "4.689"
        assert len(rec.readings) == 3
        assert all(isinstance(r, StoredReading) for r in rec.readings)

    def test_pending_survives_rebuild(self, tmp_path, created):
        rebuilt = SamplingBatchRepository(tmp_path / "moisture.db")
        rec = rebuilt.get(created.batch_no)
        assert rec.status == "待确认"
        assert rec.representative_moisture_pct is None
        assert all(r.moisture_pct is None for r in rec.readings)

    def test_confirm_then_rebuild_is_still_immutable(self, repo, tmp_path, created):
        repo.confirm(created.batch_no, TestConfirm.PCTS, "4.689")
        rebuilt = SamplingBatchRepository(tmp_path / "moisture.db")
        with pytest.raises(BatchAlreadyConfirmedError):
            rebuilt.confirm(created.batch_no, ["1"] * 3, "1.000")

    def test_list_batches_orders_by_created_at(self, tmp_path):
        r = SamplingBatchRepository(
            tmp_path / "o.db",
            batch_no_factory=iter(
                ["MC20260914-00000001", "MC20260914-00000002", "MC20260914-00000003"]
            ).__next__,
        )
        times = iter(
            ["2026-09-14T08:00:00+00:00", "2026-09-14T09:00:00+00:00", "2026-09-14T10:00:00+00:00"]
        )
        r.create("堆一", READINGS_3[:2], now=lambda: next(times))
        r.create("堆二", READINGS_3[:2], now=lambda: next(times))
        r.create("堆三", READINGS_3[:2], now=lambda: next(times))
        assert [b.pile_name for b in r.list_batches()] == ["堆一", "堆二", "堆三"]

    def test_close_is_idempotent(self, repo):
        repo.close()
        repo.close()
