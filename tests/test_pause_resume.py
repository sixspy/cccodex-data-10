"""喊停 / 续上的六条验收脚本，一条只管一件事。

全部走 HTTP 层（`/jobs/<id>/start|pause|resume`），状态与账本一律从库里读真值。
测试环境关闭了后台驱动线程（`AUTO_COLLECT=False`），由用例显式推进一条一条，
断言才稳定；生产环境由 `app/services/worker.py` 驱动同样的 `collect_next`。
"""

import pytest

from app.db import get_db
from app.services.mock_catalog import CATALOGS
from app.services.runner import collect_next, pause_job, resume_job, start_job

ALPHA = "/mock/catalog/alpha"


def login(client):
    client.post(
        "/login",
        data={"username": "admin", "password": "123456"},
        follow_redirects=True,
    )


def create_job(app, client, seed=ALPHA):
    login(client)
    client.post(
        "/jobs", data={"name": "t", "seed_path": seed}, follow_redirects=True
    )
    with app.app_context():
        return get_db().execute(
            "SELECT id FROM crawl_jobs ORDER BY id DESC LIMIT 1"
        ).fetchone()["id"]


def state(app, job_id):
    with app.app_context():
        db = get_db()
        job = db.execute("SELECT * FROM crawl_jobs WHERE id = ?", (job_id,)).fetchone()
        codes = [
            r["item_code"]
            for r in db.execute(
                "SELECT item_code FROM crawl_records WHERE job_id = ? ORDER BY id",
                (job_id,),
            )
        ]
    return {
        "status": job["status"],
        "resume_sku": job["resume_sku"],
        "count": len(codes),
        "codes": codes,
    }


def collect_until_stopped(app, job_id, limit=50):
    """把当前任务从游标一路收到底，返回处理过的编码序列。"""
    collected = []
    with app.app_context():
        for _ in range(limit):
            if state(app, job_id)["status"] != "running":
                break
            collected.append(collect_next(job_id)["processed_sku"])
    return collected


def card_html(client, job_id):
    return client.get(f"/jobs/{job_id}/card").get_data(as_text=True)


@pytest.fixture()
def job(app, client):
    """建一个 draft 任务。"""
    return create_job(app, client)


# ── 1. 喊停之后条数不再往上跳 ───────────────────────────────────────────────
def test_pause_freezes_record_count(app, client, job):
    with app.app_context():
        start_job(job)
        collect_next(job)                       # 落 1 条，游标到 A-002
        info = pause_job(job)
    assert info["status"] == "paused"
    assert state(app, job)["count"] == 1

    # 停下后无论怎么点、怎么推，账本都不许再涨。
    assert client.post(f"/jobs/{job}/pause", follow_redirects=False).status_code == 409
    with app.app_context():
        with pytest.raises(ValueError):
            collect_next(job)
    assert state(app, job)["count"] == 1


# ── 2. 卡片能读到下一条编码，并写清两个按钮何时可点 ─────────────────────────
def test_card_shows_next_code_and_button_rules(app, client, job):
    with app.app_context():
        start_job(job)
        collect_next(job)
        pause_job(job)

    html = card_html(client, job)
    assert '<code class="next-sku">A-002</code>' in html   # 下一条未处理的编码
    assert "已停下" in html
    assert "续上</button>" in html                          # 已停下 -> 画续上
    assert "喊停</button>" not in html                      # 已停下 -> 不画喊停
    assert "按钮规则" in html                               # 卡片写明两个按钮何时可点
    assert "仅在<em>已停下</em>可点" in html


# ── 3. 续上不会造出第二次相同编码 ────────────────────────────────────────────
def test_resume_never_writes_same_code_twice(app, client, job):
    with app.app_context():
        start_job(job)
        collect_next(job)
        pause_job(job)
        resume_job(job)
        collect_next(job)
        pause_job(job)                          # 再停一次
        resume_job(job)                         # 再续一次
        collect_until_stopped(app, job)

    codes = state(app, job)["codes"]
    assert len(codes) == len(set(codes)), f"出现重复编码：{codes}"
    # UNIQUE(job_id, item_code) 也必须在库层兜底。
    with app.app_context():
        dup = get_db().execute(
            "SELECT item_code, COUNT(*) c FROM crawl_records WHERE job_id = ? "
            "GROUP BY item_code HAVING c > 1",
            (job,),
        ).fetchall()
    assert dup == []


# ── 4. 卡片不是已停下时，续上会被拒绝 ───────────────────────────────────────
@pytest.mark.parametrize("phase", ["draft", "running", "done"])
def test_resume_rejected_unless_paused(app, client, job, phase):
    if phase != "draft":
        with app.app_context():
            start_job(job)
            if phase == "done":
                collect_until_stopped(app, job)
    before = state(app, job)

    resp = client.post(f"/jobs/{job}/resume", follow_redirects=False)
    assert resp.status_code == 409
    assert "只有已停下的任务才能续上" in resp.get_data(as_text=True)

    after = state(app, job)
    assert after["status"] == before["status"]
    assert after["count"] == before["count"]
    assert client.get(f"/jobs/{job}/card").get_data(as_text=True).count("续上</button>") == 0


# ── 5. 续上不会把整本再收一遍（目录变动时也只认停下时那条编码）──────────────
def test_resume_only_continues_from_saved_code(app, client, job, monkeypatch):
    with app.app_context():
        start_job(job)
        collect_next(job)                       # A-001 已入账
        pause_job(job)
        assert state(app, job)["resume_sku"] == "A-002"

    # 目录后来有变动：前面多了一条、后面换了一条。
    monkeypatch.setitem(
        CATALOGS,
        ALPHA,
        [
            {"sku": "A-000", "title": "新增前置条目"},
            {"sku": "A-002", "title": "排水井巡查台账"},
            {"sku": "A-004", "title": "新增后续条目"},
        ],
    )
    assert client.post(f"/jobs/{job}/resume", follow_redirects=False).status_code == 302
    collected = collect_until_stopped(app, job)

    assert collected == ["A-002", "A-004"]      # 严格从记下的编码往后
    assert "A-000" not in collected             # 不因目录多出前项而回头重收
    assert "A-001" not in collected             # 已入账的不再收第二遍
    assert state(app, job)["count"] == 3        # A-001 + A-002（A-004 同标题被去重）


# ── 6. 已停下时另起一趟会被拒绝 ─────────────────────────────────────────────
def test_new_run_rejected_while_paused(app, client, job):
    with app.app_context():
        start_job(job)
        collect_next(job)
        pause_job(job)
    before = state(app, job)

    assert client.post(f"/jobs/{job}/start", follow_redirects=False).status_code == 409
    assert client.post(f"/jobs/{job}/run", follow_redirects=False).status_code == 409
    resp = client.post(f"/jobs/{job}/start", follow_redirects=True)
    assert "任务已停下，不能另起一趟" in resp.get_data(as_text=True)

    after = state(app, job)
    assert after == before                      # 状态、游标、账本都没被动过
