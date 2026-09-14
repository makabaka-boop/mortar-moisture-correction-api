"""取样批次仓储：标准库 sqlite3 持久化批次、原始读数、状态与确认结果。

无数据库基线（不引入服务端数据库）：SQLite 文件库，建库时落 schema。
写入保证整批：批次主行与 2 ~ 5 行原始读数在同一事务内提交，
中途任何异常都整体回滚，不留半截批次；确认同样是条件更新事务
（仅“待确认”可转“已确认”），已确认数据不可改动。

仓储只负责存取：质量按原始读数字符串原样落库，含水率结果由
sampling_calculator 计算后以定点字符串传入。
"""
from __future__ import annotations

import os
import secrets
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from app.sampling_schemas import STATUS_CONFIRMED, STATUS_PENDING

SCHEMA = """
CREATE TABLE IF NOT EXISTS sampling_batches (
    batch_no      TEXT PRIMARY KEY,
    pile_name     TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('待确认', '已确认')),
    representative_moisture_pct TEXT,
    created_at    TEXT NOT NULL,
    confirmed_at  TEXT
);
CREATE TABLE IF NOT EXISTS sampling_readings (
    batch_no          TEXT NOT NULL
        REFERENCES sampling_batches(batch_no) ON DELETE CASCADE,
    ordinal           INTEGER NOT NULL,
    wet_sample_mass   TEXT NOT NULL,
    dry_sample_mass   TEXT NOT NULL,
    moisture_pct      TEXT,
    PRIMARY KEY (batch_no, ordinal)
);
"""

_BATCH_ID_RETRIES = 5


class BatchNotFoundError(LookupError):
    """批次编号不存在（端点映射为结构化 404）。"""


class BatchAlreadyConfirmedError(RuntimeError):
    """批次已确认，重复确认被拒绝（端点映射为结构化 409）。"""


@dataclass(frozen=True)
class StoredReading:
    """一行原始称量及（确认后的）组含水率，全部为原样/定点字符串。"""

    index: int
    wet_sample_mass: str
    dry_sample_mass: str
    moisture_pct: str | None


@dataclass(frozen=True)
class SamplingBatchRecord:
    """一个取样批次的完整持久化状态。"""

    batch_no: str
    pile_name: str
    status: str
    readings: tuple[StoredReading, ...]
    representative_moisture_pct: str | None
    created_at: str
    confirmed_at: str | None


def default_batch_no_factory() -> str:
    """生成批次编号：MC + 当地日期(YYYYMMDD) + 8 位大写十六进制随机段。

    形如 ``MC20260914-7F3A9C21``：日期前缀便于人工追溯排序，
    随机段经 PRIMARY KEY 去重（冲突时 create 内重试）。
    """
    return f"MC{datetime.now().strftime('%Y%m%d')}-{secrets.token_hex(4).upper()}"


def utc_now_iso() -> str:
    """带时区的 UTC ISO 8601 时间戳。"""
    return datetime.now(timezone.utc).isoformat()


class SamplingBatchRepository:
    """SQLite 取样批次仓储；连接按操作短开短关，重建实例即可重读同一库文件。"""

    def __init__(
        self,
        db_path: str | os.PathLike[str],
        *,
        batch_no_factory: Callable[[], str] = default_batch_no_factory,
    ) -> None:
        self._db_path = os.fspath(db_path)
        self._batch_no_factory = batch_no_factory
        parent = os.path.dirname(self._db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=30.0)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    # ---- 写入 -------------------------------------------------------------

    def create(
        self,
        pile_name: str,
        readings: Sequence[tuple[str, str]],
        *,
        now: Callable[[], str] = utc_now_iso,
    ) -> SamplingBatchRecord:
        """整批写入“待确认”批次：主行与全部读数同一事务提交或一起回滚。

        readings 为 (湿样质量原始串, 干样质量原始串)，顺序即下标。
        """
        if not (2 <= len(readings) <= 5):  # 契约层已拦截，仓储再守一道
            raise ValueError("取样批次必须包含 2 ~ 5 组称量")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            batch_no = self._unique_batch_no(conn)
            created_at = now()
            conn.execute(
                "INSERT INTO sampling_batches"
                " (batch_no, pile_name, status, created_at)"
                " VALUES (?, ?, ?, ?)",
                (batch_no, pile_name, STATUS_PENDING, created_at),
            )
            self._insert_readings(conn, batch_no, readings)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
        return self.get(batch_no)  # 提交后以标准读路径回读，避免两份组装逻辑

    def confirm(
        self,
        batch_no: str,
        reading_moisture_pct: Sequence[str],
        representative_moisture_pct: str,
        *,
        now: Callable[[], str] = utc_now_iso,
    ) -> SamplingBatchRecord:
        """确认批次：条件事务把“待确认”置为“已确认”并写入结果。

        - 编号不存在 → BatchNotFoundError；
        - 已确认 → BatchAlreadyConfirmedError，库内数据原样不动；
        - 状态、各组结果、代表值、确认时间在同一事务内提交。
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status FROM sampling_batches WHERE batch_no=?",
                (batch_no,),
            ).fetchone()
            if row is None:
                conn.rollback()
                raise BatchNotFoundError(batch_no)
            if row[0] == STATUS_CONFIRMED:
                conn.rollback()
                raise BatchAlreadyConfirmedError(batch_no)
            confirmed_at = now()
            conn.execute(
                "UPDATE sampling_batches SET status=?, "
                "representative_moisture_pct=?, confirmed_at=? WHERE batch_no=?",
                (
                    STATUS_CONFIRMED,
                    representative_moisture_pct,
                    confirmed_at,
                    batch_no,
                ),
            )
            conn.executemany(
                "UPDATE sampling_readings SET moisture_pct=? "
                "WHERE batch_no=? AND ordinal=?",
                [
                    (pct, batch_no, i)
                    for i, pct in enumerate(reading_moisture_pct)
                ],
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
        return self.get(batch_no)

    # ---- 读取 -------------------------------------------------------------

    def get(self, batch_no: str) -> SamplingBatchRecord:
        """按编号读取完整批次（含按 ordinal 排序的原始读数）；不存在则 404 异常。"""
        conn = self._connect()
        try:
            batch_row = conn.execute(
                "SELECT batch_no, pile_name, status, "
                "representative_moisture_pct, created_at, confirmed_at "
                "FROM sampling_batches WHERE batch_no=?",
                (batch_no,),
            ).fetchone()
            if batch_row is None:
                raise BatchNotFoundError(batch_no)
            reading_rows = conn.execute(
                "SELECT ordinal, wet_sample_mass, dry_sample_mass, moisture_pct "
                "FROM sampling_readings WHERE batch_no=? ORDER BY ordinal",
                (batch_no,),
            ).fetchall()
            return self._hydrate(batch_row, reading_rows)
        finally:
            conn.close()

    def list_batches(self) -> list[SamplingBatchRecord]:
        """列出全部批次，按创建时间、批次编号排序。"""
        conn = self._connect()
        try:
            batch_rows = conn.execute(
                "SELECT batch_no, pile_name, status, "
                "representative_moisture_pct, created_at, confirmed_at "
                "FROM sampling_batches ORDER BY created_at, batch_no"
            ).fetchall()
            return [
                self._hydrate(
                    row,
                    conn.execute(
                        "SELECT ordinal, wet_sample_mass, dry_sample_mass, moisture_pct "
                        "FROM sampling_readings WHERE batch_no=? ORDER BY ordinal",
                        (row[0],),
                    ).fetchall(),
                )
                for row in batch_rows
            ]
        finally:
            conn.close()

    def count_batches(self) -> int:
        """批次数（测试与验收核对“非法称量不落库”用）。"""
        conn = self._connect()
        try:
            return conn.execute("SELECT COUNT(*) FROM sampling_batches").fetchone()[0]
        finally:
            conn.close()

    def close(self) -> None:
        """连接按操作短开短关，无长驻资源；保留方法以兼容生命周期管理。"""

    # ---- 内部 -------------------------------------------------------------

    def _unique_batch_no(self, conn: sqlite3.Connection) -> str:
        """在事务内取未占用的批次编号，极小概率冲突时换号重试。"""
        for _ in range(_BATCH_ID_RETRIES):
            candidate = self._batch_no_factory()
            exists = conn.execute(
                "SELECT 1 FROM sampling_batches WHERE batch_no=?", (candidate,)
            ).fetchone()
            if exists is None:
                return candidate
        raise RuntimeError("批次编号连续冲突，无法生成唯一编号")  # pragma: no cover

    @staticmethod
    def _insert_readings(
        conn: sqlite3.Connection,
        batch_no: str,
        readings: Sequence[tuple[str, str]],
    ) -> None:
        """逐行插入原始读数；逐行（而非单条 executemany）便于测试注入中途失败。"""
        for ordinal, (wet, dry) in enumerate(readings):
            conn.execute(
                "INSERT INTO sampling_readings"
                " (batch_no, ordinal, wet_sample_mass, dry_sample_mass)"
                " VALUES (?, ?, ?, ?)",
                (batch_no, ordinal, wet, dry),
            )

    @staticmethod
    def _hydrate(
        batch_row: sqlite3.Row | tuple, reading_rows: Sequence[tuple]
    ) -> SamplingBatchRecord:
        readings = tuple(
            StoredReading(
                index=row[0],
                wet_sample_mass=row[1],
                dry_sample_mass=row[2],
                moisture_pct=row[3],
            )
            for row in reading_rows
        )
        return SamplingBatchRecord(
            batch_no=batch_row[0],
            pile_name=batch_row[1],
            status=batch_row[2],
            readings=readings,
            representative_moisture_pct=batch_row[3],
            created_at=batch_row[4],
            confirmed_at=batch_row[5],
        )
