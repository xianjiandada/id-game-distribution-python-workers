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
                max_results = MetaLibraryWorker._parse_max_results(payload.get("maxResults"))
                await MetaLibraryWorker._load_more_cards(page, max_results, int(payload.get("waitMs", 5000)))
                items = await self._parse_cards(page, max_results, payload)
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
        # 未传国家或地区时按全部地区查询，避免误回退到某个默认国家。
        country_codes = payload.get("countryCodes") or [payload.get("country") or "ALL"]
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
    async def _parse_cards(page, max_results: int | None, payload: dict) -> list[dict]:
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
                    # Ads Library 当前公开卡片主要返回图片缩略图；明确写入类型，避免 Java/CMS 把图片误判为未知。
                    "mediaType": "IMAGE" if media_urls else "UNKNOWN",
                    "startedText": started,
                    "regions": payload.get("countryCodes") or [payload.get("country") or "ALL"],
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
            if max_results is not None and len(items) >= max_results:
                break
        return items

    @staticmethod
    def _parse_max_results(value: object) -> int | None:
        """解析拉取条数；缺省、空值和 0 表示不限制。"""
        if value is None or str(value).strip() == "":
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    @staticmethod
    async def _load_more_cards(page, max_results: int | None, wait_ms: int) -> None:
        """滚动 Meta 结果页加载更多卡片，直到达到上限或页面不再增长。"""
        stable_rounds = 0
        # 有上限时给每条结果最多约 20 倍的页面加载机会；无上限时继续到页面稳定。
        max_rounds = min(max_results * 20 if max_results else 100, 100)
        cards = page.get_by_text("Library ID:", exact=False)
        for _ in range(max_rounds):
            count_before = await cards.count()
            if max_results is not None and count_before >= max_results:
                return
            await page.mouse.wheel(0, 1600)
            await page.wait_for_timeout(min(max(wait_ms // 4, 300), 1500))
            count_after = await cards.count()
            if count_after <= count_before:
                stable_rounds += 1
            else:
                stable_rounds = 0
            if stable_rounds >= 3:
                return
