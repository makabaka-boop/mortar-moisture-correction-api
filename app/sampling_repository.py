"""取样批次仓储：标准库 sqlite3 持久化批次、原始读数、状态与确认结果。

无数据库基线（不引入服务端数据库）：SQLite 文件库，建库时落 schema。
写入保证整批：批次主行与 2 ~ 5 行原始读数在同一事务内提交，
中途任何异常都整体回滚，不留半截批次；确认同样是条件更新事务
（仅“待确认”可转“已确认”），已确认数据不可改动。待确认批次的
单组修订也是事务：原子替换读数、递增批次修订号并追加审计记录。

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
    revision_no   INTEGER NOT NULL DEFAULT 0 CHECK (revision_no >= 0),
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
CREATE TABLE IF NOT EXISTS sampling_reading_revisions (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_no                TEXT NOT NULL
        REFERENCES sampling_batches(batch_no) ON DELETE CASCADE,
    ordinal                 INTEGER NOT NULL CHECK (ordinal >= 0),
    old_wet_sample_mass     TEXT NOT NULL,
    old_dry_sample_mass     TEXT NOT NULL,
    new_wet_sample_mass     TEXT NOT NULL,
    new_dry_sample_mass     TEXT NOT NULL,
    previous_revision_no    INTEGER NOT NULL CHECK (previous_revision_no >= 0),
    new_revision_no         INTEGER NOT NULL CHECK (new_revision_no >= 1),
    revised_at              TEXT NOT NULL,
    UNIQUE (batch_no, new_revision_no)
);
"""

LEGACY_SCHEMA = """
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

# SQLite INTEGER 为 64 位有符号整数；超出该范围的下标无法绑定查询参数
_SQLITE_INTEGER_MIN = -(2**63)
_SQLITE_INTEGER_MAX = 2**63 - 1


class BatchNotFoundError(LookupError):
    """批次编号不存在（端点映射为结构化 404）。"""


class BatchAlreadyConfirmedError(RuntimeError):
    """批次已确认，确认或修订被拒绝（端点映射为结构化 409）。"""


class ReadingIndexOutOfBoundsError(LookupError):
    """读数下标越界（端点映射为定位到 index 的结构化 422）。"""


class RevisionConflictError(RuntimeError):
    """客户端修订号过期，乐观并发修订被拒绝（端点映射为结构化 409）。"""


class InvalidReadingIndexError(ValueError):
    """路径中的读数下标不是整数（端点映射为定位到 index 的结构化 422）。"""


@dataclass(frozen=True)
class StoredReading:
    """一行原始称量及（确认后的）组含水率，全部为原样/定点字符串。"""

    index: int
    wet_sample_mass: str
    dry_sample_mass: str
    moisture_pct: str | None


@dataclass(frozen=True)
class ReadingRevisionRecord:
    """一次成功修订的审计记录：修改前后质量、修订号与时间。"""

    id: int
    batch_no: str
    index: int
    old_wet_sample_mass: str
    old_dry_sample_mass: str
    new_wet_sample_mass: str
    new_dry_sample_mass: str
    previous_revision_no: int
    new_revision_no: int
    revised_at: str


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
    revision_no: int = 0


def default_batch_no_factory() -> str:
    """生成批次编号：MC + 当地日期(YYYYMMDD) + 8 位大写十六进制随机段。

    形如 ``MC20260914-7F3A9C21``：日期前缀便于人工追溯排序，
    随机段经 PRIMARY KEY 去重（冲突时 create 内重试）。
    """
    return f"MC{datetime.now().strftime('%Y%m%d')}-{secrets.token_hex(4).upper()}"


def utc_now_iso() -> str:
    """带时区的 UTC ISO 8601 时间戳。"""
    return datetime.now(timezone.utc).isoformat()


def parse_reading_index(value: str) -> int:
    """解析路径读数下标；只接受十进制整数文本（如 ``0``、``-1``）。"""
    try:
        digits = value.lstrip("-")
        if not digits or not digits.isascii() or not digits.isdigit():
            raise ValueError
        parsed = int(value, 10)
    except (TypeError, ValueError, AttributeError):
        raise InvalidReadingIndexError(value) from None
    return parsed


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
            self._initialize_schema(conn)
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
                " (batch_no, pile_name, status, revision_no, created_at)"
                " VALUES (?, ?, ?, 0, ?)",
                (batch_no, pile_name, STATUS_PENDING, created_at),
            )
            self._insert_readings(conn, batch_no, readings)
            conn.commit()
        except BaseException:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()
        return self.get(batch_no)  # 提交后以标准读路径回读，避免两份组装逻辑

    def confirm(
        self,
        batch_no: str,
        reading_moisture_pct: Sequence[str] | None = None,
        representative_moisture_pct: str | None = None,
        *,
        now: Callable[[], str] = utc_now_iso,
        result_provider: (
            Callable[[Sequence[StoredReading]], tuple[Sequence[str], str]] | None
        ) = None,
    ) -> SamplingBatchRecord:
        """确认批次：条件事务把“待确认”置为“已确认”并写入结果。

        - 编号不存在 → BatchNotFoundError；
        - 已确认 → BatchAlreadyConfirmedError，库内数据原样不动；
        - result_provider 在同一写事务、锁定批次后按最新读数生成结果，避免确认
          前一刻的修订写入旧值；直接传入结果的旧调用形式仍兼容；
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

            if result_provider is not None:
                reading_rows = conn.execute(
                    "SELECT ordinal, wet_sample_mass, dry_sample_mass, moisture_pct "
                    "FROM sampling_readings WHERE batch_no=? ORDER BY ordinal",
                    (batch_no,),
                ).fetchall()
                readings_for_confirm = tuple(
                    StoredReading(
                        index=reading_row[0],
                        wet_sample_mass=reading_row[1],
                        dry_sample_mass=reading_row[2],
                        moisture_pct=reading_row[3],
                    )
                    for reading_row in reading_rows
                )
                reading_moisture_pct, representative_moisture_pct = result_provider(
                    readings_for_confirm
                )
            elif reading_moisture_pct is None or representative_moisture_pct is None:
                raise ValueError("确认批次必须提供结果或 result_provider")

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
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()
        return self.get(batch_no)

    def revise_reading(
        self,
        batch_no: str,
        index: int,
        wet_sample_mass: str,
        dry_sample_mass: str,
        expected_revision_no: int,
        *,
        now: Callable[[], str] = utc_now_iso,
    ) -> SamplingBatchRecord:
        """原子修订单组读数：替换读数、修订号加一并保存修改前后审计。

        仅“待确认”批次可修订；已确认返回 BatchAlreadyConfirmedError，
        过期修订号返回 RevisionConflictError，下标越界返回
        ReadingIndexOutOfBoundsError。任一步失败均回滚，读数、修订号和
        审计记录保持修订前状态。
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            batch = conn.execute(
                "SELECT status, revision_no FROM sampling_batches WHERE batch_no=?",
                (batch_no,),
            ).fetchone()
            if batch is None:
                conn.rollback()
                raise BatchNotFoundError(batch_no)
            status, current_revision_no = batch
            if status == STATUS_CONFIRMED:
                conn.rollback()
                raise BatchAlreadyConfirmedError(batch_no)
            if current_revision_no != expected_revision_no:
                conn.rollback()
                raise RevisionConflictError(
                    (batch_no, current_revision_no, expected_revision_no)
                )

            # 超出 SQLite INTEGER 范围的下标无法绑定查询，与越界一样定位
            # index 拒绝，而不是让 OverflowError 冒泡为服务器异常
            if not (_SQLITE_INTEGER_MIN <= index <= _SQLITE_INTEGER_MAX):
                conn.rollback()
                raise ReadingIndexOutOfBoundsError((batch_no, index))

            old_row = conn.execute(
                "SELECT wet_sample_mass, dry_sample_mass FROM sampling_readings "
                "WHERE batch_no=? AND ordinal=?",
                (batch_no, index),
            ).fetchone()
            if old_row is None:
                conn.rollback()
                raise ReadingIndexOutOfBoundsError((batch_no, index))

            new_revision_no = current_revision_no + 1
            revised_at = now()
            conn.execute(
                "UPDATE sampling_readings SET wet_sample_mass=?, dry_sample_mass=?, "
                "moisture_pct=NULL WHERE batch_no=? AND ordinal=?",
                (wet_sample_mass, dry_sample_mass, batch_no, index),
            )
            conn.execute(
                "UPDATE sampling_batches SET revision_no=? WHERE batch_no=?",
                (new_revision_no, batch_no),
            )
            conn.execute(
                "INSERT INTO sampling_reading_revisions"
                " (batch_no, ordinal, old_wet_sample_mass, old_dry_sample_mass,"
                " new_wet_sample_mass, new_dry_sample_mass, previous_revision_no,"
                " new_revision_no, revised_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    batch_no,
                    index,
                    old_row[0],
                    old_row[1],
                    wet_sample_mass,
                    dry_sample_mass,
                    current_revision_no,
                    new_revision_no,
                    revised_at,
                ),
            )
            conn.commit()
        except BaseException:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
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
                "SELECT batch_no, pile_name, status, revision_no, "
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
                "SELECT batch_no, pile_name, status, revision_no, "
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

    def list_reading_revisions(self, batch_no: str) -> tuple[ReadingRevisionRecord, ...]:
        """按发生顺序读取批次的单组称量修订审计；批次不存在则抛 404 异常。"""
        conn = self._connect()
        try:
            exists = conn.execute(
                "SELECT 1 FROM sampling_batches WHERE batch_no=?", (batch_no,)
            ).fetchone()
            if exists is None:
                raise BatchNotFoundError(batch_no)
            rows = conn.execute(
                "SELECT id, batch_no, ordinal, old_wet_sample_mass, old_dry_sample_mass,"
                " new_wet_sample_mass, new_dry_sample_mass, previous_revision_no,"
                " new_revision_no, revised_at FROM sampling_reading_revisions"
                " WHERE batch_no=? ORDER BY id",
                (batch_no,),
            ).fetchall()
            return tuple(
                ReadingRevisionRecord(
                    id=row[0],
                    batch_no=row[1],
                    index=row[2],
                    old_wet_sample_mass=row[3],
                    old_dry_sample_mass=row[4],
                    new_wet_sample_mass=row[5],
                    new_dry_sample_mass=row[6],
                    previous_revision_no=row[7],
                    new_revision_no=row[8],
                    revised_at=row[9],
                )
                for row in rows
            )
        finally:
            conn.close()

    def close(self) -> None:
        """连接按操作短开短关，无长驻资源；保留方法以兼容生命周期管理。"""

    # ---- 内部 -------------------------------------------------------------

    @staticmethod
    def _initialize_schema(conn: sqlite3.Connection) -> None:
        """建表并把旧版取样库迁移到“批次修订号 + 审计表”结构。

        SQLite 旧库没有 revision_no 列；ADD COLUMN 的 NOT NULL DEFAULT 0
        会原子地给所有既有批次补零。审计表以 IF NOT EXISTS 建立，重复初始化安全。
        """
        conn.executescript(SCHEMA)
        SamplingBatchRepository._migrate_batch_revisions(conn)

    @staticmethod
    def _migrate_batch_revisions(conn: sqlite3.Connection) -> None:
        """旧 schema 补 revision_no=0；新 schema 若已有列则保持原值不变。"""
        columns = conn.execute("PRAGMA table_info(sampling_batches)").fetchall()
        if "revision_no" not in {column[1] for column in columns}:
            conn.execute(
                "ALTER TABLE sampling_batches "
                "ADD COLUMN revision_no INTEGER NOT NULL DEFAULT 0"
            )
            conn.execute("UPDATE sampling_batches SET revision_no=0")

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
            revision_no=batch_row[3],
            readings=readings,
            representative_moisture_pct=batch_row[4],
            created_at=batch_row[5],
            confirmed_at=batch_row[6],
        )
