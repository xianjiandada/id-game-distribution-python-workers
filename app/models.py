"""任务、状态和回调使用的统一数据模型。"""

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class JobStatus(str, Enum):
    CREATED = "CREATED"
    DISPATCHED = "DISPATCHED"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    FAILED = "FAILED"
    WAITING_MANUAL = "WAITING_MANUAL"
    TIMEOUT = "TIMEOUT"


class JobSubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9_\-]+$")
    runId: int | str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    callbackUrl: str | None = Field(default=None, max_length=500)
    proxyProfile: str | None = Field(default=None, max_length=80, pattern=r"^[a-zA-Z0-9_\-]+$")
    proxyRequired: bool = False


class JobSubmitResponse(BaseModel):
    jobId: str
    runId: int | str | None
    worker: str
    status: JobStatus
    acceptedAt: datetime


class JobView(BaseModel):
    jobId: str
    runId: int | str | None
    worker: str
    status: JobStatus
    payload: dict[str, Any]
    proxyProfile: str | None = None
    fetchedCount: int = 0
    newCount: int = 0
    updatedCount: int = 0
    items: list[dict[str, Any]] = Field(default_factory=list)
    errorCode: str | None = None
    errorMessage: str | None = None
    acceptedAt: datetime
    startedAt: datetime | None = None
    finishedAt: datetime | None = None


class WorkerResult(BaseModel):
    status: JobStatus = JobStatus.SUCCESS
    fetchedCount: int = 0
    newCount: int = 0
    updatedCount: int = 0
    items: list[dict[str, Any]] = Field(default_factory=list)
    errorCode: str | None = None
    errorMessage: str | None = None


class CallbackPayload(BaseModel):
    runId: int | str | None
    jobId: str
    worker: str
    status: JobStatus
    fetchedCount: int = 0
    newCount: int = 0
    updatedCount: int = 0
    items: list[dict[str, Any]] = Field(default_factory=list)
    errorCode: str | None = None
    errorMessage: str | None = None
