import sqlite3
from contextlib import contextmanager

from flask import current_app, g


def get_db():
    if "db" not in g:
        # isolation_level=None：自己用 BEGIN IMMEDIATE 控制事务，
        # 让「推进一条」与「喊停」在多线程下严格串行。
        g.db = sqlite3.connect(
            current_app.config["DATABASE"],
            detect_types=sqlite3.PARSE_DECLTYPES,
            isolation_level=None,
            timeout=15,
        )
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA busy_timeout = 15000")
    return g.db


def close_db(e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@contextmanager
def transaction(db=None):
    """显式写事务：进入即拿写锁，退出时提交 / 异常回滚。"""
    db = db if db is not None else get_db()
    db.execute("BEGIN IMMEDIATE")
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise


@contextmanager
def db_transaction():
    with transaction(get_db()) as db:
        yield db


def _columns(db, table: str) -> set[str]:
    return {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}


def _migrate(db) -> None:
    """老库补列：resume_sku（下一条待处理编码）、item_code（已落库编码）。"""
    job_cols = _columns(db, "crawl_jobs")
    if "resume_sku" not in job_cols:
        db.execute("ALTER TABLE crawl_jobs ADD COLUMN resume_sku TEXT")

    rec_cols = _columns(db, "crawl_records")
    if "item_code" not in rec_cols:
        db.execute("ALTER TABLE crawl_records ADD COLUMN item_code TEXT")
        try:
            db.execute(
                "UPDATE crawl_records SET item_code = json_extract(payload_json, '$.sku') "
                "WHERE item_code IS NULL"
            )
        except sqlite3.Error:
            pass
    try:
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_records_job_code "
            "ON crawl_records(job_id, item_code) WHERE item_code IS NOT NULL"
        )
    except sqlite3.Error:
        pass


def init_db(app):
    with app.app_context():
        db = get_db()
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS crawl_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                seed_path TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft',
                resume_sku TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                finished_at TEXT
            );
            CREATE TABLE IF NOT EXISTS crawl_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL REFERENCES crawl_jobs(id) ON DELETE CASCADE,
                item_key TEXT NOT NULL,
                item_code TEXT,
                title TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(job_id, item_key)
            );
            """
        )
        _migrate(db)
        db.commit()
