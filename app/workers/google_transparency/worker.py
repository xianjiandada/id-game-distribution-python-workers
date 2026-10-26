import re
from urllib.parse import urlencode, urljoin

from playwright.async_api import async_playwright

from ...models import JobStatus, WorkerResult
from ..base import BaseWorker


class GoogleTransparencyWorker(BaseWorker):
    """通过 Google Ads Transparency 公开页面搜索广告主并提取素材卡片。"""

    name = "google_transparency"

    async def run(self, payload: dict, proxy_url: str | None = None) -> WorkerResult:
        """通过 Google Ads Transparency 公开网页搜索广告主，不调用 Google Ads API。"""
        keyword = str(payload.get("advertiserName") or payload.get("keyword") or "").strip()
        if not keyword:
            return WorkerResult(
                status=JobStatus.FAILED,
                errorCode="GOOGLE_ADVERTISER_REQUIRED",
                errorMessage="必须提供 advertiserName 或 keyword",
            )

        region = str(payload.get("region") or "HK").upper()
        proxy = {"server": proxy_url} if proxy_url else None
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True, proxy=proxy)
            page = await browser.new_page(locale="zh-CN")
            try:
                await page.goto(
                    "https://adstransparency.google.com/?" + urlencode({"region": region}),
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
                await page.wait_for_timeout(int(payload.get("waitMs", 3000)))
                inputs = page.locator("input[type='text']")
                if not await inputs.count():
                    return WorkerResult(
                        status=JobStatus.WAITING_MANUAL,
                        errorCode="GOOGLE_SEARCH_INPUT_UNAVAILABLE",
                        errorMessage="Google Ads Transparency 搜索框不可用，可能需要人工检查页面或登录状态",
                    )
                await inputs.first.fill(keyword)
                await inputs.first.press("Enter")
                await page.wait_for_timeout(int(payload.get("suggestionWaitMs", 2500)))

                # 搜索联想结果是公开页面的稳定语义区域，优先点击广告主结果。
                result = page.locator("suggestion-renderer .result-container").first
                if not await result.count():
                    # 当前页面有时先显示“查看更多结果”，点击后才创建结果组件。
                    more = page.get_by_role("button", name=re.compile("查看更多结果|See more results", re.I))
                    if await more.count():
                        await more.first.click()
                        await page.wait_for_timeout(int(payload.get("resultSearchWaitMs", 3000)))
                        result = page.locator("suggestion-renderer .result-container").first
                if not await result.count():
                    body = (await page.locator("body").inner_text()).lower()
                    if "captcha" in body or "访问验证" in body or "verify you are human" in body:
                        return WorkerResult(
                            status=JobStatus.WAITING_MANUAL,
                            errorCode="GOOGLE_PAGE_CHALLENGE",
                            errorMessage="Google Ads Transparency 页面需要人工登录或处理访问验证",
                        )
                    return WorkerResult(status=JobStatus.FAILED, errorCode="GOOGLE_ADVERTISER_NOT_FOUND", errorMessage=f"未找到广告主: {keyword}")

                await result.click()
                await page.wait_for_timeout(int(payload.get("resultWaitMs", 6000)))
                creatives = page.locator("creative")
                if not await creatives.count():
                    return WorkerResult(
                        status=JobStatus.FAILED,
                        errorCode="GOOGLE_NO_CREATIVES",
                        errorMessage=f"广告主已找到，但没有加载素材: {keyword}",
                    )
                items = await self._parse_creatives(page, min(int(payload.get("maxResults", 20)), await creatives.count()), region)
                return WorkerResult(fetchedCount=len(items), newCount=len(items), items=items)
            except Exception as exc:  # noqa: BLE001 - 网页变化要回调明确错误
                return WorkerResult(status=JobStatus.FAILED, errorCode="GOOGLE_BROWSER_EXCEPTION", errorMessage=str(exc)[:500])
            finally:
                await browser.close()

    @staticmethod
    async def _parse_creatives(page, max_results: int, region: str) -> list[dict]:
        """从素材卡片的无障碍标签、预览脚本和嵌套广告 iframe 提取素材。"""
        advertiser = ""
        current_name = page.locator(".advertiser-name.current-scope")
        if await current_name.count():
            advertiser = (await current_name.first.inner_text()).strip()
        advertiser_match = re.search(r"/advertiser/([^/?]+)", page.url)
        advertiser_id = advertiser_match.group(1) if advertiser_match else None
        result: list[dict] = []
        media_items: list[dict] = []
        seen_external_ids: set[str] = set()
        creatives = page.locator("creative")
        creative_links = page.locator("a[href*='/creative/']")
        creative_count = await creatives.count()
        link_count = await creative_links.count()
        # 前几条视频经常只有预览脚本没有可下载地址，向后多扫描一些卡片，避免结果页全部没有素材。
        scan_limit = min(creative_count, max(max_results * 20, max_results))
        for index in range(scan_limit):
            card = creatives.nth(index)
            label = await card.get_attribute("aria-label") or ""
            scripts = card.locator("script[src*='creativeId=']")
            src = await scripts.first.get_attribute("src") if await scripts.count() else None
            creative_match = re.search(r"[?&]creativeId=(\d+)", src or "")
            version_match = re.search(r"[?&]versionId=(\d+)", src or "")
            creative_id = creative_match.group(1) if creative_match else None
            if not creative_id:
                # 预览脚本未加载时保留卡片序号，避免整批结果丢失。
                creative_id = f"position-{index + 1}"
            # Google 结果页使用虚拟列表时可能重复挂载同一张卡片，按平台素材 ID 去重。
            if creative_id in seen_external_ids:
                continue
            seen_external_ids.add(creative_id)
            card_text = (await card.inner_text()).lower()
            # Google 的实际图片/视频不在 creative 自身，而在跨域 SafeFrame 的下一层 iframe。
            preview = await GoogleTransparencyWorker._extract_preview(card)
            is_video = preview["mediaType"] == "VIDEO" or "videocam" in card_text or await card.locator("[icon='videocam']").count() > 0
            media_type = "VIDEO" if is_video else preview["mediaType"]
            detail_href = await creative_links.nth(index).get_attribute("href") if index < link_count else None
            detail_url = urljoin(page.url, detail_href) if detail_href else None
            media_url = preview["mediaUrl"] if GoogleTransparencyWorker._is_http_url(preview["mediaUrl"]) else None
            preview_image_url = preview["previewImageUrl"] if GoogleTransparencyWorker._is_http_url(preview["previewImageUrl"]) else None
            platform_data = {
                "versionId": version_match.group(1) if version_match else None,
                "cardLabel": label,
                "previewScriptUrl": src,
                "previewText": preview["previewText"],
                "previewImageUrl": preview_image_url,
            }
            item = {
                "source": "GOOGLE_TRANSPARENCY_WEB",
                "externalAdId": creative_id,
                "identityConfidence": "STABLE" if not creative_id.startswith("position-") else "DERIVED",
                "advertiserId": advertiser_id,
                "advertiserName": advertiser,
                "region": region,
                "regions": [region],
                "format": media_type or "UNKNOWN",
                "versionId": version_match.group(1) if version_match else None,
                "cardLabel": label,
                "creativeUrl": detail_url or (
                    f"https://adstransparency.google.com/advertiser/{advertiser_id}/creative/{creative_id}?region={region}"
                    if advertiser_id and not creative_id.startswith("position-") else None
                ),
                "sourceUrl": page.url,
                "previewScriptUrl": src,
                "mediaUrl": media_url,
                "platformData": platform_data,
            }
            result.append(item)
            if media_url:
                media_items.append(item)
            # 已经凑够带可访问媒体地址的结果后停止继续扫描，避免无意义地打开全部预览 iframe。
            if len(media_items) >= max_results:
                break
        if media_items:
            return media_items[:max_results]
        return result[:max_results]

    @staticmethod
    def _is_http_url(value: str | None) -> bool:
        """只把 Java 媒体代理可以直接请求的 HTTP(S) 地址写入 media_url。"""
        return bool(value and re.match(r"^https?://", value, re.I))

    @staticmethod
    async def _extract_preview(card) -> dict[str, str | None]:
        """读取 Google 预览 iframe 内的图片、视频地址和可见文案。"""
        result: dict[str, str | None] = {
            "mediaUrl": None,
            "previewImageUrl": None,
            "previewText": None,
            "mediaType": None,
        }
        outer_iframes = card.locator("iframe")
        if not await outer_iframes.count():
            return result
        try:
            # 第一层是 Google SafeFrame，第二层通常才是实际的 html5 广告预览。
            outer_frame = outer_iframes.first.content_frame
            inner_iframes = outer_frame.locator("iframe")
            # 有些广告把媒体直接放在 SafeFrame，有些再套一层 iframe；两层都扫描，不能只取其中一层。
            frames = [outer_frame]
            for frame_index in range(await inner_iframes.count()):
                frames.append(inner_iframes.nth(frame_index).content_frame)
            for content_frame in frames:
                body = content_frame.locator("body")
                body_text = (await body.inner_text()).strip()
                if body_text and not result["previewText"]:
                    result["previewText"] = body_text[:2000]

                videos = content_frame.locator("video")
                sources = content_frame.locator("video source, source")
                images = content_frame.locator("img")
                video_url = None
                if await videos.count():
                    video_url = await videos.first.get_attribute("src")
                    result["mediaType"] = "VIDEO"
                    if not video_url:
                        video_url = await videos.first.get_attribute("poster")
                if not video_url and await sources.count():
                    video_url = await sources.first.get_attribute("src")
                    if video_url:
                        result["mediaType"] = "VIDEO"
                if await images.count():
                    image_url = await images.first.get_attribute("src")
                    if image_url:
                        result["previewImageUrl"] = image_url
                        if not video_url and result["mediaType"] is None:
                            result["mediaType"] = "IMAGE"
                if video_url:
                    result["mediaUrl"] = video_url
                elif result["previewImageUrl"] and not result["mediaUrl"]:
                    result["mediaUrl"] = result["previewImageUrl"]
        except Exception:
            # 单个广告的预览 iframe 可能被浏览器阻断，不能因此丢弃整页其他素材。
            return result
        return result
