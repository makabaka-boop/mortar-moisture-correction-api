"""校准曲线仓储：标准库 sqlite3 持久化不可变校准曲线及其对照点。

曲线写入既有 SQLite 文件（默认与取样批次同一库）：建库时以
``CREATE TABLE IF NOT EXISTS`` 追加两张表，旧库无需导出迁移，取样批次等
既有表与数据原样不动。

写入保证整批：曲线主行与 3 ~ 8 行对照点在同一事务内提交，中途任何异常
都整体回滚，不留半截曲线；失败时不产生或改写任何记录。

不可变保证：曲线一经提交，仓储不提供任何更新/删除入口；并在库内以触发器
拒绝 UPDATE/DELETE，即使绕过仓储直接打开同一文件也无法改写或删除曲线。

仓储只负责存取：原始信号与参考含水率按原始读数字符串原样落库，插值由
calibration_calculator 完成。
"""
from __future__ import annotations

import os
import secrets
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

# 对照点与曲线主行同表追加，不依赖取样模块的任何常量，保持模块独立

SCHEMA = """
CREATE TABLE IF NOT EXISTS calibration_curves (
    curve_no    TEXT PRIMARY KEY,
    sensor_id   TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS calibration_points (
    curve_no                    TEXT NOT NULL
        REFERENCES calibration_curves(curve_no) ON DELETE CASCADE,
    ordinal                     INTEGER NOT NULL CHECK (ordinal >= 0),
    raw_signal                  TEXT NOT NULL,
    reference_moisture_pct      TEXT NOT NULL,
    PRIMARY KEY (curve_no, ordinal)
);
"""

# 不可变触发器：拒绝绕过仓储直接改写/删除曲线或对照点（IF NOT EXISTS 重复初始化安全）
IMMUTABILITY_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS trg_calibration_curves_no_update
BEFORE UPDATE ON calibration_curves
BEGIN
    SELECT RAISE(ABORT, '校准曲线为不可变记录，禁止更新');
END;
CREATE TRIGGER IF NOT EXISTS trg_calibration_curves_no_delete
BEFORE DELETE ON calibration_curves
BEGIN
    SELECT RAISE(ABORT, '校准曲线为不可变记录，禁止删除');
END;
CREATE TRIGGER IF NOT EXISTS trg_calibration_points_no_update
BEFORE UPDATE ON calibration_points
BEGIN
    SELECT RAISE(ABORT, '校准曲线为不可变记录，对照点禁止更新');
END;
CREATE TRIGGER IF NOT EXISTS trg_calibration_points_no_delete
BEFORE DELETE ON calibration_points
BEGIN
    SELECT RAISE(ABORT, '校准曲线为不可变记录，对照点禁止删除');
END;
"""

_CURVE_ID_RETRIES = 5


class CurveNotFoundError(LookupError):
    """曲线编号不存在（端点映射为结构化 404）。"""


@dataclass(frozen=True)
class StoredCalibrationPoint:
    """一行对照点，信号与参考含水率均为原样字符串。"""

    index: int
    raw_signal: str
    reference_moisture_pct: str


@dataclass(frozen=True)
class CalibrationCurveRecord:
    """一条校准曲线的完整持久化状态（不可变）。"""

    curve_no: str
    sensor_id: str
    points: tuple[StoredCalibrationPoint, ...]
    created_at: str


def default_curve_no_factory() -> str:
    """生成曲线编号：CC + 当地日期(YYYYMMDD) + 8 位大写十六进制随机段。

    形如 ``CC20260914-7F3A9C21``：日期前缀便于人工追溯排序，随机段经
    PRIMARY KEY 去重（冲突时 create 内重试）。
    """
    return f"CC{datetime.now().strftime('%Y%m%d')}-{secrets.token_hex(4).upper()}"


def utc_now_iso() -> str:
    """带时区的 UTC ISO 8601 时间戳（与取样仓储同一格式）。"""
    return datetime.now(timezone.utc).isoformat()


class CalibrationCurveRepository:
    """SQLite 校准曲线仓储；连接按操作短开短关，重建实例即可重读同一库文件。"""

    def __init__(
        self,
        db_path: str | os.PathLike[str],
        *,
        curve_no_factory: Callable[[], str] = default_curve_no_factory,
    ) -> None:
        self._db_path = os.fspath(db_path)
        self._curve_no_factory = curve_no_factory
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

    # ---- 写入（仅创建，无更新/删除） --------------------------------------

    def create(
        self,
        sensor_id: str,
        points: Sequence[tuple[str, str]],
        *,
        now: Callable[[], str] = utc_now_iso,
    ) -> CalibrationCurveRecord:
        """整批写入不可变曲线：主行与全部对照点同一事务提交或一起回滚。

        points 为 (原始信号原始串, 参考含水率原始串)，顺序即下标。
        """
        if not (3 <= len(points) <= 8):  # 契约层已拦截，仓储再守一道
            raise ValueError("校准曲线必须包含 3 ~ 8 个对照点")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            curve_no = self._unique_curve_no(conn)
            created_at = now()
            conn.execute(
                "INSERT INTO calibration_curves (curve_no, sensor_id, created_at)"
                " VALUES (?, ?, ?)",
                (curve_no, sensor_id, created_at),
            )
            self._insert_points(conn, curve_no, points)
            conn.commit()
        except BaseException:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()
        return self.get(curve_no)  # 提交后以标准读路径回读，避免两份组装逻辑

    # ---- 读取 -------------------------------------------------------------

    def get(self, curve_no: str) -> CalibrationCurveRecord:
        """按编号读取完整曲线（含按 ordinal 排序的对照点）；不存在则抛 404 异常。"""
        conn = self._connect()
        try:
            curve_row = conn.execute(
                "SELECT curve_no, sensor_id, created_at"
                " FROM calibration_curves WHERE curve_no=?",
                (curve_no,),
            ).fetchone()
            if curve_row is None:
                raise CurveNotFoundError(curve_no)
            point_rows = conn.execute(
                "SELECT ordinal, raw_signal, reference_moisture_pct"
                " FROM calibration_points WHERE curve_no=? ORDER BY ordinal",
                (curve_no,),
            ).fetchall()
            return self._hydrate(curve_row, point_rows)
        finally:
            conn.close()

    def count_curves(self) -> int:
        """曲线数（测试与验收核对“非法点集不落库”用）。"""
        conn = self._connect()
        try:
            return conn.execute("SELECT COUNT(*) FROM calibration_curves").fetchone()[0]
        finally:
            conn.close()

    def count_points(self, curve_no: str) -> int:
        """某曲线的对照点数（不存在则抛 404）。"""
        conn = self._connect()
        try:
            exists = conn.execute(
                "SELECT 1 FROM calibration_curves WHERE curve_no=?", (curve_no,)
            ).fetchone()
            if exists is None:
                raise CurveNotFoundError(curve_no)
            return conn.execute(
                "SELECT COUNT(*) FROM calibration_points WHERE curve_no=?", (curve_no,)
            ).fetchone()[0]
        finally:
            conn.close()

    def close(self) -> None:
        """连接按操作短开短关，无长驻资源；保留方法以兼容生命周期管理。"""

    # ---- 内部 -------------------------------------------------------------

    @staticmethod
    def _initialize_schema(conn: sqlite3.Connection) -> None:
        """在既有库文件上追加校准曲线两表与不可变触发器。

        取样批次等既有表不受影响；IF NOT EXISTS 保证对同一文件重复初始化安全。
        """
        conn.executescript(SCHEMA)
        conn.executescript(IMMUTABILITY_TRIGGERS)

    def _unique_curve_no(self, conn: sqlite3.Connection) -> str:
        """在事务内取未占用的曲线编号，极小概率冲突时换号重试。"""
        for _ in range(_CURVE_ID_RETRIES):
            candidate = self._curve_no_factory()
            exists = conn.execute(
                "SELECT 1 FROM calibration_curves WHERE curve_no=?", (candidate,)
            ).fetchone()
            if exists is None:
                return candidate
        raise RuntimeError("曲线编号连续冲突，无法生成唯一编号")  # pragma: no cover

    @staticmethod
    def _insert_points(
        conn: sqlite3.Connection,
        curve_no: str,
        points: Sequence[tuple[str, str]],
    ) -> None:
        """逐行插入对照点；逐行（而非单条 executemany）便于测试注入中途失败。"""
        for ordinal, (raw_signal, reference_moisture_pct) in enumerate(points):
            conn.execute(
                "INSERT INTO calibration_points"
                " (curve_no, ordinal, raw_signal, reference_moisture_pct)"
                " VALUES (?, ?, ?, ?)",
                (curve_no, ordinal, raw_signal, reference_moisture_pct),
            )

    @staticmethod
    def _hydrate(
        curve_row: tuple, point_rows: Sequence[tuple]
    ) -> CalibrationCurveRecord:
        points = tuple(
            StoredCalibrationPoint(
                index=row[0],
                raw_signal=row[1],
                reference_moisture_pct=row[2],
            )
            for row in point_rows
        )
        return CalibrationCurveRecord(
            curve_no=curve_row[0],
            sensor_id=curve_row[1],
            points=points,
            created_at=curve_row[2],
        )
