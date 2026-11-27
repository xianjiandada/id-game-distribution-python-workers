"""Worker 与 Java Manager 共用按服务器本地日期划分的日志根目录。"""

from copy import deepcopy
from datetime import date, datetime, timedelta
import logging
import os
from pathlib import Path

from uvicorn.config import LOGGING_CONFIG


class DailyWorkerLog(logging.Handler):
    def __init__(self):
        super().__init__()
        self.root = Path(os.environ.get("LOG_PATH") or "logs").resolve()
        self.days = int(os.environ.get("LOG_MAX_HISTORY") or 15)
        if self.days < 1:
            raise ValueError("LOG_MAX_HISTORY must be positive")
        self.current_day = None

    def _expire(self, today: date) -> None:
        oldest = today - timedelta(days=self.days - 1)
        for directory in self.root.glob("????-??-??/sf_worker"):
            if directory.resolve() != directory:
                continue
            try:
                day = date.fromisoformat(directory.parent.name)
            except ValueError:
                continue
            if day < oldest:
                for name in ("sf_worker_info.log", "sf_worker_error.log"):
                    (directory / name).unlink(missing_ok=True)
                if directory.is_dir() and not any(directory.iterdir()):
                    directory.rmdir()

    def emit(self, record):
        try:
            day = datetime.fromtimestamp(record.created).date()
            directory = self.root / day.isoformat() / "sf_worker"
            changed = self.current_day != day
            if changed:
                directory.mkdir(parents=True, exist_ok=True)
            line = self.format(record) + "\n"
            with (directory / "sf_worker_info.log").open("a", encoding="utf-8") as stream:
                stream.write(line)
            if record.levelno >= logging.ERROR:
                with (directory / "sf_worker_error.log").open("a", encoding="utf-8") as stream:
                    stream.write(line)
            if changed:
                self.current_day = day
                self._expire(day)
        except Exception:
            self.handleError(record)


def worker_log_config() -> dict:
    config = deepcopy(LOGGING_CONFIG)
    config["formatters"]["file"] = {
        "format": "%(asctime)s [%(process)d] %(levelname)s %(name)s - %(message)s",
    }
    config["handlers"]["daily"] = {
        "()": DailyWorkerLog, "formatter": "file", "level": "INFO",
    }
    for name in ("uvicorn", "uvicorn.access"):
        config["loggers"][name]["handlers"].append("daily")
    config["loggers"]["app"] = {
        "handlers": ["default", "daily"], "level": "INFO", "propagate": False,
    }
    return config
