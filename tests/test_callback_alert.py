import json

import httpx
import pytest

from app.callback import JavaCallbackClient
from app.config import Settings
from app.models import JobSubmitRequest
from app.registry import WorkerRegistry
from app.service import JobService
from tests.test_api import MockWorker


@pytest.mark.asyncio
@pytest.mark.parametrize("status,receipt,alert_count", [
    (200, {"code": "ACCEPTED"}, 0), (200, {"code": "DUPLICATE"}, 0),
    (200, {}, 1), (503, {}, 1),
])
async def test_callback_alert_is_independent_of_manager(tmp_path, monkeypatch, status, receipt, alert_count):
    notifications = []

    def respond(request):
        if request.url.host == "manager.test":
            return httpx.Response(status, json=receipt)
        assert request.url.host == "feishu.test"
        notifications.append(json.loads(request.content)["content"]["text"])
        return httpx.Response(200, json={"code": 19024})

    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs))
    settings = Settings(java_callback_base_url="https://manager.test", feishu_alert_webhook="https://feishu.test/robot",
                        database_path=str(tmp_path / "worker.db"))
    registry = WorkerRegistry()
    registry.register(MockWorker())
    jobs = JobService(registry, JavaCallbackClient(settings), settings)
    job = jobs.submit(JobSubmitRequest(worker="mock", runID=123, payload={"queryID": 456}))
    await jobs.tasks[job.jobID]
    assert len(notifications) == alert_count
    if alert_count:
        assert f"runID=123 queryID=456 jobID={job.jobID}" in notifications[0]
        assert job.errorCode == "CALLBACK_FAILED"
        if status == 503:
            assert "HTTP 503" in notifications[0]
    assert job.status.value == "SUCCESS"
