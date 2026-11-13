from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from app.db import get_db
from app.routes.auth import login_required
from app.services.mock_catalog import CATALOGS
from app.services.runner import (
    pause_job,
    resume_job,
    run_job,
    start_job,
)
from app.services.worker import ensure_worker, stop_worker

bp = Blueprint("jobs", __name__)


def _load_job(job_id: int):
    return get_db().execute(
        "SELECT * FROM crawl_jobs WHERE id = ?", (job_id,)
    ).fetchone()


def _record_count(job_id: int) -> int:
    return get_db().execute(
        "SELECT COUNT(*) AS c FROM crawl_records WHERE job_id = ?", (job_id,)
    ).fetchone()["c"]


def _card_context(job_id: int) -> dict | None:
    """取卡片上下文（只读；推进由后台驱动线程负责）。"""
    job = _load_job(job_id)
    if job is None:
        return None
    return {
        "job": job,
        "count": _record_count(job_id),
        "total_source": len(CATALOGS.get(job["seed_path"], [])),
    }


def _render_card(job_id: int):
    ctx = _card_context(job_id)
    if ctx is None:
        return '<section class="card">任务不存在</section>', 404
    return render_template("_job_card.html", **ctx), 200


def _detail_response(job_id: int, status_code: int = 200):
    """渲染详情页；参数/状态不符时回 409，让后台拒绝在协议层也可见。"""
    ctx = _card_context(job_id)
    if ctx is None:
        flash("任务不存在", "error")
        return redirect(url_for("jobs.list_jobs"))
    records = get_db().execute(
        "SELECT * FROM crawl_records WHERE job_id = ? ORDER BY id",
        (job_id,),
    ).fetchall()
    html = render_template(
        "job_detail.html", records=records, **ctx
    )
    return html, status_code


@bp.route("/")
@login_required
def list_jobs():
    db = get_db()
    jobs = db.execute("SELECT * FROM crawl_jobs ORDER BY id DESC").fetchall()
    counts = {
        row["job_id"]: row["c"]
        for row in db.execute(
            "SELECT job_id, COUNT(*) AS c FROM crawl_records GROUP BY job_id"
        )
    }
    return render_template(
        "jobs_list.html",
        jobs=jobs,
        counts=counts,
        total_sources={
            j["id"]: len(CATALOGS.get(j["seed_path"], [])) for j in jobs
        },
        seeds=sorted(CATALOGS.keys()),
    )


@bp.post("/jobs")
@login_required
def create_job():
    name = request.form.get("name", "").strip()
    seed = request.form.get("seed_path", "").strip()
    if not name or not seed:
        flash("请填写任务名与种子路径", "error")
        return redirect(url_for("jobs.list_jobs"))
    if seed not in CATALOGS:
        flash("种子路径必须是已注册的 mock 目录", "error")
        return redirect(url_for("jobs.list_jobs"))
    db = get_db()
    db.execute(
        "INSERT INTO crawl_jobs (name, seed_path, status) VALUES (?, ?, 'draft')",
        (name, seed),
    )
    db.commit()
    flash("任务已创建", "ok")
    return redirect(url_for("jobs.list_jobs"))


@bp.post("/jobs/<int:job_id>/start")
@login_required
def start_job_view(job_id: int):
    try:
        summary = start_job(job_id)
        if summary["status"] == "failed":
            flash("运行失败：种子目录不存在", "error")
        elif summary["status"] == "running":
            ensure_worker(current_app._get_current_object(), job_id)
            flash("已开始采集，可点「喊停」", "ok")
        else:
            flash("目录为空，任务直接完成", "ok")
    except ValueError as e:
        flash(str(e), "error")
        return _detail_response(job_id, 409)
    except Exception as e:
        flash(f"运行失败: {e}", "error")
        return _detail_response(job_id, 500)
    return redirect(url_for("jobs.detail", job_id=job_id))


@bp.post("/jobs/<int:job_id>/run")
@login_required
def run_job_view(job_id: int):
    """同步一次跑完；已停下的任务会被拒绝，必须走「续上」。"""
    try:
        summary = run_job(job_id)
        flash(
            f"运行完成：写入 {summary['inserted']} 条，去重跳过 {summary['skipped_dup']} 条",
            "ok",
        )
    except ValueError as e:
        flash(str(e), "error")
        return _detail_response(job_id, 409)
    except Exception as e:
        flash(f"运行失败: {e}", "error")
        return _detail_response(job_id, 500)
    return redirect(url_for("jobs.detail", job_id=job_id))


@bp.post("/jobs/<int:job_id>/pause")
@login_required
def pause_job_view(job_id: int):
    try:
        info = pause_job(job_id)
        stop_worker(job_id)
        flash(f"已停下，账本停在 {info['recorded']} 条", "ok")
    except ValueError as e:
        flash(str(e), "error")
        return _detail_response(job_id, 409)
    except Exception as e:
        flash(f"喊停失败: {e}", "error")
        return _detail_response(job_id, 500)
    return redirect(url_for("jobs.detail", job_id=job_id))


@bp.post("/jobs/<int:job_id>/resume")
@login_required
def resume_job_view(job_id: int):
    try:
        info = resume_job(job_id)
        ensure_worker(current_app._get_current_object(), job_id)
        flash(f"已续上，从编码 {info['resume_sku'] or '—'} 往后收", "ok")
    except ValueError as e:
        flash(str(e), "error")
        return _detail_response(job_id, 409)
    except Exception as e:
        flash(f"续上失败: {e}", "error")
        return _detail_response(job_id, 500)
    return redirect(url_for("jobs.detail", job_id=job_id))


@bp.get("/jobs/<int:job_id>/card")
@login_required
def card(job_id: int):
    return _render_card(job_id)


@bp.get("/jobs/<int:job_id>/records")
@login_required
def records(job_id: int):
    job = _load_job(job_id)
    if job is None:
        return '<p class="muted">任务不存在</p>', 404
    rows = get_db().execute(
        "SELECT * FROM crawl_records WHERE job_id = ? ORDER BY id", (job_id,)
    ).fetchall()
    return render_template("_job_records.html", records=rows, job=job), 200


@bp.get("/jobs/<int:job_id>")
@login_required
def detail(job_id: int):
    return _detail_response(job_id)
