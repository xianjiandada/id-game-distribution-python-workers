"""Java Manager 回调客户端。"""

from typing import Protocol
import httpx
from .config import Settings
from .models import CallbackPayload


class CallbackSender(Protocol):
    async def send(self, payload: CallbackPayload, callback_url: str | None) -> None:
        ...


class JavaCallbackClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _url(self, payload: CallbackPayload, callback_url: str | None) -> str | None:
        if callback_url:
            return callback_url
        if self.settings.java_callback_base_url and payload.runId is not None:
            return f"{self.settings.java_callback_base_url.rstrip('/')}/{payload.runId}/callback"
        return None

    async def send(self, payload: CallbackPayload, callback_url: str | None) -> None:
        url = self._url(payload, callback_url)
        if not url:
            return
        # Manager 回调接口通过 @NoNeedLogin 和 @NoNeedCheckHeader 放行，
        # 当前协议不附加额外认证请求头。
        headers = {"Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=self.settings.callback_timeout_seconds) as client:
            response = await client.post(url, json=payload.model_dump(mode="json"), headers=headers)
            response.raise_for_status()
