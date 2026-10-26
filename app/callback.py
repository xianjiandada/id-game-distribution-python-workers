"""Java Manager 回调客户端。"""

from typing import Protocol
import httpx
from .config import Settings
from .models import CallbackPayload


class CallbackSender(Protocol):
    """定义回调发送器接口，便于生产实现和测试替身解耦。"""

    async def send(self, payload: CallbackPayload, callback_url: str | None) -> None:
        """把一次 Worker 结果发送给 Java Manager。"""
        ...


class JavaCallbackClient:
    """调用 Java Manager 的素材运行回调接口。"""

    def __init__(self, settings: Settings) -> None:
        """保存回调超时和默认地址等运行配置。"""
        self.settings = settings

    def _url(self, payload: CallbackPayload, callback_url: str | None) -> str | None:
        """优先使用任务指定地址，否则按运行 ID 拼接默认回调地址。"""
        if callback_url:
            return callback_url
        if self.settings.java_callback_base_url and payload.runId is not None:
            return f"{self.settings.java_callback_base_url.rstrip('/')}/{payload.runId}/callback"
        return None

    async def send(self, payload: CallbackPayload, callback_url: str | None) -> None:
        """发送 JSON 回调；没有配置地址时允许本地脱离 Java 单独运行。"""
        url = self._url(payload, callback_url)
        if not url:
            return
        # Manager 回调接口通过 @NoNeedLogin 和 @NoNeedCheckHeader 放行，
        # 当前协议不附加额外认证请求头。
        headers = {"Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=self.settings.callback_timeout_seconds) as client:
            response = await client.post(url, json=payload.model_dump(mode="json"), headers=headers)
            response.raise_for_status()
