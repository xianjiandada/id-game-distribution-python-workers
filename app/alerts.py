"""Worker 在 Manager 不可用时直接发送飞书告警。"""

import asyncio
import logging

import httpx

from .config import Settings
from .models import JobView

logger = logging.getLogger(__name__)


async def alert_callback_failure(settings: Settings, job: JobView, failure: Exception) -> None:
    if not settings.feishu_alert_webhook:
        return
    reason = type(failure).__name__
    if isinstance(failure, httpx.HTTPStatusError):
        reason += f" HTTP {failure.response.status_code}"
    message = (f"【素材库预警】Worker 回调 Java 失败\n环境={settings.app_env} worker={job.worker}"
               f"\nrunID={job.runID} queryID={job.queryID} jobID={job.jobID}"
               f"\n抓取状态={job.status.value} fetchedCount={job.fetchedCount}\nCALLBACK_FAILED {reason}")
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await asyncio.wait_for(client.post(
                settings.feishu_alert_webhook, json={"msg_type": "text", "content": {"text": message}},
            ), timeout=5)
            response.raise_for_status()
            if response.json().get("code") != 0:
                raise ValueError("飞书机器人未接受告警")
    except Exception as exc:
        logger.error("飞书通知失败 runID=%s jobID=%s exception=%s", job.runID, job.jobID, type(exc).__name__)
