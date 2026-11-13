from pathlib import Path

from flask import Flask

from app.config import Config
from app.db import close_db, init_db

STATUS_LABELS = {
    "draft": "待运行",
    "running": "采集中",
    "paused": "已停下",
    "done": "已完成",
    "failed": "失败",
}


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status)


def create_app(config_class=Config):
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).parent.parent / "templates"),
        static_folder=str(Path(__file__).parent.parent / "static"),
    )
    app.config.from_object(config_class)

    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)

    app.teardown_appcontext(close_db)

    app.jinja_env.globals["status_label"] = status_label

    from app.routes.auth import bp as auth_bp
    from app.routes.jobs import bp as jobs_bp
    from app.routes.mock import bp as mock_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(jobs_bp)
    app.register_blueprint(mock_bp)

    init_db(app)

    from app.services.worker import recover_running_jobs

    recover_running_jobs(app)
    return app
