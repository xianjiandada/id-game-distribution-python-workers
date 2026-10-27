"""读取 Python Worker 的环境配置。"""

from dataclasses import dataclass
import os


def _int_env(name: str, default: int) -> int:
    """读取整数环境变量，并在配置错误时尽早抛出清晰异常。"""
    value = os.getenv(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


@dataclass(frozen=True)
class Settings:
    """Python Worker 的进程级配置，字段值来自环境变量。"""

    # 服务名称，用于健康检查和日志识别。
    app_name: str = os.getenv("APP_NAME", "id-game-distribution-python-workers")
    # 当前运行环境，例如 local、staging 或 production。
    app_env: str = os.getenv("APP_ENV", "local")
    # Java Manager 回调基础地址，不包含具体 runID。
    java_callback_base_url: str = os.getenv("JAVA_CALLBACK_BASE_URL", "")
    # Java 回调的 HTTP 超时时间，单位为秒。
    callback_timeout_seconds: int = _int_env("CALLBACK_TIMEOUT_SECONDS", 10)
    # 单个网页 Worker 的最大执行时间，单位为秒。
    job_timeout_seconds: int = _int_env("JOB_TIMEOUT_SECONDS", 900)
    # 同时执行的最大任务数，避免浏览器和出口连接过载。
    max_concurrent_jobs: int = _int_env("MAX_CONCURRENT_JOBS", 2)
    # 未指定逻辑 Profile 时使用的默认代理地址。
    proxy_default_url: str = os.getenv("PROXY_DEFAULT_URL", "")
    # Python 本地审计数据库路径。
    database_path: str = os.getenv("DATABASE_PATH", "data/worker.db")


settings = Settings()
