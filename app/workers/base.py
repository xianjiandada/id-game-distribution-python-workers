"""所有平台 Worker 共享的抽象基类。"""

from abc import ABC, abstractmethod
from ..models import WorkerResult


class BaseWorker(ABC):
    """网页、API 或数据处理 Worker 的统一抽象接口。"""

    name: str

    @abstractmethod
    async def run(self, payload: dict, proxy_url: str | None = None) -> WorkerResult:
        """执行一次任务并返回宿主可识别的标准结果。"""
        raise NotImplementedError
