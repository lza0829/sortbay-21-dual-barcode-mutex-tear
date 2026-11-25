from __future__ import annotations
import sqlite3
from contextlib import contextmanager
from app.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS lines (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS shifts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  business_day TEXT NOT NULL,
  name TEXT NOT NULL,
  sealed INTEGER NOT NULL DEFAULT 0,
  sealed_at TEXT,
  UNIQUE(business_day, name)
);
CREATE TABLE IF NOT EXISTS chutes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  line_id INTEGER NOT NULL,
  code TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT '口门开',
  soft_cap INTEGER NOT NULL DEFAULT 200,
  UNIQUE(line_id, code),
  FOREIGN KEY(line_id) REFERENCES lines(id)
);
CREATE TABLE IF NOT EXISTS route_rules (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  dest_code TEXT NOT NULL UNIQUE,
  chute_id INTEGER NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(chute_id) REFERENCES chutes(id)
);
CREATE TABLE IF NOT EXISTS scans (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  barcode TEXT NOT NULL,
  dest_code TEXT NOT NULL DEFAULT '',
  shift_id INTEGER NOT NULL,
  line_id INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT '待路由',
  chute_id INTEGER,
  bag_id INTEGER,
  divert_batch_id INTEGER,
  scanned_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(shift_id) REFERENCES shifts(id),
  FOREIGN KEY(line_id) REFERENCES lines(id),
  FOREIGN KEY(chute_id) REFERENCES chutes(id)
);
-- ADR-0001 互斥单行臂：同班次同条码至多一行。
-- 唯一索引在 init_db() 内、旧库脏数据归并之后创建，避免历史双行导致建索引失败。
CREATE TABLE IF NOT EXISTS route_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scan_id INTEGER NOT NULL,
  chute_id INTEGER NOT NULL,
  action TEXT NOT NULL,
  created_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(scan_id) REFERENCES scans(id),
  FOREIGN KEY(chute_id) REFERENCES chutes(id)
);
CREATE TABLE IF NOT EXISTS divert_batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  shift_id INTEGER NOT NULL,
  code TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT '回流中',
  created_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(shift_id) REFERENCES shifts(id)
);
CREATE TABLE IF NOT EXISTS bags (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  shift_id INTEGER NOT NULL,
  chute_id INTEGER NOT NULL,
  code TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT '集包中',
  sealed_at TEXT,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(shift_id) REFERENCES shifts(id),
  FOREIGN KEY(chute_id) REFERENCES chutes(id)
);
CREATE TABLE IF NOT EXISTS audit_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  ref_id INTEGER,
  action TEXT NOT NULL,
  created_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # autocommit 模式：事务边界完全由 BEGIN/COMMIT 显式控制，
    # 避免驱动隐式事务与 SAVEPOINT/BEGIN IMMEDIATE 相互干扰。
    conn = sqlite3.connect(
        DB_PATH, check_same_thread=False, isolation_level=None, timeout=10
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    # 并发双扫：WAL 允许读写并发；busy_timeout 让后来的写者等待而非立即 locked。
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def db():
    """通用连接上下文：读多写少场景。异常回滚，正常退出提交。"""
    conn = connect()
    try:
        conn.execute("BEGIN")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


@contextmanager
def write_db():
    """
    装载写口等关键写路径：BEGIN IMMEDIATE 立刻拿写锁，
    把并发双扫在 SQLite 层排成串行队列，最终由 ux_scans_shift_barcode 仲裁唯一赢家。
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def init_db() -> None:
    # executescript 会隐式提交，故 DDL 单独执行（表均为 IF NOT EXISTS）。
    conn = connect()
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()
    # 旧库脏数据归并与唯一索引建立放在显式写事务内，失败可整体回滚。
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _migrate_scan_uniqueness(conn)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def _migrate_scan_uniqueness(conn: sqlite3.Connection) -> None:
    """
    旧库可能已被并发双扫写出同班次同码多行（ADR-0001 要修复的脏数据）。
    建唯一索引前先按 (shift_id, barcode) 归并：保留最早一行（最小 id，即首次过机），
    其下游引用（route_events）改挂到保留行，重复行删除，并留审计痕迹。
    新库无重复行时此函数为空操作。
    """
    dupes = conn.execute(
        """
        SELECT shift_id, barcode, GROUP_CONCAT(id) AS ids, COUNT(*) AS n
        FROM scans
        GROUP BY shift_id, barcode
        HAVING n > 1
        """
    ).fetchall()
    for d in dupes:
        ids = [int(x) for x in d["ids"].split(",")]
        keep_id = min(ids)
        drop_ids = [i for i in ids if i != keep_id]
        conn.execute(
            "UPDATE route_events SET scan_id=? WHERE scan_id IN (%s)"
            % ",".join("?" * len(drop_ids)),
            [keep_id, *drop_ids],
        )
        conn.execute(
            "DELETE FROM scans WHERE id IN (%s)" % ",".join("?" * len(drop_ids)),
            drop_ids,
        )
        conn.execute(
            """
            INSERT INTO audit_events(kind, ref_id, action, created_at, note)
            VALUES ('扫描', ?, '历史同码多行归并',
                    (SELECT scanned_at FROM scans WHERE id=?), ?)
            """,
            (keep_id, keep_id,
             f"shift={d['shift_id']} barcode={d['barcode']} 删除重复行={drop_ids}"),
        )
    # 索引在 SCHEMA 内已 IF NOT EXISTS 创建；此处显式确保迁移路径也成立。
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_scans_shift_barcode ON scans(shift_id, barcode)"
    )


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def rows_to_list(rows) -> list[dict]:
    return [dict(r) for r in rows]
