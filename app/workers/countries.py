"""按平台支持的单地区查询执行配置，按广告 ID 合并实际命中的国家。"""

import asyncio
import re
from itertools import zip_longest

from ..models import JobStatus, WorkerResult


async def collect_countries(worker, payload, timeout):
    codes = list(dict.fromkeys(str(v).strip().upper() for v in payload.get("countryCodes") or []))
    if any(v in ("ALL", "ANYWHERE") for v in codes):
        codes = []
    if any(not re.fullmatch("[A-Z]{2}", code) for code in codes):
        raise ValueError("国家代码格式错误")
    if payload.get("url") and codes:
        raise ValueError("指定搜索链接时请使用链接中的地区")
    scopes = codes or [None]
    pages = []
    failures = []
    last_status = JobStatus.FAILED
    deadline = asyncio.get_running_loop().time() + timeout
    for index, country in enumerate(scopes):
        request = dict(payload, countryCodes=[country] if country else [])
        if country:
            request.update(country=country, region=country)
        budget = (deadline - asyncio.get_running_loop().time()) / (len(scopes) - index)
        request["_countryDeadline"] = asyncio.get_running_loop().time() + budget
        try:
            result = await asyncio.wait_for(worker.run(request), budget)
        except asyncio.TimeoutError:
            result = WorkerResult(status=JobStatus.TIMEOUT, errorCode="JOB_TIMEOUT", errorMessage="地区查询超时")
        except Exception as exc:
            result = WorkerResult(status=JobStatus.FAILED, errorCode="WORKER_EXCEPTION", errorMessage=str(exc)[:300])
        for item in result.items:
            observed = [country] if country else []
            # 与平台主动披露的覆盖地区分别保留，筛选字段收录两类有依据的地区。
            item["requestRegions"] = observed
            item["regions"] = sorted(set(observed + item.get("regions", [])))
        if len(scopes) == 1:
            return result
        pages.append(result.items)
        if result.status != JobStatus.SUCCESS:
            failures.append(f"{country}: {result.errorMessage or result.status.value}")
            last_status = result.status

    # 轮流取各国结果，拉取条数对整次运行生效。重复素材合并所有命中国家。
    limit = int(payload.get("maxResults") or 0)
    items = {}
    for group in zip_longest(*pages):
        for item in group:
            if not item:
                continue
            key = item["externalAdID"]
            if key in items:
                for field in ("regions", "requestRegions"):
                    items[key][field] = sorted(set(items[key][field] + item[field]))
            elif limit <= 0 or len(items) < limit:
                items[key] = item
    status = JobStatus.SUCCESS
    if failures:
        status = JobStatus.PARTIAL_SUCCESS if items or len(failures) < len(scopes) else last_status
    return WorkerResult(status=status, items=list(items.values()), fetchedCount=len(items), newCount=len(items),
                        errorCode="COUNTRY_QUERY_FAILED" if failures else None,
                        errorMessage=" | ".join(failures)[:500] or None)
