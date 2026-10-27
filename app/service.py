"""任务提交、异步执行、超时和回调编排。"""

import asyncio
import hashlib
import json
from uuid import uuid4

from .callback import CallbackSender
from .config import Settings
from .models import CallbackPayload, JobStatus, JobSubmitRequest, JobView, WorkerResult, utc_now
from .proxy import ProxyLease, ProxyResolver
from .storage import JobStore
from .registry import WorkerRegistry


class JobService:
    """统一负责任务登记、并发控制、超时处理、本地审计和 Java 回调。"""

    def __init__(self, registry: WorkerRegistry, callback: CallbackSender, settings: Settings, proxy_resolver: ProxyResolver | None = None, store: JobStore | None = None) -> None:
        """初始化宿主依赖和每个进程的任务运行状态。"""
        self.registry = registry
        self.callback = callback
        self.settings = settings
        self.jobs: dict[str, JobView] = {}
        self.callback_urls: dict[str, str | None] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.semaphore = asyncio.Semaphore(settings.max_concurrent_jobs)
        self.proxy_resolver = proxy_resolver or ProxyResolver(settings)
        self.store = store or JobStore(settings.database_path)
        self.proxies: dict[str, ProxyLease | None] = {}

    def submit(self, request: JobSubmitRequest) -> JobView:
        """校验请求、解析代理、登记任务并立即创建后台协程。"""
        self.registry.get(request.worker)
        proxy = self.proxy_resolver.resolve(request.proxyProfile, request.proxyRequired)
        job_id = uuid4().hex
        job = JobView(
            jobID=job_id,
            runID=request.runID,
            worker=request.worker,
            status=JobStatus.DISPATCHED,
            payload=request.payload,
            queryID=request.payload.get("queryID"),
            appID=request.payload.get("appID"),
            productName=request.payload.get("productName"),
            keyword=request.payload.get("keyword") or request.payload.get("advertiserName"),
            media=("GOOGLE_TRANSPARENCY_WEB" if request.worker.startswith("google") else "META_LIBRARY_WEB"),
            windowStart=request.payload.get("windowStart"),
            windowEnd=request.payload.get("windowEnd"),
            proxyProfile=request.proxyProfile,
            acceptedAt=utc_now(),
        )
        self.jobs[job_id] = job
        self.callback_urls[job_id] = request.callbackUrl
        self.proxies[job_id] = proxy
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
        try:
            async with self.semaphore:
                worker = self.registry.get(job.worker)
                proxy = self.proxies.get(job_id)
                proxy_url = proxy.server if proxy else None
                result = await asyncio.wait_for(worker.run(job.payload, proxy_url), timeout=self.settings.job_timeout_seconds)
            self._apply_result(job, result)
        except asyncio.TimeoutError:
            self._apply_result(job, WorkerResult(status=JobStatus.TIMEOUT, errorCode="JOB_TIMEOUT", errorMessage="worker exceeded timeout"))
        except Exception as exc:  # noqa: BLE001 - Worker 异常必须回调 Java
            self._apply_result(job, WorkerResult(status=JobStatus.FAILED, errorCode="WORKER_EXCEPTION", errorMessage=str(exc)[:500]))
        finally:
            job.finishedAt = utc_now()
            self.store.save_finished(job)
            await self._callback(job)

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
            normalized.setdefault("appID", job.appID)
            normalized.setdefault("productName", job.productName)
            normalized.setdefault("keyword", job.keyword)
            normalized.setdefault("media", job.media)
            # 内容指纹忽略产品和查询上下文，避免同一素材被不同搜索词发现时误判为内容更新。
            normalized.setdefault("contentHash", _content_hash(normalized))
            job.items.append(normalized)
        job.errorCode = result.errorCode
        job.errorMessage = result.errorMessage

    async def _callback(self, job: JobView) -> None:
        """组装回调契约并发送给 Java；回调失败不覆盖原始抓取状态。"""
        payload = CallbackPayload(
            runID=job.runID,
            jobID=job.jobID,
            worker=job.worker,
            status=job.status,
            queryID=job.queryID,
            appID=job.appID,
            productName=job.productName,
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
        )
        try:
            await self.callback.send(payload, self.callback_urls.get(job.jobID))
        except Exception as exc:  # 回调失败不覆盖原始 Worker 结果
            job.errorCode = job.errorCode or "CALLBACK_FAILED"
            job.errorMessage = job.errorMessage or str(exc)[:500]


def _content_hash(item: dict) -> str:
    """根据平台素材字段生成稳定指纹，排除产品、查询和抓取时间等上下文。"""
    ignored = {"queryID", "appID", "productName", "keyword", "media", "collectionDate", "collectedAt"}
    content = {key: value for key, value in item.items() if key not in ignored}
    encoded = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
