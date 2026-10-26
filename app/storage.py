"""Python Worker 本地任务数据库，默认使用 SQLite 作为运行审计和断点记录。"""

import json
import sqlite3
from pathlib import Path

from .models import JobView


class JobStore:
    """保存 Worker 任务和素材结果；Java Manager 仍是业务主库。"""

    def __init__(self, database_path: str) -> None:
        """创建 SQLite 数据库目录并确保审计表存在。"""
        self.database_path = database_path
        if database_path != ":memory:":
            Path(database_path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        """打开一个短生命周期连接，并使用 Row 方便后续扩展查询。"""
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        """初始化任务表和素材表；唯一键保证同一素材不会重复保存。"""
        with self._connect() as connection:
            connection.executescript(
                """
                -- worker_job 保存 Python 任务的状态、计数、错误和时间。
                CREATE TABLE IF NOT EXISTS worker_job (
                    -- Python 任务唯一 ID。
                    job_id TEXT PRIMARY KEY,
                    -- Java 业务运行 ID，可为空以支持独立本地任务。
                    run_id TEXT,
                    -- Worker 注册名称。
                    worker TEXT NOT NULL,
                    -- 任务当前状态。
                    status TEXT NOT NULL,
                    -- Worker 原始请求参数 JSON。
                    payload_json TEXT NOT NULL,
                    -- 抓取素材总数。
                    fetched_count INTEGER NOT NULL DEFAULT 0,
                    -- 新素材数量。
                    new_count INTEGER NOT NULL DEFAULT 0,
                    -- 更新素材数量。
                    updated_count INTEGER NOT NULL DEFAULT 0,
                    -- 机器可读错误码。
                    error_code TEXT,
                    -- 错误或人工介入说明。
                    error_message TEXT,
                    -- 任务接收时间。
                    accepted_at TEXT NOT NULL,
                    -- Worker 开始执行时间。
                    started_at TEXT,
                    -- Worker 结束时间。
                    finished_at TEXT
                );
                -- worker_item 保存平台素材快照，按来源和外部素材 ID 幂等。
                CREATE TABLE IF NOT EXISTS worker_item (
                    -- 平台或标准化来源名称。
                    source TEXT NOT NULL,
                    -- 平台侧广告或素材外部 ID。
                    external_ad_id TEXT NOT NULL,
                    -- 最近一次保存该素材的 Python 任务 ID。
                    job_id TEXT NOT NULL,
                    -- 素材标准化 JSON 快照。
                    item_json TEXT NOT NULL,
                    -- 首次发现时间。
                    first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    -- 最近一次发现或更新的时间。
                    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    -- 同一来源和外部 ID 只保留一条。
                    PRIMARY KEY (source, external_ad_id),
                    FOREIGN KEY (job_id) REFERENCES worker_job(job_id)
                );
                """
            )

    def save_submitted(self, job: JobView) -> None:
        """保存刚接收的任务，支持同一 jobId 重试覆盖。"""
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
        """更新任务最终状态，并同步保存每条素材的最新快照。"""
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
