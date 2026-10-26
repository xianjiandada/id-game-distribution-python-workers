import asyncio

"""FastAPI 任务接口的离线测试，不访问真实 Meta 或 Google 页面。"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.callback import CallbackSender
from app.config import Settings
from app.main import create_app
from app.models import CallbackPayload
from app.models import JobStatus, WorkerResult
from app.workers.base import BaseWorker


class RecordingCallback(CallbackSender):
    """记录回调内容，验证任务完成后确实触发 Java 回调替身。"""

    def __init__(self):
        """创建一个只记录回调而不发网络请求的测试替身。"""
        self.payloads: list[CallbackPayload] = []

    async def send(self, payload: CallbackPayload, callback_url: str | None) -> None:
        """把回调保存到列表，供断言检查。"""
        self.payloads.append(payload)


class ImmediateManualWorker(BaseWorker):
    """测试用的立即人工介入 Worker，避免 API 测试依赖外网。"""

    name = "google_transparency"

    async def run(self, payload: dict, proxy_url: str | None = None) -> WorkerResult:
        """立即返回人工介入状态，避免测试访问真实 Google 页面。"""
        return WorkerResult(status=JobStatus.WAITING_MANUAL, errorCode="TEST_MANUAL")


@pytest.mark.asyncio
async def test_mock_job_calls_java_callback_and_is_queryable():
    """验证提交接口立即返回，并最终得到成功状态和素材数量。"""
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
async def test_product_query_context_is_kept_in_callback_and_items():
    """验证 appID、查询配置和搜索词会随素材回调保留，便于 Java 按产品和日期入库。"""
    app = create_app()
    callback = RecordingCallback()
    app.state.jobs.callback = callback
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/jobs",
            json={
                "worker": "mock",
                "runId": 100,
                "payload": {
                    "queryId": 20001,
                    "appId": 1001,
                    "productName": "示例产品",
                    "keyword": "竞品A",
                    "items": [{"externalAdId": "context-1"}],
                },
            },
        )
        assert response.status_code == 202
        for _ in range(20):
            if callback.payloads:
                break
            await asyncio.sleep(0.01)
    assert callback.payloads[0].queryId == 20001
    assert callback.payloads[0].appId == 1001
    assert callback.payloads[0].productName == "示例产品"
    assert callback.payloads[0].items[0]["keyword"] == "竞品A"


@pytest.mark.asyncio
async def test_failure_and_waiting_manual_are_explicit():
    """验证失败和人工介入状态不会被吞掉。"""
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
    """验证未注册 Worker 会被接口拒绝。"""
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/jobs", json={"worker": "unknown"})
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_required_proxy_without_server_configuration_is_rejected():
    """验证强制代理但未配置时返回 503。"""
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/jobs",
            json={"worker": "google_transparency", "runId": 3, "proxyProfile": "google_hk", "proxyRequired": True},
        )
    assert response.status_code == 503
    assert "proxy profile is not configured" in response.json()["detail"]
