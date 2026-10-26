"""Worker 注册表，负责按名称找到具体的任务实现。"""

from typing import Protocol
from .models import WorkerResult


class Worker(Protocol):
    name: str

    async def run(self, payload: dict, proxy_url: str | None = None) -> WorkerResult:
        ...


class WorkerRegistry:
    def __init__(self) -> None:
        self._workers: dict[str, Worker] = {}

    def register(self, worker: Worker) -> None:
        if worker.name in self._workers:
            raise ValueError(f"worker already registered: {worker.name}")
        self._workers[worker.name] = worker

    def get(self, name: str) -> Worker:
        try:
            return self._workers[name]
        except KeyError as exc:
            raise KeyError(f"unknown worker: {name}") from exc

    def names(self) -> list[str]:
        return sorted(self._workers)
