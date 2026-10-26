"""Python Worker 本地任务数据库，默认使用 SQLite 作为运行审计和断点记录。"""

import json
import sqlite3
from pathlib import Path

from .models import JobView


class JobStore:
    """保存 Worker 任务和素材结果；Java Manager 仍是业务主库。"""

    def __init__(self, database_path: str) -> None:
        self.database_path = database_path
        if database_path != ":memory:":
            Path(database_path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS worker_job (
                    job_id TEXT PRIMARY KEY,
                    run_id TEXT,
                    worker TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    fetched_count INTEGER NOT NULL DEFAULT 0,
                    new_count INTEGER NOT NULL DEFAULT 0,
                    updated_count INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT,
                    error_message TEXT,
                    accepted_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT
                );
                CREATE TABLE IF NOT EXISTS worker_item (
                    source TEXT NOT NULL,
                    external_ad_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    item_json TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (source, external_ad_id),
                    FOREIGN KEY (job_id) REFERENCES worker_job(job_id)
                );
                """
            )

    def save_submitted(self, job: JobView) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO worker_job
                (job_id, run_id, worker, status, payload_json, accepted_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    job.jobId,
                    str(job.runId) if job.runId is not None else None,
                    job.worker,
                    job.status.value,
                    json.dumps(job.payload, ensure_ascii=False),
                    job.acceptedAt.isoformat(),
                ),
            )

    def save_finished(self, job: JobView) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE worker_job SET status=?, fetched_count=?, new_count=?, updated_count=?,
                    error_code=?, error_message=?, started_at=?, finished_at=? WHERE job_id=?
                """,
                (
                    job.status.value,
                    job.fetchedCount,
                    job.newCount,
                    job.updatedCount,
                    job.errorCode,
                    job.errorMessage,
                    job.startedAt.isoformat() if job.startedAt else None,
                    job.finishedAt.isoformat() if job.finishedAt else None,
                    job.jobId,
                ),
            )
            for item in job.items:
                source = str(item.get("source") or job.worker)
                external_id = item.get("externalAdId")
                if not external_id:
                    continue
                connection.execute(
                    """
                    INSERT INTO worker_item (source, external_ad_id, job_id, item_json)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(source, external_ad_id) DO UPDATE SET
                        job_id=excluded.job_id, item_json=excluded.item_json,
                        last_seen_at=CURRENT_TIMESTAMP
                    """,
                    (source, str(external_id), job.jobId, json.dumps(item, ensure_ascii=False)),
                )
