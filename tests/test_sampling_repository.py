"""取样批次仓储测试：SQLite 持久化、整批事务、条件确认、重建读取。"""
import sqlite3

import pytest

from app.sampling_repository import (
    BatchAlreadyConfirmedError,
    BatchNotFoundError,
    SamplingBatchRepository,
    SamplingBatchRecord,
    StoredReading,
    default_batch_no_factory,
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

    def test_confirm_missing_batch_raises_404(self, repo):
        with pytest.raises(BatchNotFoundError):
            repo.confirm("MC19990101-DEADBEEF", ["1", "2"], "1.500")
        assert repo.count_batches() == 0

    def test_get_missing_raises_404(self, repo):
        with pytest.raises(BatchNotFoundError):
            repo.get("不存在")


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
