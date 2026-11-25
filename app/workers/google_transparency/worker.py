import asyncio
import html
import json
import re
from urllib.parse import quote, urlencode, urljoin

import httpx

from playwright.async_api import Error as PlaywrightError, async_playwright

from ...creative_fields import infer_function, infer_language
from ...models import JobStatus, WorkerResult
from ...youtube_meta import enrich as enrich_youtube_metadata
from ..base import BaseWorker


class GoogleTransparencyWorker(BaseWorker):
    """通过 Google Ads Transparency 公开页面搜索广告主并提取素材卡片。"""

    name = "google_transparency"

    _ATC_RPC = "https://adstransparency.google.com/anji/_/rpc/"
    _ATC_GEO = 2344
    _ATC_PAGE_SIZE = 40
    _ATC_MAX_CREATIVES = 500
    _ADVERTISER_ID = re.compile(r"^AR\d{10,}$", re.IGNORECASE)

    async def run(self, payload: dict) -> WorkerResult:
        """通过 Google Ads Transparency 公开网页搜索广告主，不调用 Google Ads API。"""
        keyword = str(payload.get("keyword") or "").strip()
        if not keyword:
            return WorkerResult(
                status=JobStatus.FAILED,
                errorCode="GOOGLE_ADVERTISER_REQUIRED",
                errorMessage="必须提供 keyword",
            )

        if GoogleTransparencyWorker._is_advertiser_id(keyword):
            return await self._run_atc(payload, keyword.upper())

        # Google 未填写国家/地区时使用 ANYWHERE；宿主按地区分别调用。
        region = GoogleTransparencyWorker._resolve_region(payload)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context(locale="zh-CN")
            page = await context.new_page()
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
                max_results = GoogleTransparencyWorker._parse_max_results(payload.get("maxResults"))
                await GoogleTransparencyWorker._load_more_creatives(page, max_results, int(payload.get("resultWaitMs", 6000)))
                script_timeout_ms = GoogleTransparencyWorker._parse_positive_int(
                    payload.get("scriptTimeoutMs"), 8000
                )
                items = await self._parse_creatives(page, max_results, region, script_timeout_ms)
                if not items:
                    return WorkerResult(status=JobStatus.FAILED, errorCode="GOOGLE_CREATIVES_UNREADABLE",
                                        errorMessage="Google 素材卡片未返回可识别的平台素材 ID")
                if region == "ANYWHERE":
                    # 留出关闭浏览器、回传已采集素材的时间。
                    remaining = payload.get("_countryDeadline", asyncio.get_running_loop().time() + 900)
                    remaining -= asyncio.get_running_loop().time() + 5
                    if remaining > 0:
                        try:
                            await asyncio.wait_for(self._load_delivery_regions(page.context, items), remaining)
                        except (asyncio.TimeoutError, PlaywrightError):
                            pass
                return WorkerResult(fetchedCount=len(items), newCount=len(items), items=items)
            except Exception as exc:  # noqa: BLE001 - 网页变化要回调明确错误
                return WorkerResult(status=JobStatus.FAILED, errorCode="GOOGLE_BROWSER_EXCEPTION", errorMessage=str(exc)[:500])
            finally:
                await browser.close()

    @classmethod
    def _is_advertiser_id(cls, value: str | None) -> bool:
        """识别 Google Ads Transparency 的 AR 广告主 ID。"""
        return bool(cls._ADVERTISER_ID.fullmatch(str(value or "").strip()))

    @classmethod
    def _atc_search_payload(cls, advertiser_id: str, page_token: str | None = None) -> dict:
        payload = {
            "2": cls._ATC_PAGE_SIZE,
            "3": {"12": {"1": "", "2": True}, "13": {"1": [advertiser_id]}},
            "7": {"1": 1, "2": 0, "3": cls._ATC_GEO},
        }
        if page_token:
            payload["4"] = page_token
        return payload

    @classmethod
    async def _atc_rpc(cls, client: httpx.AsyncClient, method: str, payload: dict) -> dict:
        """调用 ATC 内部 RPC。该接口按 f.req 接收 JSON，而不是普通 REST JSON。"""
        body = "f.req=" + quote(json.dumps(payload, separators=(",", ":")))
        last_error = "unknown"
        for attempt in range(3):
            try:
                response = await client.post(
                    cls._ATC_RPC + method + "?authuser=",
                    content=body.encode("utf-8"),
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
            except httpx.HTTPError as exc:
                last_error = str(exc)
                if attempt < 2:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                raise RuntimeError(f"ATC 请求失败：{last_error}") from exc
            if response.status_code == 200:
                try:
                    data = response.json()
                except ValueError as exc:
                    raise RuntimeError("ATC 返回不是合法 JSON") from exc
                if isinstance(data, dict):
                    return data
                raise RuntimeError("ATC 返回结构异常")
            last_error = f"HTTP {response.status_code}"
            if response.status_code == 429 and attempt < 2:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            break
        raise RuntimeError(f"ATC {method} 失败：{last_error}")

    @classmethod
    async def _atc_preview_urls(cls, client: httpx.AsyncClient, advertiser_id: str, item: dict) -> list[str]:
        """读取搜索结果中的预览 URL，缺失时按创意 ID 查询详情。"""
        urls: list[str] = []
        creative = item.get("3") or {}
        if isinstance(creative, dict):
            first = creative.get("1") or {}
            if isinstance(first, dict) and first.get("4"):
                urls.append(str(first["4"]))
        if urls:
            return urls
        creative_id = str(item.get("2") or "").strip()
        if not creative_id:
            return urls
        try:
            data = await cls._atc_rpc(client, "LookupService/GetCreativeById", {
                "1": advertiser_id,
                "2": creative_id,
                "5": {"1": 1, "2": 0, "3": cls._ATC_GEO},
            })
        except RuntimeError:
            return urls
        groups = (data.get("1") or {}).get("5") if isinstance(data.get("1"), dict) else []
        for group in groups or []:
            if isinstance(group, dict) and isinstance(group.get("1"), dict) and group["1"].get("4"):
                urls.append(str(group["1"]["4"]))
        return list(dict.fromkeys(urls))

    @classmethod
    def _parse_atc_preview(cls, source: str) -> dict[str, str | None]:
        """解析 ATC 预览脚本中的应用文案、媒体地址和 YouTube ID。"""
        decoded = re.sub(r"\\([/?=&:#])", r"\1", cls._decode_preview_script(source))
        app_name = None
        package_name = None
        for field, target in (("appName", "app_name"), ("appId", "package_name")):
            match = re.search(r"['\"]" + field + r"['\"]\s*:\s*['\"]([^'\"]{1,500})", decoded)
            if match:
                if target == "app_name":
                    app_name = " ".join(match.group(1).split())
                else:
                    package_name = " ".join(match.group(1).split())
        texts: list[str] = []
        for field in ("headline", "description", "description1", "description2", "longHeadline", "title"):
            for match in re.finditer(r"['\"]" + field + r"['\"]\s*:\s*['\"]([^'\"]{1,2000})", decoded, re.I):
                value = " ".join(match.group(1).split())
                if value and value not in texts:
                    texts.append(value)
        preview_text = "\n".join(texts[:20]) or None
        parsed = cls._parse_preview_text(preview_text)
        copy_text = "\n".join(texts[:10])[:4000] or None
        video_id = cls._extract_youtube_video_id(decoded)
        video_match = re.search(r"(?:(?:https?:)?//)[^\"'<>\s]+googlevideo\.com/videoplayback\?[^\"'<>\s]+", decoded, re.I)
        image_match = re.search(r"(?:(?:https?:)?//)(?:i\d+\.ytimg\.com|tpc\.googlesyndication\.com/simgad/|lh\d+\.googleusercontent\.com)/[^\"'<>\s]+", decoded, re.I)
        media_url = cls._normalize_media_url(video_match.group(0).rstrip("])")) if video_match else None
        image_url = cls._normalize_media_url(image_match.group(0).rstrip("])")) if image_match else None
        return {
            "previewText": preview_text,
            "productName": app_name or parsed["productName"],
            "packageName": package_name or parsed["packageName"],
            "copyText": copy_text or parsed["copyText"],
            "mediaUrl": media_url or image_url,
            "previewImageUrl": image_url,
            "mediaType": "VIDEO" if media_url else ("IMAGE" if image_url else None),
            "youtubeVideoId": video_id,
        }

    async def _run_atc(self, payload: dict, advertiser_id: str) -> WorkerResult:
        """按案例项目的 ATC RPC 直接查询广告主 ID，绕过网页搜索联想。"""
        region = self._resolve_region(payload)
        max_results = self._parse_max_results(payload.get("maxResults")) or self._ATC_MAX_CREATIVES
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
            "Accept": "application/json,text/plain,*/*",
            "Referer": f"https://adstransparency.google.com/advertiser/{advertiser_id}?region={region}",
        }
        timeout = httpx.Timeout(self._parse_positive_int(payload.get("requestTimeoutMs"), 30000) / 1000)
        items: list[dict] = []
        token = ""
        scanned = 0
        try:
            async with httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=True) as client:
                while scanned < max_results:
                    data = await self._atc_rpc(client, "SearchService/SearchCreatives", self._atc_search_payload(advertiser_id, token))
                    page = data.get("1") or []
                    if not page:
                        break
                    for creative in page:
                        if scanned >= max_results:
                            break
                        scanned += 1
                        creative_id = str(creative.get("2") or "").strip()
                        if not creative_id:
                            continue
                        preview_urls = await self._atc_preview_urls(client, advertiser_id, creative)
                        preview = {}
                        for preview_url in preview_urls:
                            try:
                                response = await client.get(preview_url)
                                if response.is_success:
                                    preview = self._parse_atc_preview(response.text)
                                    if preview.get("mediaUrl") or preview.get("youtubeVideoId") or preview.get("copyText"):
                                        break
                            except httpx.HTTPError:
                                continue
                        creative_text = " ".join(filter(None, [
                            preview.get("previewText"), preview.get("productName"), preview.get("copyText")
                        ]))
                        item = {
                            "media": "GOOGLE_TRANSPARENCY_WEB",
                            "externalAdID": creative_id,
                            "identityConfidence": "STABLE",
                            "advertiserID": advertiser_id,
                            "advertiserName": str(creative.get("12") or "") or None,
                            "region": region,
                            "format": preview.get("mediaType") or "UNKNOWN",
                            "title": preview.get("productName"),
                            "copyText": preview.get("copyText"),
                            "creativeUrl": f"https://adstransparency.google.com/advertiser/{advertiser_id}/creative/{creative_id}?region={region}",
                            "sourceUrl": f"https://adstransparency.google.com/advertiser/{advertiser_id}?region={region}",
                            "mediaUrl": preview.get("mediaUrl"),
                            "previewImageUrl": preview.get("previewImageUrl"),
                            "youtubeVideoId": preview.get("youtubeVideoId"),
                            "creativeFunction": self._infer_function(creative_text),
                            "language": self._infer_language(creative_text),
                            "platformData": {
                                "source": "ATC_RPC",
                                "creativeId": creative_id,
                                "lastShown": creative.get("7"),
                                "previewUrls": preview_urls,
                                "previewText": preview.get("previewText"),
                                "packageName": preview.get("packageName"),
                            },
                        }
                        items.append(item)
                    token = str(data.get("2") or "")
                    if not token or len(page) < self._ATC_PAGE_SIZE:
                        break
            items = await enrich_youtube_metadata(items)
            for item in items:
                if not item.get("creativeFunction"):
                    item["creativeFunction"] = self._infer_function(" ".join(filter(None, [
                        item.get("title"), item.get("copyText"), item.get("youtubeTitle")
                    ])))
                if not item.get("language"):
                    item["language"] = self._infer_language(" ".join(filter(None, [
                        item.get("title"), item.get("copyText"), item.get("youtubeTitle")
                    ])))
            if not items:
                return WorkerResult(status=JobStatus.FAILED, errorCode="GOOGLE_CREATIVES_UNREADABLE", errorMessage=f"ATC 未解析到广告主素材：{advertiser_id}")
            return WorkerResult(fetchedCount=len(items), newCount=len(items), items=items)
        except (RuntimeError, httpx.HTTPError) as exc:
            return WorkerResult(status=JobStatus.FAILED, errorCode="GOOGLE_ATC_REQUEST_FAILED", errorMessage=str(exc)[:500])

    @staticmethod
    async def _load_delivery_regions(context, items):
        """全球查询只读取单条广告详情中可见的展示地区菜单。"""
        detail = await context.new_page()
        try:
            for item in items:
                url = item.get("creativeUrl") or ""
                if not re.match(r"^https://adstransparency\.google\.com/advertiser/AR[0-9]+/creative/CR[0-9]+", url):
                    continue
                try:
                    response = await detail.goto(url, wait_until="domcontentloaded", timeout=12000)
                    if response and response.status in (403, 429):
                        break
                    if not response or not response.ok:
                        continue
                    await detail.locator("creative-region-filter [role=button]:visible").first.click(timeout=5000)
                    options = detail.locator("material-select-item[role=option]:visible")
                    await options.first.wait_for(timeout=3000)
                    codes = await options.evaluate_all("""options => {
                        const names = new Map();
                        for (const locale of ['en', 'zh-CN', 'zh-TW', 'zh-HK', document.documentElement.lang || 'en']) {
                            const display = new Intl.DisplayNames(locale, {type:'region', fallback:'none'});
                            for (let i=0; i<676; i++) {
                                const code = String.fromCharCode(65 + Math.floor(i/26), 65 + i%26);
                                const name = display.of(code);
                                if (name && !['EU','UN','EZ','ZZ'].includes(code)) names.set(name, code);
                            }
                        }
                        for (const [name, code] of Object.entries({
                            '大韓民國':'KR', '香港':'HK', '澳门':'MO', '澳門':'MO', '台湾':'TW',
                            '波斯尼亚 黑塞哥维那':'BA'
                        })) names.set(name, code);
                        return [...new Set(options.map(el => names.get(el.innerText.trim())).filter(Boolean))].sort();
                    }""")
                    if codes:
                        item["regions"] = codes
                except PlaywrightError:
                    continue
        finally:
            await detail.close()

    @staticmethod
    async def _parse_creatives(
        page, max_results: int | None, region: str, script_timeout_ms: int
    ) -> list[dict]:
        """从素材卡片的无障碍标签、预览脚本和嵌套广告 iframe 提取素材。"""
        advertiser = ""
        current_name = page.locator(".advertiser-name.current-scope")
        if await current_name.count():
            advertiser = (await current_name.first.inner_text()).strip()
        advertiser_match = re.search(r"/advertiser/([^/?]+)", page.url)
        advertiser_id = advertiser_match.group(1) if advertiser_match else None
        result: list[dict] = []
        seen_external_ids: set[str] = set()
        creatives = page.locator("creative")
        creative_links = page.locator("a[href*='/creative/']")
        creative_count = await creatives.count()
        link_count = await creative_links.count()
        # 前几条视频经常只有预览脚本没有可下载地址，向后多扫描一些卡片，避免结果页全部没有素材。
        scan_limit = creative_count if max_results is None else min(creative_count, max(max_results * 20, max_results))
        records: list[dict] = []
        for index in range(scan_limit):
            card = creatives.nth(index)
            label = await card.get_attribute("aria-label") or ""
            scripts = card.locator("script[src*='creativeId=']")
            src = await scripts.first.get_attribute("src") if await scripts.count() else None
            creative_match = re.search(r"[?&]creativeId=(\d+)", src or "")
            version_match = re.search(r"[?&]versionId=(\d+)", src or "")
            creative_id = creative_match.group(1) if creative_match else None
            if not creative_id:
                # 排序位置会变化，不能用作跨广告主共享的素材唯一键。
                continue
            # Google 结果页使用虚拟列表时可能重复挂载同一张卡片，按平台素材 ID 去重。
            if creative_id in seen_external_ids:
                continue
            seen_external_ids.add(creative_id)
            card_text = (await card.inner_text()).lower()
            detail_href = await creative_links.nth(index).get_attribute("href") if index < link_count else None
            detail_url = urljoin(page.url, detail_href) if detail_href else None
            records.append({
                "card": card,
                "label": label,
                "src": src,
                "creative_id": creative_id,
                "version_id": version_match.group(1) if version_match else None,
                "card_text": card_text,
                "detail_url": detail_url,
                "has_video_icon": await card.locator("[icon='videocam']").count() > 0,
            })

        # 预览脚本请求彼此独立，限制并发后可以避免逐卡等待失效 URL。
        semaphore = asyncio.Semaphore(8)

        async def extract(record: dict) -> dict:
            async with semaphore:
                return await GoogleTransparencyWorker._extract_preview(
                    page, record["card"], record["src"], script_timeout_ms
                )

        for start in range(0, len(records), 8):
            batch = records[start:start + 8]
            previews = await asyncio.gather(*(extract(record) for record in batch), return_exceptions=True)
            for record, preview in zip(batch, previews):
                if isinstance(preview, Exception):
                    preview = {
                        "mediaUrl": None,
                        "previewImageUrl": None,
                        "previewText": None,
                        "mediaType": None,
                        "productName": None,
                        "packageName": None,
                        "copyText": None,
                        "youtubeVideoId": None,
                    }
                is_video = (
                    preview["mediaType"] == "VIDEO"
                    or "videocam" in record["card_text"]
                    or record["has_video_icon"]
                )
                media_type = "VIDEO" if is_video else preview["mediaType"]
                media_url = GoogleTransparencyWorker._normalize_media_url(preview["mediaUrl"])
                preview_image_url = GoogleTransparencyWorker._normalize_media_url(preview["previewImageUrl"])
                platform_data = {
                    "versionId": record["version_id"],
                    "cardLabel": record["label"],
                    "previewScriptUrl": record["src"],
                    "previewText": preview["previewText"],
                    "previewImageUrl": preview_image_url,
                    # 预览文案中通常包含竞品产品名；包名只有页面明确返回时才填入，不能臆造。
                    "detectedProductName": preview["productName"],
                    "detectedPackageName": preview["packageName"],
                    "youtubeVideoId": preview["youtubeVideoId"],
                }
                item = {
                    "media": "GOOGLE_TRANSPARENCY_WEB",
                    "externalAdID": record["creative_id"],
                    "identityConfidence": "STABLE",
                    "advertiserID": advertiser_id,
                    "advertiserName": advertiser,
                    "region": region,
                    "format": media_type or "UNKNOWN",
                    # 标准列用于 CMS 直接展示，完整预览文案仍保存在 platformData 中。
                    "title": preview["productName"],
                    "copyText": preview["copyText"],
                    "versionId": record["version_id"],
                    "cardLabel": record["label"],
                    "creativeUrl": record["detail_url"] or (
                        f"https://adstransparency.google.com/advertiser/{advertiser_id}/creative/{record['creative_id']}?region={region}"
                        if advertiser_id else None
                    ),
                    "sourceUrl": page.url,
                    "previewScriptUrl": record["src"],
                    "mediaUrl": media_url,
                    "youtubeVideoId": preview["youtubeVideoId"],
                    "creativeFunction": GoogleTransparencyWorker._infer_function(
                        " ".join(filter(None, [preview["previewText"], preview["productName"], preview["copyText"], record["label"]]))
                    ),
                    # 页面卡片标签可能是当前界面的中文控件文案，不能作为素材语言依据。
                    "language": GoogleTransparencyWorker._infer_language(
                        " ".join(filter(None, [preview["previewText"], preview["productName"], preview["copyText"]]))
                    ),
                    "platformData": platform_data,
                }
                result.append(item)
        selected = result if max_results is None else result[:max_results]
        selected = await enrich_youtube_metadata(selected)
        for item in selected:
            if not item.get("creativeFunction"):
                item["creativeFunction"] = GoogleTransparencyWorker._infer_function(
                    " ".join(filter(None, [item.get("title"), item.get("copyText"), item.get("youtubeTitle")]))
                )
            if not item.get("language"):
                item["language"] = GoogleTransparencyWorker._infer_language(
                    " ".join(filter(None, [item.get("title"), item.get("copyText"), item.get("youtubeTitle")]))
                )
        return selected

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
    def _parse_positive_int(value: object, default: int) -> int:
        """解析正整数配置，错误或非正数使用默认值。"""
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        return parsed if parsed > 0 else default

    @staticmethod
    def _resolve_region(payload: dict) -> str:
        """把可选国家/地区输入转换为 Google Transparency 的地区参数。"""
        region = payload.get("region")
        if region is None or str(region).strip() == "":
            country_codes = payload.get("countryCodes") or []
            region = country_codes[0] if country_codes else "ANYWHERE"
        normalized = str(region).strip().upper()
        return "ANYWHERE" if normalized in {"", "ALL", "ANYWHERE"} else normalized

    @staticmethod
    async def _load_more_creatives(page, max_results: int | None, wait_ms: int) -> None:
        """滚动 Google 广告主页面加载更多素材，直到达到上限或页面不再增长。"""
        stable_rounds = 0
        max_rounds = min(max_results * 20 if max_results else 300, 300)
        creatives = page.locator("creative")
        for _ in range(max_rounds):
            count_before = await creatives.count()
            if max_results is not None and count_before >= max_results:
                return
            await page.mouse.wheel(0, 1600)
            await page.wait_for_timeout(min(max(wait_ms // 4, 300), 1500))
            count_after = await creatives.count()
            if count_after <= count_before:
                stable_rounds += 1
            else:
                stable_rounds = 0
            if stable_rounds >= 5:
                return

    @staticmethod
    def _is_http_url(value: str | None) -> bool:
        """只把 Java 媒体归档可以直接请求的 HTTP(S) 地址写入 mediaUrl。"""
        return bool(value and re.match(r"^https?://", value, re.I))

    @staticmethod
    def _normalize_media_url(value: str | None) -> str | None:
        """把 Google 返回的协议相对媒体地址规范化为可下载的 HTTPS 地址。"""
        if not value:
            return None
        normalized = value.strip()
        # Google 的预览脚本有时把查询分隔符按 JavaScript 字符串转义返回，
        # 例如 `\\=` 和 `\\&`。还原后 Java Manager 才能直接下载视频。
        normalized = re.sub(r"\\([/?=&:#])", r"\1", normalized)
        normalized = normalized.split(");", 1)[0]
        if normalized.startswith("//"):
            normalized = "https:" + normalized
        return normalized if GoogleTransparencyWorker._is_http_url(normalized) else None

    @staticmethod
    async def _extract_preview(
        page, card, preview_script_url: str | None, script_timeout_ms: int
    ) -> dict[str, str | None]:
        """读取 Google 预览脚本和 iframe 内的图片、视频地址及可见文案。"""
        result: dict[str, str | None] = {
            "mediaUrl": None,
            "previewImageUrl": None,
            "previewText": None,
            "mediaType": None,
            "productName": None,
            "packageName": None,
            "copyText": None,
            "youtubeVideoId": None,
        }
        script_preview = await GoogleTransparencyWorker._extract_script_preview(
            page, preview_script_url, script_timeout_ms
        )
        result.update({key: value for key, value in script_preview.items() if value})
        script_media_type = script_preview["mediaType"]
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
                    parsed = GoogleTransparencyWorker._parse_preview_text(result["previewText"])
                    result["productName"] = parsed["productName"]
                    result["packageName"] = parsed["packageName"]
                    result["copyText"] = parsed["copyText"]

                videos = content_frame.locator("video")
                sources = content_frame.locator("video media, media")
                images = content_frame.locator("img")
                video_url = None
                if await videos.count():
                    video_url = await videos.first.get_attribute("src")
                    result["mediaType"] = "VIDEO"
                    result["youtubeVideoId"] = result["youtubeVideoId"] or GoogleTransparencyWorker._extract_youtube_video_id(video_url)
                    if not video_url:
                        video_url = await videos.first.get_attribute("poster")
                if not video_url and await sources.count():
                    video_url = await sources.first.get_attribute("src")
                    if video_url:
                        result["mediaType"] = "VIDEO"
                        result["youtubeVideoId"] = result["youtubeVideoId"] or GoogleTransparencyWorker._extract_youtube_video_id(video_url)
                if await images.count():
                    image_url = await images.first.get_attribute("src")
                    if image_url:
                        result["previewImageUrl"] = image_url
                        result["youtubeVideoId"] = result["youtubeVideoId"] or GoogleTransparencyWorker._extract_youtube_video_id(image_url)
                        if not video_url and result["mediaType"] is None:
                            result["mediaType"] = "IMAGE"
                if video_url and (not result["mediaUrl"] or script_media_type != "VIDEO"):
                    result["mediaUrl"] = video_url
                elif result["previewImageUrl"] and not result["mediaUrl"]:
                    result["mediaUrl"] = result["previewImageUrl"]
        except Exception:
            # 单个广告的预览 iframe 可能被浏览器阻断，不能因此丢弃整页其他素材。
            return result
        return result

    @staticmethod
    async def _extract_script_preview(
        page, preview_script_url: str | None, timeout_ms: int
    ) -> dict[str, str | None]:
        """从 Google 预览脚本内的 VAST 片段提取带签名的视频和缩略图地址。"""
        result = {"mediaUrl": None, "previewImageUrl": None, "mediaType": None, "youtubeVideoId": None}
        if not preview_script_url:
            return result
        try:
            response = await page.context.request.get(
                preview_script_url,
                headers={"Referer": page.url, "User-Agent": "Mozilla/5.0"},
                timeout=timeout_ms,
            )
            if not response.ok:
                return result
            source = await response.text()
            source = GoogleTransparencyWorker._decode_preview_script(source)
            result["youtubeVideoId"] = GoogleTransparencyWorker._extract_youtube_video_id(source)
            video_match = re.search(
                r"//[^\"'\s<>]+googlevideo\.com/videoplayback\?[^\"'\s<>]+",
                source,
                re.IGNORECASE,
            )
            image_matches = re.findall(
                r"(?:(?:https?:)?//)(?:i\d+\.ytimg\.com|tpc\.googlesyndication\.com/simgad/|lh\d+\.googleusercontent\.com)/[^\"'\s<>]+",
                source,
                re.IGNORECASE,
            )
            if video_match:
                result["mediaUrl"] = GoogleTransparencyWorker._normalize_media_url(
                    video_match.group(0).rstrip("]")
                )
                result["mediaType"] = "VIDEO"
            if image_matches:
                preferred_image = next(
                    (url for url in image_matches if "ytimg.com" in url or "/simgad/" in url),
                    image_matches[0],
                )
                image_url = re.split(r"\);|,", preferred_image, maxsplit=1)[0]
                result["previewImageUrl"] = GoogleTransparencyWorker._normalize_media_url(image_url)
        except Exception:
            # 预览签名过期或脚本请求被拦截时，仍保留 iframe 解析结果。
            return result
        return result

    @staticmethod
    def _extract_youtube_video_id(value: str | None) -> str | None:
        """从 YouTube、缩略图或嵌入地址中提取标准 11 位视频 ID。"""
        text = str(value or "")
        match = re.search(
            r"(?:youtube(?:-nocookie)?\.com/(?:watch\?[^\s\"'<>]*?v=|embed/|shorts/)|youtu\.be/|ytimg\.com/(?:vi|vi_webp)/)([A-Za-z0-9_-]{11})",
            text,
            re.IGNORECASE,
        )
        return match.group(1) if match else None

    @staticmethod
    def _infer_language(text: str | None) -> str | None:
        """仅在文案中存在明确语言信号时返回语言代码。"""
        return infer_language(text)

    @staticmethod
    def _infer_function(text: str | None) -> str | None:
        """按明确的广告文案关键词归类素材用途，无法区分时留空。"""
        return infer_function(text)

    @staticmethod
    def _decode_preview_script(source: str) -> str:
        """还原 Google 脚本中使用的十六进制转义和 HTML 实体。"""
        decoded = re.sub(
            r"\\x([0-9a-fA-F]{2})",
            lambda match: chr(int(match.group(1), 16)),
            source,
        )
        decoded = re.sub(
            r"\\u([0-9a-fA-F]{4})",
            lambda match: chr(int(match.group(1), 16)),
            decoded,
        )
        return html.unescape(decoded).replace("\\/", "/")

    @staticmethod
    def _parse_preview_text(text: str | None) -> dict[str, str | None]:
        """从 Google 广告预览可见文案提取产品名、包名和描述，无法确认时保留空值。"""
        lines = [re.sub(r"\s+", " ", line).strip() for line in (text or "").splitlines()]
        lines = [line for line in lines if line]
        ignored = {
            "install", "instalar", "安装", "learn more", "了解详情", "sponsored",
            "visit advertiser", "skip", "×", "⋮", "downloads", "game_casual",
        }
        meaningful = [line for line in lines if line.lower() not in ignored and not re.fullmatch(r"[·.\s]+", line)]
        if not meaningful:
            return {"productName": None, "packageName": None, "copyText": None}
        # 过滤 Google 展示控件、价格占位符和下载量占位符。
        meaningful = [line for line in meaningful if not re.search(r"^(AdNaN|\[Number of downloads\])", line, re.I)]
        product_name = meaningful[0] if meaningful else None
        # 不能直接取第一行的 search 结果；产品标题通常不是包名，要继续查找后续文案。
        package_match = next(
            (match for line in meaningful
             if (match := re.search(r"\b([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){1,})\b", line, re.I))),
            None,
        )
        package_name = package_match.group(1) if package_match else None
        copy_lines = [line for line in meaningful[1:] if line != product_name and line != package_name]
        return {
            "productName": product_name[:500] if product_name else None,
            "packageName": package_name[:255] if package_name else None,
            "copyText": "\n".join(copy_lines)[:4000] if copy_lines else None,
        }
