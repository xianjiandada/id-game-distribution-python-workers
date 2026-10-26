import re
from urllib.parse import urlencode

from playwright.async_api import async_playwright

from ...models import JobStatus, WorkerResult
from ..base import BaseWorker


class MetaLibraryWorker(BaseWorker):
    """通过 Meta Ads Library 公开页面按关键词提取广告卡片。"""

    name = "meta_library"

    async def run(self, payload: dict, proxy_url: str | None = None) -> WorkerResult:
        """通过 Meta Ads Library 公开网页抓取结果，不调用 Graph API。"""
        url = self._build_url(payload)
        proxy = {"server": proxy_url} if proxy_url else None
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True, proxy=proxy)
            page = await browser.new_page(locale="en-US")
            try:
                # Meta 可能返回 403，但仍然把公开结果渲染在页面中，不能只看 HTTP 状态。
                response = await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(int(payload.get("waitMs", 5000)))
                items = await self._parse_cards(page, int(payload.get("maxResults", 20)), payload)
                if items:
                    return WorkerResult(fetchedCount=len(items), newCount=len(items), items=items)

                body = (await page.locator("body").inner_text()).lower()
                if any(marker in body for marker in ("captcha", "checkpoint", "log in", "暂时无法")):
                    return WorkerResult(
                        status=JobStatus.WAITING_MANUAL,
                        errorCode="META_PAGE_CHALLENGE",
                        errorMessage="Meta Ads Library 页面需要人工处理登录、验证码或访问挑战",
                    )
                http_status = response.status if response else "unknown"
                return WorkerResult(
                    status=JobStatus.FAILED,
                    errorCode="META_NO_RESULTS",
                    errorMessage=f"Meta Ads Library 页面未找到广告卡片，HTTP 状态: {http_status}",
                )
            finally:
                await browser.close()

    @staticmethod
    def _build_url(payload: dict) -> str:
        """把后台查询条件转换为 Ads Library 网页查询参数。"""
        country_codes = payload.get("countryCodes") or [payload.get("country", "HK")]
        params = {
            "active_status": payload.get("activeStatus", "active"),
            "ad_type": payload.get("adType", "all"),
            "country": country_codes[0],
            "is_targeted_country": "false",
            "media_type": payload.get("mediaType", "all"),
            "search_type": "keyword_unordered",
            "sort_data[mode]": "total_impressions",
            "sort_data[direction]": "desc",
        }
        keyword = payload.get("keyword") or payload.get("q")
        if keyword:
            params["q"] = keyword
        return "https://www.facebook.com/ads/library/?" + urlencode(params)

    @staticmethod
    async def _parse_cards(page, max_results: int, payload: dict) -> list[dict]:
        """读取语义文本和链接，避免依赖 Meta 会变化的随机 CSS class。"""
        cards = page.get_by_text("Library ID:", exact=False)
        items: list[dict] = []
        seen: set[str] = set()
        for index in range(await cards.count()):
            library_text = cards.nth(index)
            card = library_text.locator("xpath=ancestor::div[.//div[normalize-space()='See ad details']][1]")
            if not await card.count():
                continue
            text = await card.inner_text()
            match = re.search(r"Library ID:\s*(\d+)", text)
            if not match or match.group(1) in seen:
                continue
            external_id = match.group(1)
            seen.add(external_id)
            lines = [line.strip() for line in text.splitlines() if line.strip() and line.strip() != "\u200b"]
            advertiser = ""
            copy_text = ""
            if "See ad details" in lines:
                start = lines.index("See ad details") + 1
                if start < len(lines):
                    advertiser = lines[start]
                if start + 2 < len(lines) and lines[start + 1] == "Sponsored":
                    copy_text = lines[start + 2]
            started = next((line for line in lines if line.startswith("Started running on ")), "")
            anchors = card.locator("a[target='_blank']")
            hrefs = [await anchors.nth(i).get_attribute("href") for i in range(await anchors.count())]
            hrefs = [href for href in hrefs if href]
            images = card.locator("img")
            media_urls = [await images.nth(i).get_attribute("src") for i in range(await images.count())]
            media_urls = [url for url in media_urls if url]
            items.append(
                {
                    "source": "META_LIBRARY_WEB",
                    "externalAdId": external_id,
                    "identityConfidence": "STABLE",
                    "advertiserName": advertiser,
                    "copyText": copy_text,
                    "startedText": started,
                    "regions": payload.get("countryCodes") or [payload.get("country", "HK")],
                    # Meta 页面字段可能随版本变化，保留有用的原始筛选和投放信息。
                    "platformData": {
                        "startedText": started,
                        "activeStatus": payload.get("activeStatus", "active"),
                        "adType": payload.get("adType", "all"),
                        "mediaType": payload.get("mediaType", "all"),
                    },
                    "advertiserUrl": hrefs[0] if hrefs else None,
                    "landingUrl": hrefs[1] if len(hrefs) > 1 else None,
                    "mediaUrl": media_urls[-1] if media_urls else None,
                    "snapshotUrl": f"https://www.facebook.com/ads/library/?id={external_id}",
                    "sourceUrl": page.url,
                }
            )
            if len(items) >= max_results:
                break
        return items
