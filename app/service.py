"""任务提交、异步执行、超时和回调编排。"""

import asyncio
from datetime import datetime
from uuid import uuid4

from .callback import CallbackSender
from .config import Settings
from .models import CallbackPayload, JobStatus, JobSubmitRequest, JobView, WorkerResult, utc_now
from .proxy import ProxyLease, ProxyResolver
from .storage import JobStore
from .registry import WorkerRegistry


class JobService:
    def __init__(self, registry: WorkerRegistry, callback: CallbackSender, settings: Settings, proxy_resolver: ProxyResolver | None = None, store: JobStore | None = None) -> None:
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
        self.registry.get(request.worker)
        proxy = self.proxy_resolver.resolve(request.proxyProfile, request.proxyRequired)
        job_id = uuid4().hex
        job = JobView(
            jobId=job_id,
            runId=request.runId,
            worker=request.worker,
            status=JobStatus.DISPATCHED,
            payload=request.payload,
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
        return self.jobs.get(job_id)

    async def _run(self, job_id: str) -> None:
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
        job.status = result.status
        job.fetchedCount = result.fetchedCount
        job.newCount = result.newCount
        job.updatedCount = result.updatedCount
        job.items = result.items
        job.errorCode = result.errorCode
        job.errorMessage = result.errorMessage

    async def _callback(self, job: JobView) -> None:
        payload = CallbackPayload(
            runId=job.runId,
            jobId=job.jobId,
            worker=job.worker,
            status=job.status,
            fetchedCount=job.fetchedCount,
            newCount=job.newCount,
            updatedCount=job.updatedCount,
            items=job.items,
            errorCode=job.errorCode,
            errorMessage=job.errorMessage,
        )
        try:
            await self.callback.send(payload, self.callback_urls.get(job.jobId))
        except Exception as exc:  # 回调失败不覆盖原始 Worker 结果
            job.errorCode = job.errorCode or "CALLBACK_FAILED"
            job.errorMessage = job.errorMessage or str(exc)[:500]
