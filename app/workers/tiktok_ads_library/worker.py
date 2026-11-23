"""通过 TikTok EU Ads Library 公开页面抓取广告素材。"""

import asyncio
import os
from calendar import monthrange
from datetime import datetime, timezone
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

import httpx
from playwright.async_api import async_playwright

from ...models import JobStatus, WorkerResult
from ..base import BaseWorker
from .official import query_ads
from .search_response import SearchResults


class TikTokAdsLibraryWorker(BaseWorker):
    """按关键词、广告主和地区读取 TikTok 公开广告资料库。"""

    name = "tiktok_ads_library"
    BASE_URL = "https://library.tiktok.com/ads"

    async def run(self, payload: dict, proxy_url: str | None = None) -> WorkerResult:
        """抓取公开广告卡片并保留图片、视频和详情链接。"""
        url = self._build_url(payload)
        limit = self._parse_max_results(payload.get("maxResults"))
        token = os.getenv("TIKTOK_ADLIB_ACCESS_TOKEN")
        if token and not payload.get("url"):
            async with httpx.AsyncClient(proxy=proxy_url, timeout=30) as client:
                return await query_ads(client, token, payload, SearchResults(url, limit, "official_api"))
        proxy = {"server": proxy_url} if proxy_url else None
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True, proxy=proxy)
            page = await browser.new_page(locale="en-US")
            responses = asyncio.Queue()
            page.on(
                "response",
                lambda response: responses.put_nowait(response)
                if response.request.method == "POST"
                and urlsplit(response.url).hostname == "library.tiktok.com"
                and urlsplit(response.url).path.rstrip("/") == "/api/v1/search"
                else None,
            )
            try:
                response = await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                wait_ms = max(1000, int(payload.get("resultWaitMs", payload.get("waitMs", 15000))))
                try:
                    first_response = await asyncio.wait_for(responses.get(), wait_ms / 1000)
                except asyncio.TimeoutError:
                    first_response = None
                if first_response is not None:
                    return await self._read_pages(page, first_response, responses, SearchResults(url, limit, "web"), wait_ms)
                body = (await page.locator("body").inner_text()).lower()
                if any(marker in body for marker in (
                    "captcha",
                    "verify",
                    "robot",
                    "访问验证",
                    "access blocked",
                    "temporarily restricted",
                    "48 hours",
                )):
                    return WorkerResult(
                        status=JobStatus.WAITING_MANUAL,
                        errorCode="TIKTOK_PAGE_CHALLENGE",
                        errorMessage="TikTok Ads Library 页面需要人工处理访问验证，当前出口可能被临时限制",
                    )
                http_status = response.status if response else "unknown"
                return WorkerResult(status=JobStatus.FAILED, errorCode="TIKTOK_RESULTS_UNREADABLE",
                                    errorMessage=f"TikTok 页面 HTTP {http_status}，未观察到广告搜索响应")
            except Exception as exc:  # noqa: BLE001 - 页面结构变化需要回调明确错误
                return WorkerResult(
                    status=JobStatus.FAILED,
                    errorCode="TIKTOK_BROWSER_EXCEPTION",
                    errorMessage=str(exc)[:500],
                )
            finally:
                await browser.close()

    @staticmethod
    async def _read_pages(page, response, responses, results: SearchResults, wait_ms: int) -> WorkerResult:
        """只读取页面自身发起的搜索，按平台末页标记结束。"""
        for _ in range(80):
            if not response.ok:
                busy = response.status in {403, 421, 429, 503}
                result = results.result("TIKTOK_API_BUSY" if busy else "TIKTOK_HTTP_ERROR",
                                        f"POST /api/v1/search 返回 HTTP {response.status}",
                                        JobStatus.WAITING_MANUAL if busy else JobStatus.FAILED)
                if response.status == 421:
                    result.restrictionType = "HTTP_421"
                return result
            try:
                results.add_page(await response.json())
            except Exception as exc:
                return results.result("TIKTOK_INVALID_RESPONSE", str(exc)[:500])
            if results.full or not results.has_more:
                break
            if responses.empty():
                await page.mouse.wheel(0, 1800)
            try:
                response = await asyncio.wait_for(responses.get(), wait_ms / 1000)
            except asyncio.TimeoutError:
                break
        return results.result()

    @classmethod
    def _build_url(cls, payload: dict) -> str:
        """使用公开结果页的筛选参数。任务给出的完整搜索链接优先。"""
        supplied_url = str(payload.get("url") or "").strip()
        overrides = {}
        if supplied_url.startswith(cls.BASE_URL) and urlsplit(supplied_url).path.rstrip("/") == "/ads":
            overrides = dict(parse_qsl(urlsplit(supplied_url).query, keep_blank_values=True))
            if overrides.get("adv_name") and "start_time" in overrides and "end_time" in overrides:
                return supplied_url
        regions = payload.get("countryCodes") or [payload.get("country") or "ALL"]
        region = str(regions[0]).lower() if regions else "all"
        end = datetime.now(timezone.utc)
        previous_month = end.month - 1 or 12
        year = end.year - int(end.month == 1)
        start = end.replace(year=year, month=previous_month, day=min(end.day, monthrange(year, previous_month)[1]))
        if payload.get("windowStart") and payload.get("windowEnd"):
            start = datetime.fromisoformat(payload["windowStart"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(payload["windowEnd"].replace("Z", "+00:00"))
        keyword = str(payload.get("keyword") or payload.get("advertiserName") or "").strip()
        params = {
            "region": region, "start_time": int(start.timestamp() * 1000), "end_time": int(end.timestamp() * 1000),
            "adv_name": keyword, "adv_biz_ids": "", "query_type": "1", "sort_type": "last_shown_date,desc",
            "ad_type": "0", "ad_status": "1", "ages": "all", "ad_reach": "all", "gender": "ALL",
        }
        if payload.get("mediaType") and str(payload["mediaType"]).upper() != "ALL":
            params["creative_type"] = str(payload["mediaType"]).lower()
        params.update(overrides)
        return f"{cls.BASE_URL}?{urlencode(params, quote_via=quote, safe=',')}"

    @staticmethod
    def _parse_max_results(value: object) -> int | None:
        if value is None or str(value).strip() == "":
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None
