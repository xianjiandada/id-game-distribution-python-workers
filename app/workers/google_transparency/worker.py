import re
from urllib.parse import urlencode

from playwright.async_api import async_playwright

from ...models import JobStatus, WorkerResult
from ..base import BaseWorker


class GoogleTransparencyWorker(BaseWorker):
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
        """从素材卡片的无障碍标签和预览脚本提取稳定 ID。"""
        advertiser = ""
        current_name = page.locator(".advertiser-name.current-scope")
        if await current_name.count():
            advertiser = (await current_name.first.inner_text()).strip()
        advertiser_match = re.search(r"/advertiser/([^/?]+)", page.url)
        advertiser_id = advertiser_match.group(1) if advertiser_match else None
        result: list[dict] = []
        creatives = page.locator("creative")
        for index in range(min(max_results, await creatives.count())):
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
            card_text = (await card.inner_text()).lower()
            is_video = "videocam" in card_text or await card.locator("[icon='videocam']").count() > 0
            item = {
                "source": "GOOGLE_TRANSPARENCY_WEB",
                "externalAdId": creative_id,
                "advertiserId": advertiser_id,
                "advertiserName": advertiser,
                "region": region,
                "format": "VIDEO" if is_video else "UNKNOWN",
                "versionId": version_match.group(1) if version_match else None,
                "cardLabel": label,
                "creativeUrl": (
                    f"https://adstransparency.google.com/advertiser/{advertiser_id}/creative/{creative_id}?region={region}"
                    if advertiser_id and not creative_id.startswith("position-") else None
                ),
                "sourceUrl": page.url,
                "previewScriptUrl": src,
            }
            result.append(item)
        return result
