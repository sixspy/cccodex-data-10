import os
import tempfile

import pytest

from app import create_app
from app.db import get_db, init_db


@pytest.fixture()
def app():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.environ["DATABASE_PATH"] = path
    application = create_app()
    application.config["DATABASE"] = path
    application.config["TESTING"] = True
    # 测试里不用后台线程：每条用例自己显式推进，结果才可断言。
    application.config["AUTO_COLLECT"] = False
    application.config["COLLECT_STEP_DELAY"] = 0
    with application.app_context():
        init_db(application)
    yield application
    try:
        os.unlink(path)
    except OSError:
        pass


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def auth(client):
    resp = client.post(
        "/login",
        data={"username": "admin", "password": "123456"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    return client


@pytest.fixture()
def make_job(app, auth):
    """建任务并返回 job_id。"""

    def _make(seed="/mock/catalog/alpha", name="t"):
        auth.post(
            "/jobs",
            data={"name": name, "seed_path": seed},
            follow_redirects=True,
        )
        with app.app_context():
            return get_db().execute(
                "SELECT id FROM crawl_jobs ORDER BY id DESC LIMIT 1"
            ).fetchone()["id"]

    return _make


@pytest.fixture()
def job_state(app):
    """读取库里的任务状态与账本条数（含已落库编码）。"""

    def _state(job_id):
        with app.app_context():
            db = get_db()
            job = db.execute(
                "SELECT * FROM crawl_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            rows = db.execute(
                "SELECT item_code FROM crawl_records WHERE job_id = ?", (job_id,)
            ).fetchall()
            return {
                "status": job["status"],
                "resume_sku": job["resume_sku"],
                "count": len(rows),
                "codes": [r["item_code"] for r in rows],
            }

    return _state
