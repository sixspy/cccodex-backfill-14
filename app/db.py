import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import Config


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None: 事务由 BEGIN IMMEDIATE 显式控制，避免隐式事务
    # 让「先核后写」出现 TOCTOU 窗口。
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


@contextmanager
def get_db(db_path: str | None = None):
    conn = connect(db_path)
    try:
        # IMMEDIATE 先拿写锁：同一片键的占片请求在此串行化，
        # 配合 slice_locks.slice_key / backfill_target.slice_key 的唯一索引兜底。
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS backfill_windows (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL,
  start_date TEXT NOT NULL,
  end_date TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('草稿','已计划','执行中','已完成')),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS backfill_slices (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  window_id INTEGER NOT NULL REFERENCES backfill_windows(id),
  slice_date TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('待重放','已落库','失败')),
  row_count INTEGER NOT NULL DEFAULT 0,
  UNIQUE(window_id, slice_date)
);

CREATE TABLE IF NOT EXISTS coverage_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  window_id INTEGER NOT NULL,
  planned INTEGER NOT NULL,
  loaded INTEGER NOT NULL,
  missing INTEGER NOT NULL,
  summary TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 片锁账册：一个片键只允许一把锁，唯一索引即「挡后到」的硬闸。
CREATE TABLE IF NOT EXISTS slice_locks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  lock_no TEXT NOT NULL UNIQUE,
  slice_id INTEGER NOT NULL,
  slice_key TEXT NOT NULL UNIQUE,
  claimer TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('持有')),
  acquired_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 争用台账：每次占片尝试一行，拒重行带拒重号。
CREATE TABLE IF NOT EXISTS claim_ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  attempt_no INTEGER NOT NULL,
  slice_id INTEGER NOT NULL,
  slice_key TEXT NOT NULL,
  claimer TEXT NOT NULL,
  outcome TEXT NOT NULL CHECK (outcome IN ('占片成功','拒重')),
  lock_no TEXT,
  reject_no TEXT,
  round_no TEXT,
  detail TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_claim_ledger_key ON claim_ledger(slice_key);
CREATE INDEX IF NOT EXISTS ix_claim_ledger_round ON claim_ledger(round_no);

-- 争抢记录：一轮同片争抢只落一条，供编排留证。
CREATE TABLE IF NOT EXISTS contention_rounds (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  round_no TEXT NOT NULL UNIQUE,
  slice_key TEXT NOT NULL,
  lock_no TEXT NOT NULL,
  reject_no TEXT NOT NULL,
  attempts INTEGER NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_contention_rounds_key ON contention_rounds(slice_key);

-- 目标表落库证据：同片键只留一行，重放复验只认这里的条数。
CREATE TABLE IF NOT EXISTS backfill_target (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slice_key TEXT NOT NULL UNIQUE,
  slice_id INTEGER NOT NULL,
  window_id INTEGER NOT NULL,
  table_name TEXT NOT NULL,
  slice_date TEXT NOT NULL,
  row_count INTEGER NOT NULL,
  lock_no TEXT,
  written_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_backfill_target_window ON backfill_target(window_id);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


def init_db(db_path: str | None = None) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        if conn.execute("SELECT value FROM meta WHERE key='seeded'").fetchone():
            return
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO backfill_windows(table_name, start_date, end_date, status) "
            "VALUES ('dw_orders_d','2026-11-10','2026-11-12','草稿')"
        )
        conn.execute(
            "INSERT INTO backfill_windows(table_name, start_date, end_date, status) "
            "VALUES ('dw_payments_d','2026-11-11','2026-11-11','草稿')"
        )
        conn.execute("INSERT INTO meta(key, value) VALUES ('seeded','1')")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
