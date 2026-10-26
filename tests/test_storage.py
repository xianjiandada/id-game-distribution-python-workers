"""SQLite 本地任务审计和素材幂等保存测试。"""

import json

from app.models import JobStatus, JobView
from app.storage import JobStore


def test_job_store_persists_job_and_deduplicates_items(tmp_path):
    """验证任务可保存，重复素材会更新快照而不是新增行。"""
    store = JobStore(str(tmp_path / "worker.db"))
    job = JobView(
        jobId="job-1",
        runId=123,
        worker="meta_library",
        status=JobStatus.SUCCESS,
        payload={"keyword": "螺丝"},
        queryId=20001,
        appId=1001,
        productName="示例产品",
        keyword="螺丝",
        fetchedCount=1,
        newCount=1,
        items=[{"source": "META_LIBRARY_WEB", "externalAdId": "ad-1", "copyText": "A"}],
        acceptedAt=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )
    store.save_submitted(job)
    store.save_finished(job)
    job.items[0]["copyText"] = "B"
    store.save_finished(job)
    import sqlite3
    with sqlite3.connect(tmp_path / "worker.db") as connection:
        assert connection.execute("select count(*) from worker_job").fetchone()[0] == 1
        row = connection.execute("select item_json from worker_item").fetchone()[0]
        assert json.loads(row)["copyText"] == "B"
        # 每次任务对素材的观察都保留一条历史，后续可按日期比较。
        assert connection.execute("select count(*) from worker_item_observation").fetchone()[0] == 1
        job_row = connection.execute("select query_id, app_id, product_name, keyword from worker_job").fetchone()
        assert tuple(job_row) == ("20001", "1001", "示例产品", "螺丝")
