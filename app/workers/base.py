"""所有平台 Worker 共享的抽象基类。"""

from abc import ABC, abstractmethod
from ..models import WorkerResult


class BaseWorker(ABC):
    name: str

    @abstractmethod
    async def run(self, payload: dict, proxy_url: str | None = None) -> WorkerResult:
        raise NotImplementedError
