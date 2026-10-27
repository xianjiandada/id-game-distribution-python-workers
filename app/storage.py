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
                    jobID TEXT PRIMARY KEY,
                    -- Java 业务运行 ID，可为空以支持独立本地任务。
                    runID TEXT,
                    -- Java 侧素材查询配置 ID。
                    queryID TEXT,
                    -- 产品 appID，用于按产品区分素材。
                    appID TEXT,
                    -- 产品名称快照。
                    productName TEXT,
                    -- 本次运行的搜索词或广告主名称。
                    keyword TEXT,
                    -- Worker 注册名称。
                    worker TEXT NOT NULL,
                    -- 任务当前状态。
                    status TEXT NOT NULL,
                    -- Worker 原始请求参数 JSON。
                    payloadJson TEXT NOT NULL,
                    -- 抓取素材总数。
                    fetchedCount INTEGER NOT NULL DEFAULT 0,
                    -- 新素材数量。
                    newCount INTEGER NOT NULL DEFAULT 0,
                    -- 更新素材数量。
                    updatedCount INTEGER NOT NULL DEFAULT 0,
                    -- 机器可读错误码。
                    errorCode TEXT,
                    -- 错误或人工介入说明。
                    errorMessage TEXT,
                    -- 任务接收时间。
                    acceptedAt TEXT NOT NULL,
                    -- Worker 开始执行时间。
                    startedAt TEXT,
                    -- Worker 结束时间。
                    finishedAt TEXT
                );
                -- worker_item 保存平台素材快照，按来源和外部素材 ID 幂等。
                CREATE TABLE IF NOT EXISTS worker_item (
                    -- 平台或标准化来源名称。
                    media TEXT NOT NULL,
                    -- 平台侧广告或素材外部 ID。
                    externalAdID TEXT NOT NULL,
                    -- 最近一次保存该素材的 Python 任务 ID。
                    jobID TEXT NOT NULL,
                    -- 产品 appID 快照。
                    appID TEXT,
                    -- 查询配置 ID 快照。
                    queryID TEXT,
                    -- 搜索词快照。
                    keyword TEXT,
                    -- 素材标准化 JSON 快照。
                    itemJson TEXT NOT NULL,
                    -- 首次发现时间。
                    firstSeenAt TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    -- 最近一次发现或更新的时间。
                    lastSeenAt TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    -- 同一来源和外部 ID 只保留一条。
                    PRIMARY KEY (media, externalAdID),
                    FOREIGN KEY (jobID) REFERENCES worker_job(jobID)
                );
                -- worker_item_observation 保存每次运行看到素材的历史，支持按天判断新增和更新。
                CREATE TABLE IF NOT EXISTS worker_item_observation (
                    -- 观察记录自增主键。
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    -- 本次 Python 任务 ID。
                    jobID TEXT NOT NULL,
                    -- 平台来源。
                    media TEXT NOT NULL,
                    -- 平台侧素材 ID。
                    externalAdID TEXT NOT NULL,
                    -- 产品 appID。
                    appID TEXT,
                    -- 查询配置 ID。
                    queryID TEXT,
                    -- 本次采集日期（UTC）。
                    collectionDate TEXT NOT NULL,
                    -- 本次观察时间。
                    observedAt TEXT NOT NULL,
                    -- 本次素材快照 JSON。
                    itemJson TEXT NOT NULL,
                    FOREIGN KEY (jobID) REFERENCES worker_job(jobID),
                    UNIQUE (jobID, media, externalAdID)
                );
                """
            )
    def save_submitted(self, job: JobView) -> None:
        """保存刚接收的任务，支持同一 jobID 重试覆盖。"""
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO worker_job
                (jobID, runID, queryID, appID, productName, keyword, worker, status, payloadJson, acceptedAt)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.jobID,
                    str(job.runID) if job.runID is not None else None,
                    str(job.queryID) if job.queryID is not None else None,
                    str(job.appID) if job.appID is not None else None,
                    job.productName,
                    job.keyword,
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
                UPDATE worker_job SET status=?, fetchedCount=?, newCount=?, updatedCount=?,
                    errorCode=?, errorMessage=?, startedAt=?, finishedAt=? WHERE jobID=?
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
                    job.jobID,
                ),
            )
            for item in job.items:
                media = str(item.get("media") or job.worker)
                external_id = item.get("externalAdID")
                if not external_id:
                    continue
                connection.execute(
                    """
                    INSERT INTO worker_item (media, externalAdID, jobID, appID, queryID, keyword, itemJson)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(media, externalAdID) DO UPDATE SET
                        jobID=excluded.jobID, appID=excluded.appID, queryID=excluded.queryID,
                        keyword=excluded.keyword, itemJson=excluded.itemJson,
                        lastSeenAt=CURRENT_TIMESTAMP
                    """,
                    (
                        media,
                        str(external_id),
                        job.jobID,
                        str(item.get("appID") or job.appID) if (item.get("appID") or job.appID) is not None else None,
                        str(item.get("queryID") or job.queryID) if (item.get("queryID") or job.queryID) is not None else None,
                        item.get("keyword") or job.keyword,
                        json.dumps(item, ensure_ascii=False),
                    ),
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO worker_item_observation
                    (jobID, media, externalAdID, appID, queryID, collectionDate, observedAt, itemJson)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job.jobID,
                        media,
                        str(external_id),
                        str(item.get("appID") or job.appID) if (item.get("appID") or job.appID) is not None else None,
                        str(item.get("queryID") or job.queryID) if (item.get("queryID") or job.queryID) is not None else None,
                        (job.finishedAt or job.acceptedAt).date().isoformat(),
                        (job.finishedAt or job.acceptedAt).isoformat(),
                        json.dumps(item, ensure_ascii=False),
                    ),
                )
