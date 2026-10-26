"""读取 Python Worker 的环境配置。"""

from dataclasses import dataclass
import os


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


@dataclass(frozen=True)
class Settings:
    app_name: str = os.getenv("APP_NAME", "id-game-distribution-python-workers")
    app_env: str = os.getenv("APP_ENV", "local")
    java_callback_base_url: str = os.getenv("JAVA_CALLBACK_BASE_URL", "")
    callback_timeout_seconds: int = _int_env("CALLBACK_TIMEOUT_SECONDS", 10)
    job_timeout_seconds: int = _int_env("JOB_TIMEOUT_SECONDS", 900)
    max_concurrent_jobs: int = _int_env("MAX_CONCURRENT_JOBS", 2)
    proxy_default_url: str = os.getenv("PROXY_DEFAULT_URL", "")
    database_path: str = os.getenv("DATABASE_PATH", "data/worker.db")


settings = Settings()
