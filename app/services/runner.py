"""采集执行器：逐条推进，支持「喊停 / 续上」。

状态机：
    draft --start--> running --自然收完--> done
                        |  pause
                        v
                     paused --resume--> running

`crawl_jobs.resume_sku` 存「下一条待处理编码」（sku），采集与暂停都以它为游标；
续上时只按这条编码定位，绝不从头重收整本目录。
"""

import json
import sqlite3

from flask import current_app

from app.db import db_transaction, get_db
from app.services.dedup import normalize_item_key
from app.services.mock_catalog import CATALOGS, parse_catalog_html, render_catalog_page

PAUSED_RUN_ERROR = "任务已停下，不能另起一趟；请点「续上」"
NOT_PAUSED_RESUME_ERROR = "只有已停下的任务才能续上"
NOT_RUNNING_PAUSE_ERROR = "只有采集中的任务可以喊停"


def _get_job(db, job_id: int):
    job = db.execute("SELECT * FROM crawl_jobs WHERE id = ?", (job_id,)).fetchone()
    if job is None:
        raise ValueError("job not found")
    return job


def _source_items(seed: str) -> list[dict] | None:
    if seed not in CATALOGS:
        return None
    html = render_catalog_page(seed)
    if isinstance(html, tuple):
        html = html[0]
    return parse_catalog_html(html, seed)


def start_job(job_id: int) -> dict:
    """draft -> running，游标指向目录第一条编码。已停下的任务不许另起一趟。"""
    db = get_db()
    _get_job(db, job_id)

    # 状态校验与置位放在同一个写事务里，避免「刚喊停就另起一趟」的竞态。
    with db_transaction() as tx:
        row = tx.execute(
            "SELECT status, seed_path FROM crawl_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise ValueError("job not found")
        status = row["status"]
        if status == "paused":
            raise ValueError(PAUSED_RUN_ERROR)
        if status == "running":
            raise ValueError("job already running")
        if status == "done":
            raise ValueError("job already done")
        if status != "draft":
            raise ValueError(f"任务状态 {status} 不能开始采集")

        items = _source_items(row["seed_path"])
        if items is None:
            tx.execute(
                "UPDATE crawl_jobs SET status = 'failed', resume_sku = NULL, "
                "finished_at = datetime('now') WHERE id = ?",
                (job_id,),
            )
            return {"status": "failed", "reason": "unknown seed",
                    "inserted": 0, "skipped_dup": 0}

        if items:
            tx.execute(
                "UPDATE crawl_jobs SET status = 'running', resume_sku = ?, "
                "finished_at = NULL WHERE id = ?",
                (items[0]["sku"], job_id),
            )
        else:
            tx.execute(
                "UPDATE crawl_jobs SET status = 'done', resume_sku = NULL, "
                "finished_at = datetime('now') WHERE id = ?",
                (job_id,),
            )

    return {
        "status": "running" if items else "done",
        "inserted": 0,
        "skipped_dup": 0,
        "total_source": len(items),
        "resume_sku": items[0]["sku"] if items else None,
    }


def collect_next(job_id: int) -> dict:
    """推进一条：从 resume_sku 指向的那条编码开始，收完把游标挪到下一条。"""
    db = get_db()
    job = _get_job(db, job_id)
    if job["status"] != "running":
        raise ValueError("任务不在采集中，不能再推进")

    # 先拿写锁再读游标：喊停与推进在多线程下严格串行，不会越过喊停多收一条。
    with db_transaction() as tx:
        row = tx.execute(
            "SELECT status, resume_sku, seed_path FROM crawl_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if row is None or row["status"] != "running":
            raise ValueError("任务不在采集中，不能再推进")
        job = row
        seed = job["seed_path"]

        items = _source_items(seed)
        if items is None:
            tx.execute(
                "UPDATE crawl_jobs SET status = 'failed', resume_sku = NULL, "
                "finished_at = datetime('now') WHERE id = ?",
                (job_id,),
            )
            return {"status": "failed", "reason": "unknown seed",
                    "inserted": 0, "skipped_dup": 0}

        cursor = job["resume_sku"]
        idx = 0 if cursor is None else next(
            (i for i, it in enumerate(items) if it["sku"] == cursor), None
        )
        if idx is None or idx >= len(items):
            # 停下时记下的编码已不在目录中：不回头重收整本，就地收尾。
            tx.execute(
                "UPDATE crawl_jobs SET status = 'done', resume_sku = NULL, "
                "finished_at = datetime('now') WHERE id = ? AND status = 'running'",
                (job_id,),
            )
            return {"status": "done", "inserted": 0, "skipped_dup": 0,
                    "reason": "resume_sku 已不在目录中", "resume_sku": None,
                    "total_source": len(items)}

        item = items[idx]
        next_sku = items[idx + 1]["sku"] if idx + 1 < len(items) else None
        key = normalize_item_key(seed, item["title"])
        inserted = 0
        skipped_dup = 0

        try:
            tx.execute(
                """
                INSERT INTO crawl_records (job_id, item_key, item_code, title, payload_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    key,
                    item["sku"],
                    item["title"],
                    json.dumps(item, ensure_ascii=False),
                ),
            )
            inserted = 1
        except sqlite3.IntegrityError:
            # 同一任务内编码或 item_key 已在账本里（含重复标题），不写第二条。
            skipped_dup = 1

        if next_sku is None:
            tx.execute(
                "UPDATE crawl_jobs SET status = 'done', resume_sku = NULL, "
                "finished_at = datetime('now') WHERE id = ?",
                (job_id,),
            )
        else:
            tx.execute(
                "UPDATE crawl_jobs SET resume_sku = ? WHERE id = ?",
                (next_sku, job_id),
            )

    status = "done" if next_sku is None else "running"
    current_app.logger.info(
        "job %s step sku=%s inserted=%s skipped_dup=%s next=%s",
        job_id, item["sku"], inserted, skipped_dup, next_sku,
    )
    return {
        "status": status,
        "inserted": inserted,
        "skipped_dup": skipped_dup,
        "processed_sku": item["sku"],
        "resume_sku": next_sku,
        "total_source": len(items),
    }


def pause_job(job_id: int) -> dict:
    """喊停：冻结游标，账本不再增加。"""
    db = get_db()
    _get_job(db, job_id)
    with db_transaction() as tx:
        guard = tx.execute(
            "UPDATE crawl_jobs SET status = 'paused' WHERE id = ? AND status = 'running'",
            (job_id,),
        )
        if guard.rowcount != 1:
            raise ValueError(NOT_RUNNING_PAUSE_ERROR)

    # 拿到写锁之后再读，确保报出的游标就是冻结下来的那一条。
    frozen = db.execute(
        "SELECT resume_sku FROM crawl_jobs WHERE id = ?", (job_id,)
    ).fetchone()["resume_sku"]
    count = db.execute(
        "SELECT COUNT(*) AS c FROM crawl_records WHERE job_id = ?", (job_id,)
    ).fetchone()["c"]
    current_app.logger.info("job %s paused at sku=%s", job_id, frozen)
    return {"status": "paused", "resume_sku": frozen, "recorded": count}


def resume_job(job_id: int) -> dict:
    """续上：仅从停下时记下的编码往后收。"""
    db = get_db()
    _get_job(db, job_id)
    with db_transaction() as tx:
        guard = tx.execute(
            "UPDATE crawl_jobs SET status = 'running', finished_at = NULL "
            "WHERE id = ? AND status = 'paused'",
            (job_id,),
        )
        if guard.rowcount != 1:
            raise ValueError(NOT_PAUSED_RESUME_ERROR)

    cursor = db.execute(
        "SELECT resume_sku FROM crawl_jobs WHERE id = ?", (job_id,)
    ).fetchone()["resume_sku"]
    current_app.logger.info("job %s resumed at sku=%s", job_id, cursor)
    return {"status": "running", "resume_sku": cursor}


def run_job(job_id: int) -> dict:
    """一次跑完（供脚本/测试使用的同步入口）；已停下的任务会被拒绝。"""
    db = get_db()
    job = _get_job(db, job_id)
    if job["status"] == "paused":
        raise ValueError(PAUSED_RUN_ERROR)

    summary = start_job(job_id)
    if summary["status"] == "failed":
        return summary

    inserted = 0
    skipped_dup = 0
    total_source = summary.get("total_source", 0)
    guard_steps = 0
    while True:
        guard_steps += 1
        if guard_steps > 100000:
            raise RuntimeError("runner step guard exceeded")
        if db.execute(
            "SELECT status FROM crawl_jobs WHERE id = ?", (job_id,)
        ).fetchone()["status"] != "running":
            break
        step = collect_next(job_id)
        if step["status"] == "failed":
            return step
        inserted += step["inserted"]
        skipped_dup += step["skipped_dup"]
        total_source = step.get("total_source", total_source)

    status = db.execute(
        "SELECT status FROM crawl_jobs WHERE id = ?", (job_id,)
    ).fetchone()["status"]
    current_app.logger.info(
        "job %s finished inserted=%s skipped_dup=%s", job_id, inserted, skipped_dup
    )
    return {
        "status": status,
        "inserted": inserted,
        "skipped_dup": skipped_dup,
        "total_source": total_source,
    }
