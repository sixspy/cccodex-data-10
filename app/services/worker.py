"""后台采集驱动：任务处于 running 时逐条推进。

每次「开始 / 续上」都会签发新令牌并启动一个驱动线程；旧线程看到令牌被换掉即退出，
不会再落库。每一步都由 `collect_next` 在事务内确认状态，所以喊停与推进竞争时，
喊停返回之后账本不会再增加。
"""

import threading
import time

from flask import Flask

from app.db import get_db
from app.services.runner import collect_next

_tokens: dict[int, object] = {}
_lock = threading.Lock()


def ensure_worker(app: Flask, job_id: int) -> None:
    if not app.config.get("AUTO_COLLECT", True):
        return
    token = object()
    with _lock:
        _tokens[job_id] = token
        thread = threading.Thread(
            target=_loop,
            args=(app, job_id, token),
            name=f"crawl-job-{job_id}",
            daemon=True,
        )
        thread.start()


def recover_running_jobs(app: Flask) -> None:
    """进程重启后，把库里仍为 running 的任务重新接上驱动线程。"""
    if not app.config.get("AUTO_COLLECT", True):
        return
    with app.app_context():
        rows = get_db().execute(
            "SELECT id FROM crawl_jobs WHERE status = 'running'"
        ).fetchall()
    for row in rows:
        ensure_worker(app, row["id"])


def stop_worker(job_id: int) -> None:
    """撤销当前令牌；正在跑的那条会跑完，随后线程自行退出。"""
    with _lock:
        _tokens.pop(job_id, None)


def _loop(app: Flask, job_id: int, token: object) -> None:
    delay = float(app.config.get("COLLECT_STEP_DELAY", 2.0))
    while _is_current(job_id, token):
        if delay > 0:
            time.sleep(delay)
        if not _is_current(job_id, token):
            return
        with app.app_context():
            try:
                row = get_db().execute(
                    "SELECT status FROM crawl_jobs WHERE id = ?", (job_id,)
                ).fetchone()
                if row is None or row["status"] != "running":
                    return
                step = collect_next(job_id)
            except ValueError:
                # 事务校验发现状态已变（喊停/完成），正常退出。
                return
            except Exception:
                app.logger.exception("job %s step failed", job_id)
                try:
                    db = get_db()
                    db.execute(
                        "UPDATE crawl_jobs SET status = 'failed', resume_sku = NULL, "
                        "finished_at = datetime('now') WHERE id = ? AND status = 'running'",
                        (job_id,),
                    )
                    db.commit()
                except Exception:
                    app.logger.exception("job %s fail-mark failed", job_id)
                return
            if step["status"] != "running":
                return


def _is_current(job_id: int, token: object) -> bool:
    with _lock:
        return _tokens.get(job_id) is token
