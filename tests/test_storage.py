import json

from app.models import JobStatus, JobView
from app.storage import JobStore


def test_job_store_persists_job_and_deduplicates_items(tmp_path):
    store = JobStore(str(tmp_path / "worker.db"))
    job = JobView(
        jobId="job-1",
        runId=123,
        worker="meta_library",
        status=JobStatus.SUCCESS,
        payload={"keyword": "螺丝"},
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
