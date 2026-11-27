from datetime import datetime
import logging

from app.worker_logging import DailyWorkerLog


def test_rollover_retention_and_restart_use_only_worker_files(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_PATH", str(tmp_path))
    monkeypatch.setenv("LOG_MAX_HISTORY", "2")
    handler = DailyWorkerLog()

    def write(day, level, message):
        record = logging.LogRecord("app.service", level, __file__, 1, message, (), None)
        record.created = datetime(2026, 11, day).timestamp()
        handler.handle(record)

    write(24, logging.ERROR, "旧错误")
    java = tmp_path / "2026-11-24/sf_manager/sf_manager_info_0.log"
    java.parent.mkdir()
    java.write_text("java", encoding="utf-8")
    write(25, logging.INFO, "保留")
    write(26, logging.INFO, "开始")
    handler = DailyWorkerLog()
    write(26, logging.ERROR, "失败")

    assert java.is_file()
    assert not (java.parent.parent / "sf_worker").exists()
    assert (tmp_path / "2026-11-25/sf_worker/sf_worker_info.log").is_file()
    current = tmp_path / "2026-11-26/sf_worker"
    assert (current / "sf_worker_info.log").read_text(encoding="utf-8").splitlines() == ["开始", "失败"]
    assert (current / "sf_worker_error.log").read_text(encoding="utf-8").splitlines() == ["失败"]
