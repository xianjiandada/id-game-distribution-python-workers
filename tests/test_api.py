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


class MockWorker(BaseWorker):
    """用于本地验证异步调度、失败回调和持久化的模拟 Worker。"""

    name = "mock"

    async def run(self, payload: dict) -> WorkerResult:
        """按请求参数模拟耗时、失败或返回素材列表。"""
        # Mock Worker 用于本地验证异步执行和回调链路。
        await asyncio.sleep(float(payload.get("sleepSeconds", 0)))
        if payload.get("fail"):
            return WorkerResult(status=JobStatus.FAILED, errorCode="MOCK_FAILURE", errorMessage="mock failure")
        items = payload.get("items", [])
        return WorkerResult(
            fetchedCount=len(items),
            newCount=len(items),
            items=items,
        )


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

    async def run(self, payload: dict) -> WorkerResult:
        """立即返回人工介入状态，避免测试访问真实 Google 页面。"""
        return WorkerResult(status=JobStatus.WAITING_MANUAL, errorCode="TEST_MANUAL")


@pytest.mark.asyncio
async def test_mock_job_calls_java_callback_and_is_queryable():
    """验证提交接口立即返回，并最终得到成功状态和素材数量。"""
    app = create_app()
    app.state.registry.register(MockWorker())
    callback = RecordingCallback()
    app.state.jobs.callback = callback
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/jobs", json={"worker": "mock", "runID": 99, "payload": {"items": [{"externalAdID": "a1"}]}})
        assert response.status_code == 202
        job_id = response.json()["jobID"]
        for _ in range(20):
            if callback.payloads:
                break
            await asyncio.sleep(0.01)
        job = (await client.get(f"/jobs/{job_id}")).json()
    assert callback.payloads[0].status.value == "SUCCESS"
    assert job["status"] == "SUCCESS"
    assert job["newCount"] == 1


@pytest.mark.asyncio
async def test_query_context_is_kept_in_callback_and_items():
    """验证查询配置和搜索词会随素材回调保留，便于 Java 入库。"""
    app = create_app()
    app.state.registry.register(MockWorker())
    callback = RecordingCallback()
    app.state.jobs.callback = callback
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/jobs",
            json={
                "worker": "mock",
                "runID": 100,
                "payload": {
                    "queryID": 20001,
                    "keyword": "竞品A",
                    "items": [{"externalAdID": "context-1"}],
                },
            },
        )
        assert response.status_code == 202
        for _ in range(20):
            if callback.payloads:
                break
            await asyncio.sleep(0.01)
    assert callback.payloads[0].queryID == 20001
    assert callback.payloads[0].items[0]["keyword"] == "竞品A"


@pytest.mark.asyncio
async def test_configured_creative_function_overrides_worker_value():
    """查询配置中的素材分类应覆盖 Worker 自动推断值并随回调返回。"""
    app = create_app()
    app.state.registry.register(MockWorker())
    callback = RecordingCallback()
    app.state.jobs.callback = callback
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/jobs",
            json={
                "worker": "mock",
                "runID": 101,
                "payload": {
                    "creativeFunction": "文件恢复",
                    "items": [{"externalAdID": "category-1", "creativeFunction": "清理"}],
                },
            },
        )
        assert response.status_code == 202
        for _ in range(20):
            if callback.payloads:
                break
            await asyncio.sleep(0.01)
    assert callback.payloads[0].items[0]["creativeFunction"] == "文件恢复"


@pytest.mark.asyncio
async def test_failure_and_waiting_manual_are_explicit():
    """验证失败和人工介入状态不会被吞掉。"""
    app = create_app()
    app.state.registry._workers["google_transparency"] = ImmediateManualWorker()
    app.state.registry.register(MockWorker())
    callback = RecordingCallback()
    app.state.jobs.callback = callback
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        failed = await client.post("/jobs", json={"worker": "mock", "runID": 1, "payload": {"fail": True}})
        waiting = await client.post("/jobs", json={"worker": "google_transparency", "runID": 2})
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
