"""用默认或自定义查询词执行两个网页 Worker 的小规模真实烟测。"""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

# 允许用户直接执行 `python scripts/live_smoke_test.py`，不需要额外设置 PYTHONPATH。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import settings  # noqa: E402
from app.models import JobStatus, WorkerResult  # noqa: E402
from app.proxy import ProxyResolver  # noqa: E402
from app.workers.google_transparency.worker import GoogleTransparencyWorker  # noqa: E402
from app.workers.meta_library.worker import MetaLibraryWorker  # noqa: E402


def _preview(item: dict[str, Any]) -> dict[str, Any]:
    """只输出验收需要的字段，避免把完整页面原始内容打印到终端。"""
    return {
        key: item.get(key)
        for key in (
            "media",
            "externalAdID",
            "advertiserID",
            "advertiserName",
            "format",
            "mediaType",
            "mediaUrl",
            "landingUrl",
            "creativeUrl",
            "snapshotUrl",
            "previewScriptUrl",
        )
        if item.get(key) is not None
    }


async def _run_worker(
    name: str,
    worker: Any,
    payload: dict[str, Any],
    query: dict[str, str],
    proxy_profile: str | None,
) -> dict[str, Any]:
    """执行单个平台并转换成适合终端查看的结果。"""
    proxy = ProxyResolver(settings).resolve(proxy_profile, required=False)
    result: WorkerResult = await worker.run(payload, proxy.server if proxy else None)
    return {
        "worker": name,
        "query": query,
        "status": result.status.value,
        "fetchedCount": result.fetchedCount,
        "errorCode": result.errorCode,
        "errorMessage": result.errorMessage,
        "items": [_preview(item) for item in result.items],
    }


async def _main(
    max_results: int,
    wait_ms: int,
    proxy_profile: str | None,
    meta_keyword: str,
    google_advertiser: str,
    google_region: str,
) -> int:
    """执行一组 Meta/Google 查询，并用退出码表示是否通过。"""
    tasks = [
        (
            "meta_library",
            MetaLibraryWorker(),
            {
                "keyword": meta_keyword,
                "countryCodes": ["ALL"],
                "activeStatus": "active",
                "adType": "all",
                "mediaType": "all",
                "maxResults": None if max_results == 0 else max_results,
                "waitMs": wait_ms,
            },
            {"keyword": meta_keyword, "country": "ALL"},
        ),
        (
            "google_transparency",
            GoogleTransparencyWorker(),
            {
                "advertiserName": google_advertiser,
                "region": google_region,
                "maxResults": None if max_results == 0 else max_results,
                "suggestionWaitMs": wait_ms,
                "resultWaitMs": wait_ms,
            },
            {"advertiserName": google_advertiser, "region": google_region},
        ),
    ]
    results: list[dict[str, Any]] = []
    for name, worker, payload, query in tasks:
        try:
            results.append(await _run_worker(name, worker, payload, query, proxy_profile))
        except Exception as exc:  # noqa: BLE001 - 烟测要把平台异常显示给操作者
            results.append(
                {
                    "worker": name,
                    "query": query,
                    "status": JobStatus.FAILED.value,
                    "fetchedCount": 0,
                    "errorCode": "SMOKE_TEST_EXCEPTION",
                    "errorMessage": str(exc)[:500],
                    "items": [],
                }
            )
    print(json.dumps({"results": results}, ensure_ascii=False, indent=2))
    return 0 if all(item["status"] == JobStatus.SUCCESS.value and item["fetchedCount"] > 0 for item in results) else 1


def main() -> int:
    """解析烟测参数并把异步执行结果转换为进程退出码。"""
    parser = argparse.ArgumentParser(description="执行 Meta 和 Google 公开网页抓取小规模烟测")
    parser.add_argument("--max-results", type=int, default=3, help="每个平台最多抓取多少条，默认 3")
    parser.add_argument("--wait-ms", type=int, default=4000, help="页面等待时间，默认 4000 毫秒")
    parser.add_argument("--proxy-profile", help="可选逻辑代理 Profile，例如 google_hk")
    parser.add_argument("--meta-keyword", default="螺丝", help="Meta 搜索词，默认：螺丝")
    parser.add_argument(
        "--google-advertiser",
        default="SOFISH TECHNOLOGY PTE. LTD.",
        help="Google 广告主名称，默认：SOFISH TECHNOLOGY PTE. LTD.",
    )
    parser.add_argument("--google-region", default="SG", help="Google 搜索地区代码，默认：SG")
    args = parser.parse_args()
    if not args.meta_keyword.strip():
        parser.error("--meta-keyword 不能为空")
    if not args.google_advertiser.strip():
        parser.error("--google-advertiser 不能为空")
    if len(args.google_region.strip()) != 2 or not args.google_region.strip().isalpha():
        parser.error("--google-region 必须是两位地区代码，例如 SG 或 HK")
    if args.max_results < 0 or args.max_results > 1000:
        parser.error("--max-results 必须在 0 到 1000 之间，0 表示不限制")
    if args.wait_ms < 0 or args.wait_ms > 60000:
        parser.error("--wait-ms 必须在 0 到 60000 之间")
    return asyncio.run(
        _main(
            args.max_results,
            args.wait_ms,
            args.proxy_profile,
            args.meta_keyword.strip(),
            args.google_advertiser.strip(),
            args.google_region.strip().upper(),
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
