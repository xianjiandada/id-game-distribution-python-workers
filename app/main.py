"""FastAPI 应用入口和内部任务接口。"""

from fastapi import FastAPI, HTTPException, status

from .callback import JavaCallbackClient
from .config import settings
from .models import JobSubmitRequest, JobSubmitResponse
from .registry import WorkerRegistry
from .proxy import ProxyUnavailable
from .service import JobService
from .workers.google_transparency.worker import GoogleTransparencyWorker
from .workers.meta_library.worker import MetaLibraryWorker
from .workers.mock_worker import MockWorker


def create_app() -> FastAPI:
    """创建 FastAPI 应用并注册内置 Worker。"""
    app = FastAPI(title=settings.app_name, version="0.1.0")
    registry = WorkerRegistry()
    registry.register(MockWorker())
    registry.register(MetaLibraryWorker())
    registry.register(GoogleTransparencyWorker())
    app.state.registry = registry
    app.state.jobs = JobService(registry, JavaCallbackClient(settings), settings)

    @app.get("/health")
    @app.post("/health")
    async def health() -> dict:
        """返回进程存活状态，供 Supervisor、宝塔或负载均衡检查。"""
        return {"status": "UP", "service": settings.app_name, "environment": settings.app_env}

    @app.get("/workers")
    async def workers() -> dict:
        """列出当前进程已经注册的 Worker 名称。"""
        return {"workers": registry.names()}

    @app.post("/jobs", response_model=JobSubmitResponse, status_code=status.HTTP_202_ACCEPTED)
    async def submit_job(request: JobSubmitRequest) -> JobSubmitResponse:
        """登记并异步派发任务，接口不会等待网页抓取完成。"""
        try:
            job = app.state.jobs.submit(request)
        except KeyError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ProxyUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return JobSubmitResponse(jobID=job.jobID, runID=job.runID, worker=job.worker, status=job.status, acceptedAt=job.acceptedAt)

    @app.get("/jobs/{jobID}")
    async def get_job(jobID: str):
        """按 Python jobID 查询内存中的实时任务状态。"""
        job = app.state.jobs.get(jobID)
        if not job:
            raise HTTPException(status_code=404, detail="job not found")
        return job

    return app


app = create_app()
