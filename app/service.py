"""任务提交、异步执行、超时和回调编排。"""

import asyncio
import hashlib
import json
import logging
from uuid import uuid4

from .alerts import alert_callback_failure
from .callback import CallbackSender
from .config import Settings
from .models import CallbackPayload, JobStatus, JobSubmitRequest, JobView, WorkerResult, utc_now
from .storage import JobStore
from .registry import WorkerRegistry
from .workers.countries import collect_countries

logger = logging.getLogger(__name__)


class JobService:
    """统一负责任务登记、并发控制、超时处理、本地审计和 Java 回调。"""

    def __init__(self, registry: WorkerRegistry, callback: CallbackSender, settings: Settings, store: JobStore | None = None) -> None:
        """初始化宿主依赖和每个进程的任务运行状态。"""
        self.registry = registry
        self.callback = callback
        self.settings = settings
        self.jobs: dict[str, JobView] = {}
        self.callback_urls: dict[str, str | None] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.semaphore = asyncio.Semaphore(settings.max_concurrent_jobs)
        self.store = store or JobStore(settings.database_path)

    def submit(self, request: JobSubmitRequest) -> JobView:
        """校验请求、登记任务并立即创建后台协程。"""
        self.registry.get(request.worker)
        job_id = uuid4().hex
        job = JobView(
            jobID=job_id,
            runID=request.runID,
            worker=request.worker,
            status=JobStatus.DISPATCHED,
            payload=request.payload,
            queryID=request.payload.get("queryID"),
            keyword=request.payload.get("keyword"),
            media=_media_for_worker(request.worker),
            windowStart=request.payload.get("windowStart"),
            windowEnd=request.payload.get("windowEnd"),
            acceptedAt=utc_now(),
        )
        self.jobs[job_id] = job
        self.callback_urls[job_id] = request.callbackUrl
        self.store.save_submitted(job)
        self.tasks[job_id] = asyncio.create_task(self._run(job_id))
        return job

    def get(self, job_id: str) -> JobView | None:
        """返回进程内任务快照；任务不存在时返回空值。"""
        return self.jobs.get(job_id)

    async def _run(self, job_id: str) -> None:
        """执行单个 Worker，并把成功、失败和超时统一转换为回调结果。"""
        job = self.jobs[job_id]
        job.status = JobStatus.RUNNING
        job.startedAt = utc_now()
        logger.info("开始任务 runID=%s queryID=%s jobID=%s worker=%s", job.runID, job.queryID, job.jobID, job.worker)
        try:
            async with self.semaphore:
                worker = self.registry.get(job.worker)
                if job.worker in {"google_transparency", "meta_library"}:
                    result = await collect_countries(worker, job.payload, self.settings.job_timeout_seconds)
                else:
                    result = await asyncio.wait_for(worker.run(job.payload), self.settings.job_timeout_seconds)
            self._apply_result(job, result)
        except asyncio.TimeoutError:
            self._apply_result(job, WorkerResult(status=JobStatus.TIMEOUT, errorCode="JOB_TIMEOUT", errorMessage="worker exceeded timeout"))
        except Exception as exc:  # noqa: BLE001 - Worker 异常必须回调 Java
            self._apply_result(job, WorkerResult(status=JobStatus.FAILED, errorCode="WORKER_EXCEPTION", errorMessage=str(exc)[:500]))
        finally:
            job.finishedAt = utc_now()
            try:
                await self._callback(job)
            finally:
                level = logging.ERROR if job.status in (JobStatus.FAILED, JobStatus.TIMEOUT) else logging.INFO
                logger.log(level, "结束任务 runID=%s queryID=%s jobID=%s worker=%s status=%s fetchedCount=%s errorCode=%s",
                           job.runID, job.queryID, job.jobID, job.worker, job.status.value, job.fetchedCount, job.errorCode)
                self.store.save_finished(job)

    @staticmethod
    def _apply_result(job: JobView, result: WorkerResult) -> None:
        """把 Worker 标准结果复制到可查询的任务模型。"""
        job.status = result.status
        job.fetchedCount = result.fetchedCount
        job.newCount = result.newCount
        job.updatedCount = result.updatedCount
        # 统一给每条素材补齐查询上下文，平台 Worker 只负责平台字段解析。
        job.items = []
        for item in result.items:
            normalized = dict(item)
            normalized.setdefault("queryID", job.queryID)
            normalized.setdefault("keyword", job.keyword)
            normalized.setdefault("media", job.media)
            configured_function = job.payload.get("creativeFunction")
            # 配置任务使用派发时的分类快照，留空也覆盖平台推断值。
            if job.queryID is not None or "creativeFunction" in job.payload:
                normalized["creativeFunction"] = configured_function or None
            # 内容指纹忽略查询上下文，避免同一素材被不同搜索词发现时误判为内容更新。
            normalized.setdefault("contentHash", _content_hash(normalized))
            job.items.append(normalized)
        job.errorCode = result.errorCode
        job.errorMessage = result.errorMessage
        job.restrictionType = result.restrictionType or _restriction_type(result.errorCode, result.errorMessage)
        job.restrictionEvidence = result.restrictionEvidence or (
            result.errorMessage[:1000] if job.restrictionType and result.errorMessage else None
        )

    async def _callback(self, job: JobView) -> None:
        """组装回调契约并发送给 Java；回调失败不覆盖原始抓取状态。"""
        payload = CallbackPayload(
            runID=job.runID,
            jobID=job.jobID,
            worker=job.worker,
            status=job.status,
            queryID=job.queryID,
            keyword=job.keyword,
            media=job.media,
            collectionDate=(job.finishedAt or utc_now()).date().isoformat(),
            collectedAt=job.finishedAt or utc_now(),
            windowStart=job.windowStart,
            windowEnd=job.windowEnd,
            fetchedCount=job.fetchedCount,
            newCount=job.newCount,
            updatedCount=job.updatedCount,
            items=job.items,
            errorCode=job.errorCode,
            errorMessage=job.errorMessage,
            restrictionType=job.restrictionType,
            restrictionEvidence=job.restrictionEvidence,
        )
        try:
            await self.callback.send(payload, self.callback_urls.get(job.jobID))
        except Exception as exc:  # 回调失败不覆盖原始 Worker 结果
            logger.error("回调失败 runID=%s queryID=%s jobID=%s exception=%s",
                         job.runID, job.queryID, job.jobID, type(exc).__name__)
            job.errorCode = job.errorCode or "CALLBACK_FAILED"
            job.errorMessage = job.errorMessage or str(exc)[:500]
            await alert_callback_failure(self.settings, job, exc)


def _content_hash(item: dict) -> str:
    """根据平台素材字段生成稳定指纹，排除查询和抓取时间等上下文。"""
    ignored = {"queryID", "keyword", "media", "collectionDate", "collectedAt"}
    content = {key: value for key, value in item.items() if key not in ignored}
    encoded = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _media_for_worker(worker: str) -> str:
    """把 Worker 名称映射为稳定的平台来源值。"""
    if worker.startswith("google"):
        return "GOOGLE_TRANSPARENCY_WEB"
    return "META_LIBRARY_WEB"


def _restriction_type(error_code: str | None, error_message: str | None) -> str | None:
    """把平台挑战和限流错误归一化，供 Java 运行记录保存。"""
    text = f"{error_code or ''} {error_message or ''}".lower()
    if any(token in text for token in ("captcha", "verify you are human", "访问验证", "验证码")):
        return "CAPTCHA_REQUIRED"
    if any(token in text for token in ("429", "rate limit", "too many requests", "暂时不可用", "api_busy")):
        return "RATE_LIMITED"
    if any(token in text for token in ("page_challenge", "access denied", "forbidden", "blocked", "ip")):
        return "IP_RESTRICTED"
    return None
