"""Worker 注册表，负责按名称找到具体的任务实现。"""

from typing import Protocol
from .models import WorkerResult


class Worker(Protocol):
    """所有 Worker 必须提供名称和异步执行方法。"""

    name: str

    async def run(self, payload: dict, proxy_url: str | None = None) -> WorkerResult:
        """执行平台任务并返回统一结果。"""
        ...


class WorkerRegistry:
    """维护 Worker 名称到实现对象的唯一映射。"""

    def __init__(self) -> None:
        """创建空注册表，应用启动时逐个注册实现。"""
        self._workers: dict[str, Worker] = {}

    def register(self, worker: Worker) -> None:
        """注册 Worker，并拒绝同名实现覆盖已有路由。"""
        if worker.name in self._workers:
            raise ValueError(f"worker already registered: {worker.name}")
        self._workers[worker.name] = worker

    def get(self, name: str) -> Worker:
        """按名称获取 Worker；名称不存在时转换为业务可识别异常。"""
        try:
            return self._workers[name]
        except KeyError as exc:
            raise KeyError(f"unknown worker: {name}") from exc

    def names(self) -> list[str]:
        """返回排序后的 Worker 名称，便于接口稳定展示。"""
        return sorted(self._workers)
