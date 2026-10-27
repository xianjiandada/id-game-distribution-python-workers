"""任务、状态和回调使用的统一数据模型。"""

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    """生成带 UTC 时区的当前时间，统一任务和回调时间格式。"""
    return datetime.now(timezone.utc)


class JobStatus(str, Enum):
    """任务从接收到完成、失败或人工介入所经历的状态。"""
    CREATED = "CREATED"
    DISPATCHED = "DISPATCHED"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    FAILED = "FAILED"
    WAITING_MANUAL = "WAITING_MANUAL"
    TIMEOUT = "TIMEOUT"


class JobSubmitRequest(BaseModel):
    """提交任务的外部请求模型，拒绝未声明字段避免拼写错误。"""
    model_config = ConfigDict(extra="forbid")

    # Worker 注册名，例如 meta_library 或 google_transparency。
    worker: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9_\-]+$")
    # Java 侧业务运行 ID，用于回调地址和最终入库关联。
    runID: int | str | None = None
    # 平台专属参数，例如关键词、国家和最大抓取数量。
    payload: dict[str, Any] = Field(default_factory=dict)
    # 可选的任务级回调地址；生产建议使用服务端默认地址。
    callbackUrl: str | None = Field(default=None, max_length=500)
    # 逻辑代理 Profile 名称，不直接接收带密码的代理地址。
    proxyProfile: str | None = Field(default=None, max_length=80, pattern=r"^[a-zA-Z0-9_\-]+$")
    # 为 true 时没有可用代理就拒绝任务，不允许降级直连。
    proxyRequired: bool = False


class JobSubmitResponse(BaseModel):
    """任务接收接口的快速响应模型。"""
    jobID: str
    runID: int | str | None
    worker: str
    status: JobStatus
    acceptedAt: datetime


class JobView(BaseModel):
    """任务实时状态和抓取结果模型。"""
    jobID: str
    runID: int | str | None
    worker: str
    status: JobStatus
    payload: dict[str, Any]
    # 查询配置 ID，用于把一次抓取归属到具体的产品查询条件。
    queryID: int | str | None = None
    # 产品在 Java Manager 中的 appID；同一个 Worker 可以服务多个产品。
    appID: int | str | None = None
    # 产品名称快照，避免产品改名后历史运行记录无法识别。
    productName: str | None = None
    # 本次运行使用的搜索词或广告主名称快照。
    keyword: str | None = None
    # 统一平台来源快照，避免 Java 只能根据 Worker 名称猜来源。
    media: str | None = None
    # 固定的查询窗口，用于回调 Java 后按同一窗口重试和核对。
    windowStart: datetime | None = None
    windowEnd: datetime | None = None
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
    """Worker 实现返回给宿主编排层的统一结果模型。"""
    status: JobStatus = JobStatus.SUCCESS
    fetchedCount: int = 0
    newCount: int = 0
    updatedCount: int = 0
    items: list[dict[str, Any]] = Field(default_factory=list)
    errorCode: str | None = None
    errorMessage: str | None = None


class CallbackPayload(BaseModel):
    """Python 回调 Java Manager 时使用的统一 JSON 契约。"""
    runID: int | str | None
    jobID: str
    worker: str
    status: JobStatus
    # 查询配置和产品维度，Java 入库后可以按产品、搜索词和日期统计。
    queryID: int | str | None = None
    appID: int | str | None = None
    productName: str | None = None
    keyword: str | None = None
    media: str | None = None
    # 采集日期和采集时间，分别用于按天汇总和精确审计。
    collectionDate: str | None = None
    collectedAt: datetime | None = None
    # 查询窗口快照，重试时保持原窗口，避免日报边界漂移。
    windowStart: datetime | None = None
    windowEnd: datetime | None = None
    fetchedCount: int = 0
    newCount: int = 0
    updatedCount: int = 0
    items: list[dict[str, Any]] = Field(default_factory=list)
    errorCode: str | None = None
    errorMessage: str | None = None
