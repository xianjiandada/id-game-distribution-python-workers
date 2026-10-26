import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from app.callback import CallbackSender
from app.config import Settings
from app.main import create_app
from app.models import CallbackPayload
from app.models import JobStatus, WorkerResult
from app.workers.base import BaseWorker


class RecordingCallback(CallbackSender):
    def __init__(self):
        self.payloads: list[CallbackPayload] = []

    async def send(self, payload: CallbackPayload, callback_url: str | None) -> None:
        self.payloads.append(payload)


class ImmediateManualWorker(BaseWorker):
    """测试用的立即人工介入 Worker，避免 API 测试依赖外网。"""

    name = "google_transparency"

    async def run(self, payload: dict, proxy_url: str | None = None) -> WorkerResult:
        return WorkerResult(status=JobStatus.WAITING_MANUAL, errorCode="TEST_MANUAL")


@pytest.mark.asyncio
async def test_mock_job_calls_java_callback_and_is_queryable():
    app = create_app()
    callback = RecordingCallback()
    app.state.jobs.callback = callback
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/jobs", json={"worker": "mock", "runId": 99, "payload": {"items": [{"externalAdId": "a1"}]}})
        assert response.status_code == 202
        job_id = response.json()["jobId"]
        for _ in range(20):
            if callback.payloads:
                break
            await asyncio.sleep(0.01)
        job = (await client.get(f"/jobs/{job_id}")).json()
    assert callback.payloads[0].status.value == "SUCCESS"
    assert job["status"] == "SUCCESS"
    assert job["newCount"] == 1


@pytest.mark.asyncio
async def test_failure_and_waiting_manual_are_explicit():
    app = create_app()
    app.state.registry._workers["google_transparency"] = ImmediateManualWorker()
    callback = RecordingCallback()
    app.state.jobs.callback = callback
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        failed = await client.post("/jobs", json={"worker": "mock", "runId": 1, "payload": {"fail": True}})
        waiting = await client.post("/jobs", json={"worker": "google_transparency", "runId": 2})
        await asyncio.sleep(0.03)
    assert failed.status_code == 202
    assert waiting.status_code == 202
    assert {p.status.value for p in callback.payloads} == {"FAILED", "WAITING_MANUAL"}


@pytest.mark.asyncio
async def test_unknown_worker_is_rejected():
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/jobs", json={"worker": "unknown"})
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_required_proxy_without_server_configuration_is_rejected():
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/jobs",
            json={"worker": "google_transparency", "runId": 3, "proxyProfile": "google_hk", "proxyRequired": True},
        )
    assert response.status_code == 503
    assert "proxy profile is not configured" in response.json()["detail"]
