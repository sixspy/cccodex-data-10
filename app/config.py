import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-change-me-in-production")
    DATABASE = os.environ.get(
        "DATABASE_PATH",
        str(BASE_DIR / "instance" / "crawl_lab.db"),
    )
    ADMIN_USER = "admin"
    ADMIN_PASSWORD = "123456"
    # 后台采集驱动：每隔 COLLECT_STEP_DELAY 秒推进一条；AUTO_COLLECT=False 时只靠页面驱动。
    AUTO_COLLECT = os.environ.get("AUTO_COLLECT", "1") == "1"
    COLLECT_STEP_DELAY = float(os.environ.get("COLLECT_STEP_DELAY", "2.0"))

