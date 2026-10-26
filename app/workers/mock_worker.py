import asyncio
from .base import BaseWorker
from ..models import JobStatus, WorkerResult


class MockWorker(BaseWorker):
    """用于本地验证异步调度、失败回调和持久化的模拟 Worker。"""

    name = "mock"

    async def run(self, payload: dict, proxy_url: str | None = None) -> WorkerResult:
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
