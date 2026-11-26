import asyncio
import html
import json
import logging
import re
import time
from collections import Counter
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlsplit

import httpx

from playwright.async_api import Error as PlaywrightError, async_playwright

from ...creative_fields import infer_function, infer_google_language, infer_language
from ...config import settings
from ...models import JobStatus, WorkerResult
from ...youtube_meta import enrich as enrich_youtube_metadata
from ..base import BaseWorker
from .geo import COUNTRY_CODES


logger = logging.getLogger("uvicorn.error")
_run_counts: ContextVar[Counter | None] = ContextVar("google_run_counts", default=None)


def _count(name: str):
    counts = _run_counts.get()
    if counts is not None:
        counts[name] += 1


class _RateLimited(RuntimeError):
    """暂停同一主机的显式请求，下一次调度可以继续。"""


_cooldowns: dict[str, float] = {}
_consecutive_limits: dict[str, int] = {}
_preview_locks: dict[str, asyncio.Lock] = {}
_next_preview: dict[str, float] = {}


@asynccontextmanager
async def _preview_slot(host: str):
    async with _preview_locks.setdefault(host, asyncio.Lock()):
        _check_cooldown(host)
        await asyncio.sleep(max(0, _next_preview.get(host, 0) - time.monotonic()))
        _check_cooldown(host)
        _next_preview[host] = time.monotonic() + settings.google_preview_interval_seconds
        yield


def _check_cooldown(host: str):
    if _cooldowns.get(host, 0) > time.monotonic():
        _count("cooldown")
        raise _RateLimited(f"{host} HTTP 429，等待冷却")


def _cool_down(host: str, retry_after: str | None):
    _count("http429")
    count = min(5, _consecutive_limits.get(host, 0) + 1)
    _consecutive_limits[host] = count
    seconds = 60 * 2 ** (count - 1)
    if retry_after:
        try:
            seconds = int(retry_after)
        except ValueError:
            try:
                seconds = parsedate_to_datetime(retry_after).timestamp() - time.time()
            except (TypeError, ValueError, OverflowError):
                pass
    _cooldowns[host] = max(_cooldowns.get(host, 0), time.monotonic() + max(1, seconds))
    raise _RateLimited(f"{host} HTTP 429，等待 {max(1, int(seconds))} 秒")


async def _open_google_page(page, url: str, timeout: int):
    _check_cooldown(urlsplit(url).hostname)
    response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
    if response:
        host = urlsplit(response.url).hostname
        if response.status == 429:
            _cool_down(host, response.headers.get("retry-after"))
        if response.ok:
            _consecutive_limits.pop(host, None)
    return response


def _destination(href: str) -> str | None:
    """Use an explicit ad destination, including one disclosed in a Google click URL."""
    try:
        link = urlsplit(href)
        host = (link.hostname or "").lower()
        tracking_domains = ("googleadservices.com", "doubleclick.net")
        if any(host == d or host.endswith("." + d) for d in tracking_domains) or link.path == "/aclk":
            href = next(iter(parse_qs(link.query).get("adurl", [])), "")
            link = urlsplit(href)
            host = (link.hostname or "").lower()
        if link.scheme not in ("http", "https") or not host:
            return None
        if any(host == d or host.endswith("." + d) for d in (*tracking_domains, "example.com", "youradchoices.com")):
            return None
        if host in {"support.google.com", "adstransparency.google.com", "adssettings.google.com", "myadcenter.google.com"}:
            return None
        return href
    except ValueError:
        return None


def _landing_url(source: str) -> str | None:
    match = re.search(r"""["'](?:landingPageUrl|landingUrl|finalUrl)["']\s*:\s*["'](https?://[^"'<>\s]+)""", source, re.I)
    if match:
        destination = _destination(match.group(1))
        if destination:
            return destination
    for click in re.finditer(r"""https?://[a-z0-9.-]+\.(?:googleadservices\.com|doubleclick\.net)/[^\s"'<>]+""", source, re.I):
        destination = _destination(click.group())
        if destination:
            return destination
    return None


def _has_content(preview: dict) -> bool:
    return any(str(preview.get(key) or "").strip() for key in (
        "mediaUrl", "previewImageUrl", "youtubeVideoId", "copyText", "landingUrl",
    ))


class _InlineAd(HTMLParser):
    """Read only visible ad text and links from an ATC inline HTML variant."""

    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0
        self.image = None
        self.landing = None

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "template"}:
            self.hidden += 1
        if self.hidden:
            return
        attrs = dict(attrs)
        if tag == "img" and not self.image:
            src = attrs.get("src") or ""
            if re.match(r"^https://[a-z0-9.-]+\.(?:googlesyndication|googleusercontent|ytimg)\.com/", src, re.I):
                self.image = src
        if tag == "a" and not self.landing:
            href = attrs.get("href") or ""
            if re.match(r"^https?://[^/\s]+", href, re.I):
                self.landing = _destination(href)

    def handle_endtag(self, tag):
        if tag in {"script", "style", "template"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden and data.strip():
            self.parts.append(" ".join(data.split()))

    def fields(self):
        return {"mediaUrl": self.image, "mediaType": "IMAGE" if self.image else ("TEXT" if self.parts else None),
                "copyText": "\n".join(self.parts)[:4000] or None, "landingUrl": self.landing}


class _MarkedCopy(HTMLParser):
    """Collect only explicitly labelled OCH and VAST copy inside a preview script."""

    def __init__(self):
        super().__init__()
        self.tag = None
        self.depth = 0
        self.parts = []
        self.texts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "template"}:
            self.hidden += 1
        if self.hidden:
            return
        if self.tag:
            if tag == self.tag:
                self.depth += 1
            if tag == "br":
                self.parts.append(" ")
            return
        targets = (dict(attrs).get("data-asoch-targets") or "").split()
        if tag in {"adtitle", "description"} or (
            tag in {"div", "span"} and {"ochTitle", "ochDescription", "ochHeadline", "ochAppName"}.intersection(targets)
        ):
            self.tag, self.depth = tag, 1
            self.parts = []

    def handle_data(self, data):
        if self.tag and not self.hidden:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if self.hidden:
            if tag in {"script", "style", "template"}:
                self.hidden -= 1
            return
        if tag == self.tag:
            self.depth -= 1
            if not self.depth:
                value = " ".join("".join(self.parts).split())
                if value and not re.match(r"(?:https?:)?//", value, re.I) and value not in self.texts:
                    self.texts.append(value[:4000])
                self.tag = None


class GoogleTransparencyWorker(BaseWorker):
    """通过 Google Ads Transparency 公开页面搜索广告主并提取素材卡片。"""

    name = "google_transparency"

    _ATC_RPC = "https://adstransparency.google.com/anji/_/rpc/"
    _ATC_GEO = 2344
    _ATC_PAGE_SIZE = 40
    _ATC_MAX_CREATIVES = 500
    _ADVERTISER_ID = re.compile(r"^AR\d{10,}$", re.IGNORECASE)

    async def run(self, payload: dict) -> WorkerResult:
        counts = Counter()
        token = _run_counts.set(counts)
        keyword = str(payload.get("keyword") or "").strip()
        rpc = self._is_advertiser_id(keyword)
        result = None
        try:
            result = await self._run_atc(payload, keyword.upper()) if rpc else await self._search_web(payload, keyword)
            return result
        finally:
            _run_counts.reset(token)
            logger.info(
                "Google 抓取汇总 runID=%s queryID=%s route=%s keyword=%s region=%s status=%s "
                "候选=%d 窗口外=%s 已入库跳过=%d 预览请求=%d 预览失败=%d 预览成功但无内容=%d "
                "无内容跳过=%d HTTP429=%d 冷却拦截=%d 返回=%d",
                payload.get("runID"), payload.get("queryID"), "RPC" if rpc else "WEB", keyword,
                self._resolve_region(payload), result.status.value if result else "INTERRUPTED",
                counts["candidates"], counts["old"] if rpc else "网页端过滤", counts["known"],
                counts["requests"], counts["failed"], counts["empty"], counts["skipped"],
                counts["http429"], counts["cooldown"], len(result.items) if result else 0,
            )

    async def _search_web(self, payload: dict, keyword: str) -> WorkerResult:
        if not keyword:
            return WorkerResult(
                status=JobStatus.FAILED,
                errorCode="GOOGLE_ADVERTISER_REQUIRED",
                errorMessage="必须提供 keyword",
            )

        # Google 未填写国家/地区时使用 ANYWHERE；宿主按地区分别调用。
        region = GoogleTransparencyWorker._resolve_region(payload)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context(locale="zh-CN")
            page = await context.new_page()
            try:
                filters = {"region": region}
                if payload.get("windowStart") and payload.get("windowEnd"):
                    filters.update({"start-date": payload["windowStart"][:10], "end-date": payload["windowEnd"][:10]})
                await _open_google_page(page, "https://adstransparency.google.com/?" + urlencode(filters), 60000)
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
                await GoogleTransparencyWorker._load_more_creatives(page, min(max_results or 500, 500), int(payload.get("resultWaitMs", 6000)))
                script_timeout_ms = GoogleTransparencyWorker._parse_positive_int(
                    payload.get("scriptTimeoutMs"), 8000
                )
                items, limited = await self._parse_creatives(page, max_results, region, script_timeout_ms,
                                                            set(payload.get("knownCreativeIDs") or []))
                if items is None:
                    return WorkerResult(status=JobStatus.FAILED, errorCode="GOOGLE_CREATIVES_UNREADABLE",
                                        errorMessage="Google 素材卡片未返回可识别的平台素材 ID")
                if items:
                    # 留出关闭浏览器、回传已采集素材的时间。
                    remaining = payload.get("_countryDeadline", asyncio.get_running_loop().time() + 900)
                    remaining -= asyncio.get_running_loop().time() + 5
                    if remaining > 0:
                        try:
                            await asyncio.wait_for(self._load_delivery_regions(page.context, items), remaining)
                        except (asyncio.TimeoutError, PlaywrightError):
                            pass
                        except _RateLimited as exc:
                            limited = str(exc)
                if limited:
                    return self._limited_result(items, limited)
                return WorkerResult(fetchedCount=len(items), newCount=len(items), items=items)
            except _RateLimited as exc:
                return self._limited_result([], str(exc))
            except Exception as exc:  # noqa: BLE001 - 网页变化要回调明确错误
                return WorkerResult(status=JobStatus.FAILED, errorCode="GOOGLE_BROWSER_EXCEPTION", errorMessage=str(exc)[:500])
            finally:
                await browser.close()

    @classmethod
    def _is_advertiser_id(cls, value: str | None) -> bool:
        """识别 Google Ads Transparency 的 AR 广告主 ID。"""
        return bool(cls._ADVERTISER_ID.fullmatch(str(value or "").strip()))

    @classmethod
    def _atc_search_payload(cls, advertiser_id: str, page_token: str | None = None, region: str = "ANYWHERE") -> dict:
        payload = {
            "2": cls._ATC_PAGE_SIZE,
            "3": {"12": {"1": "", "2": True}, "13": {"1": [advertiser_id]}},
            "7": {"1": 1, "2": 0, "3": cls._ATC_GEO},
        }
        if region != "ANYWHERE":
            geo = next((key for key, code in COUNTRY_CODES.items() if code == region), None)
            if geo is None:
                raise ValueError(f"Google ATC 不支持的地区：{region}")
            payload["3"]["8"] = [geo]
            payload["7"]["3"] = geo
        if page_token:
            payload["4"] = page_token
        return payload

    @classmethod
    async def _atc_rpc(cls, client: httpx.AsyncClient, method: str, payload: dict) -> dict:
        """调用 ATC 内部 RPC。该接口按 f.req 接收 JSON，而不是普通 REST JSON。"""
        body = "f.req=" + quote(json.dumps(payload, separators=(",", ":")))
        last_error = "unknown"
        for attempt in range(3):
            host = urlsplit(cls._ATC_RPC).hostname
            _check_cooldown(host)
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
                _consecutive_limits.pop(host, None)
                try:
                    data = response.json()
                except ValueError as exc:
                    raise RuntimeError("ATC 返回不是合法 JSON") from exc
                if isinstance(data, dict):
                    return data
                raise RuntimeError("ATC 返回结构异常")
            last_error = f"HTTP {response.status_code}"
            if response.status_code == 429:
                _cool_down(host, response.headers.get("Retry-After"))
            break
        raise RuntimeError(f"ATC {method} 失败：{last_error}")

    @classmethod
    async def _atc_detail(cls, client, advertiser_id, creative_id):
        try:
            data = await cls._atc_rpc(client, "LookupService/GetCreativeById", {
                "1": advertiser_id, "2": creative_id, "5": {"1": 1, "2": 0, "3": cls._ATC_GEO},
            })
        except _RateLimited:
            raise
        except RuntimeError as exc:
            logger.info("ATC 详情读取失败 advertiserID=%s creativeID=%s: %s", advertiser_id, creative_id, exc)
            return {}
        detail = data.get("1")
        if isinstance(detail, dict) and detail.get("1") == advertiser_id and detail.get("2") == creative_id:
            return detail
        return {}

    @staticmethod
    def _atc_regions(detail):
        codes = set()
        for record in detail.get("17") or []:
            criterion = str(record.get("1", "")) if isinstance(record, dict) else ""
            if criterion.isascii() and criterion.isdecimal():
                code = COUNTRY_CODES.get(int(criterion))
                if code:
                    codes.add(code)
        return sorted(codes)

    @classmethod
    async def _atc_preview_urls(cls, client: httpx.AsyncClient, advertiser_id: str, item: dict) -> tuple[list[str], dict, dict | None]:
        """ATC can return a preview script or inline HTML in both search and detail."""
        def unpack(groups):
            urls = []
            for group in groups:
                if not isinstance(group, dict):
                    continue
                script = (group.get("1") or {}).get("4")
                if isinstance(script, str) and re.match(r"^https?://", script):
                    urls.append(script)
                markup = (group.get("3") or {}).get("2")
                if isinstance(markup, str) and markup.strip():
                    ad = _InlineAd()
                    ad.feed(markup)
                    ad.close()
                    fields = ad.fields()
                    if _has_content(fields):
                        return urls, fields
            return list(dict.fromkeys(urls)), {}

        urls, preview = unpack([item.get("3")])
        if urls or preview:
            return urls, preview, None
        detail = await cls._atc_detail(client, advertiser_id, str(item.get("2") or ""))
        urls, preview = unpack(detail.get("5") or [])
        return urls, preview, detail

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
        for field in ("headline", "description", "description1", "description2", "shortDescription", "longHeadline", "title"):
            for match in re.finditer(r"['\"]" + field + r"['\"]\s*:\s*(?:\[\s*)?['\"]([^'\"]{1,2000})", decoded, re.I):
                value = " ".join(match.group(1).split())
                if value and value not in texts:
                    texts.append(value)
        if re.search(r"data-asoch-targets|<AdTitle\b|<Description\b", decoded, re.I):
            parser = _MarkedCopy()
            markup = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", decoded, flags=re.S)
            try:
                parser.feed(markup.replace(r'\"', '"').replace(r"\'", "'"))
                parser.close()
            except (AssertionError, ValueError):
                pass  # Malformed markup can coexist with usable JSON fields and media.
            texts.extend(value for value in parser.texts if value not in texts)
        preview_text = "\n".join(texts[:20]) or None
        parsed = cls._parse_preview_text(preview_text)
        copy_text = "\n".join(texts[:10])[:4000] or None
        video_id = cls._extract_youtube_video_id(decoded)
        video_match = re.search(r"(?:(?:https?:)?//)[^\"'<>\s]+googlevideo\.com/videoplayback\?[^\"'<>\s]+", decoded, re.I)
        image_match = re.search(r"(?:(?:https?:)?//)(?:i\d*\.ytimg\.com/|tpc\.googlesyndication\.com/simgad/|lh\d+\.googleusercontent\.com/)[^\"'<>\s]+", decoded, re.I)
        media_url = cls._normalize_media_url(video_match.group(0).rstrip("])")) if video_match else None
        image_url = cls._normalize_media_url(image_match.group(0).rstrip("])")) if image_match else None
        return {
            "previewText": preview_text,
            "landingUrl": _landing_url(decoded),
            "productName": app_name or parsed["productName"],
            "packageName": package_name or parsed["packageName"],
            "copyText": copy_text or parsed["copyText"],
            "mediaUrl": media_url if video_id else (media_url or image_url),
            "previewImageUrl": image_url,
            "mediaType": "VIDEO" if media_url or video_id else ("IMAGE" if image_url else None),
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
        seen_ids: set[str] = set()
        seen_tokens: set[str] = set()
        known_ids = set(payload.get("knownCreativeIDs") or [])
        since = datetime.fromisoformat(payload["windowStart"].replace("Z", "+00:00")).timestamp() if payload.get("windowStart") else None
        scanned = 0
        deadline = payload.get("_countryDeadline", float("inf"))
        time_limited = False
        try:
            async with httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=True) as client:
                while len(items) < max_results and scanned < self._ATC_MAX_CREATIVES:
                    data = await self._atc_rpc(client, "SearchService/SearchCreatives", self._atc_search_payload(advertiser_id, token, region))
                    page = data.get("1") or []
                    if not page:
                        break
                    whole_page_old = since is not None
                    for creative in page:
                        if asyncio.get_running_loop().time() + 90 >= deadline:
                            time_limited = True
                            break
                        if len(items) >= max_results or scanned >= self._ATC_MAX_CREATIVES:
                            break
                        scanned += 1
                        _count("candidates")
                        timestamp = creative.get("7") or {}
                        last_shown = timestamp.get("1") if isinstance(timestamp, dict) else None
                        if since is not None and str(last_shown).isdigit() and int(last_shown) < since:
                            _count("old")
                            continue
                        whole_page_old = False
                        creative_id = str(creative.get("2") or "").strip()
                        if not creative_id or creative_id in seen_ids:
                            continue
                        seen_ids.add(creative_id)
                        if creative_id in known_ids:
                            _count("known")
                            continue
                        preview_urls, preview, detail = await self._atc_preview_urls(client, advertiser_id, creative)
                        creative_url = f"https://adstransparency.google.com/advertiser/{advertiser_id}/creative/{creative_id}?region={region}"
                        preview_status = None
                        for preview_url in preview_urls:
                            if _has_content(preview):
                                break
                            try:
                                host = urlsplit(preview_url).hostname
                                async with _preview_slot(host):
                                    _count("requests")
                                    response = await client.get(preview_url)
                                    if not response.is_success:
                                        _count("failed")
                                    if response.status_code == 429:
                                        logger.info("Google 预览限流 queryID=%s creativeID=%s", payload.get("queryID"), creative_id)
                                        _cool_down(host, response.headers.get("Retry-After"))
                                    if response.is_success:
                                        _consecutive_limits.pop(host, None)
                                preview_status = response.status_code
                                if response.is_success:
                                    preview = self._parse_atc_preview(response.text)
                                    if _has_content(preview):
                                        break
                                    _count("empty")
                            except httpx.HTTPError:
                                _count("failed")
                                preview_status = "network_error"
                        if not _has_content(preview):
                            _count("skipped")
                            logger.info("跳过空素材 queryID=%s advertiserID=%s creativeID=%s previewStatus=%s", payload.get("queryID"), advertiser_id, creative_id, preview_status)
                            continue
                        detail_limit = None
                        if detail is None:
                            try:
                                detail = await self._atc_detail(client, advertiser_id, creative_id)
                            except _RateLimited as exc:
                                detail = {}
                                detail_limit = exc
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
                            "regions": self._atc_regions(detail),
                            "format": preview.get("mediaType") or ("TEXT" if preview.get("copyText") else None),
                            "title": preview.get("productName"),
                            "copyText": preview.get("copyText"),
                            "landingUrl": preview.get("landingUrl"),
                            "creativeUrl": creative_url,
                            "sourceUrl": f"https://adstransparency.google.com/advertiser/{advertiser_id}?region={region}",
                            "mediaUrl": preview.get("mediaUrl"),
                            "previewImageUrl": preview.get("previewImageUrl"),
                            "youtubeVideoId": preview.get("youtubeVideoId"),
                            "creativeFunction": self._infer_function(creative_text),
                            "platformData": {
                                "source": "ATC_RPC",
                                "creativeId": creative_id,
                                "lastShown": creative.get("7"),
                                "previewUrls": preview_urls,
                                "previewStatus": preview_status,
                                "previewText": preview.get("previewText"),
                                "packageName": preview.get("packageName"),
                            },
                        }
                        items.append(item)
                        if detail_limit:
                            raise detail_limit
                    token = str(data.get("2") or "")
                    if time_limited or whole_page_old or not token or token in seen_tokens:
                        break
                    seen_tokens.add(token)
            items = await self._enrich_items(items)
            if time_limited:
                return WorkerResult(status=JobStatus.PARTIAL_SUCCESS if items else JobStatus.TIMEOUT, items=items,
                                    fetchedCount=len(items), newCount=len(items), errorCode="GOOGLE_SCAN_TIME_BUDGET",
                                    errorMessage="已达到本次抓取时间预算")
            if scanned and not seen_ids and not whole_page_old:
                return WorkerResult(status=JobStatus.FAILED, errorCode="GOOGLE_CREATIVES_UNREADABLE", errorMessage=f"ATC 未解析到广告主素材：{advertiser_id}")
            return WorkerResult(fetchedCount=len(items), newCount=len(items), items=items)
        except _RateLimited as exc:
            items = await self._enrich_items(items)
            return self._limited_result(items, str(exc))
        except (RuntimeError, httpx.HTTPError) as exc:
            return WorkerResult(status=JobStatus.FAILED, errorCode="GOOGLE_ATC_REQUEST_FAILED", errorMessage=str(exc)[:500])

    @staticmethod
    def _limited_result(items, message):
        return WorkerResult(status=JobStatus.PARTIAL_SUCCESS if items else JobStatus.FAILED,
                            items=items, fetchedCount=len(items), newCount=len(items),
                            errorCode="GOOGLE_RATE_LIMITED", errorMessage=message, restrictionType="RATE_LIMITED")

    @staticmethod
    async def _load_delivery_regions(context, items):
        """读取素材详情的完整展示地区，独立于本次搜索的国家范围。"""
        detail = await context.new_page()
        try:
            for item in items:
                url = item.get("creativeUrl") or ""
                if not re.match(r"^https://adstransparency\.google\.com/advertiser/AR[0-9]+/creative/CR[0-9]+", url):
                    continue
                try:
                    global_url = urlsplit(url)._replace(query="region=anywhere", fragment="").geturl()
                    response = await _open_google_page(detail, global_url, 12000)
                    if response and response.status == 403:
                        break
                    if not response or not response.ok:
                        continue
                    await detail.locator("creative-region-filter [role=button]:visible").first.click(timeout=5000)
                    options = detail.locator("material-select-item[role=option]:visible")
                    await options.first.wait_for(timeout=3000)
                    codes = await options.evaluate_all("""(options, countryCodes) => {
                        const names = new Map();
                        for (const locale of ['en', 'zh-CN', 'zh-TW', 'zh-HK', document.documentElement.lang || 'en']) {
                            const display = new Intl.DisplayNames(locale, {type:'region', fallback:'none'});
                            for (const code of countryCodes) {
                                const name = display.of(code);
                                if (name && !['EU','UN','EZ','ZZ'].includes(code)) names.set(name, code);
                            }
                        }
                        for (const [name, code] of Object.entries({
                            '大韓民國':'KR', '香港':'HK', '澳门':'MO', '澳門':'MO', '台湾':'TW',
                            '波斯尼亚 黑塞哥维那':'BA', '刚果':'CG', '刚果民主共和国':'CD', '马达加斯加岛':'MG'
                        })) names.set(name, code);
                        return [...new Set(options.map(el => names.get(el.innerText.trim())).filter(Boolean))].sort();
                    }""", sorted(set(COUNTRY_CODES.values())))
                    if codes:
                        item["regions"] = codes
                except PlaywrightError:
                    continue
        finally:
            await detail.close()

    @staticmethod
    async def _parse_creatives(
        page, max_results: int | None, region: str, script_timeout_ms: int, known_ids=None
    ) -> tuple[list[dict] | None, str | None]:
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
        creative_count = await creatives.count()
        # 前几条视频经常只有预览脚本没有可下载地址，向后多扫描一些卡片，避免结果页全部没有素材。
        scan_limit = creative_count if max_results is None else min(creative_count, max(max_results * 20, max_results))
        records: list[dict] = []
        skipped_known = False
        for index in range(min(scan_limit, GoogleTransparencyWorker._ATC_MAX_CREATIVES)):
            _count("candidates")
            card = creatives.nth(index)
            label = await card.get_attribute("aria-label") or ""
            scripts = card.locator("script[src*='creativeId=']")
            src = await scripts.first.get_attribute("src") if await scripts.count() else None
            creative_match = re.search(r"[?&]creativeId=(\d+)", src or "")
            version_match = re.search(r"[?&]versionId=(\d+)", src or "")
            creative_id = creative_match.group(1) if creative_match else None
            card_link = card.locator("xpath=ancestor-or-self::a[contains(@href, '/creative/')] | .//a[contains(@href, '/creative/')]").first
            href = await card_link.get_attribute("href") if await card_link.count() else None
            detail_url = urljoin(page.url, href) if href else None
            linked_id = re.match(r"^https://adstransparency\.google\.com/advertiser/AR[0-9]+/creative/(CR[0-9]+)(?:[/?#]|$)", detail_url or "")
            if not linked_id:
                detail_url = None
            else:
                creative_id = linked_id.group(1)
            if known_ids and creative_id in known_ids:
                _count("known")
                skipped_known = True
                continue
            if not creative_id:
                # 排序位置会变化，不能用作跨广告主共享的素材唯一键。
                continue
            # Google 结果页使用虚拟列表时可能重复挂载同一张卡片，按平台素材 ID 去重。
            if creative_id in seen_external_ids:
                continue
            seen_external_ids.add(creative_id)
            card_text = (await card.inner_text()).lower()
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

        if not records:
            if skipped_known:
                return [], None
            return None, None  # 卡片结构无法识别，与已识别但内容全部为空分别处理。
        # 预览脚本请求彼此独立，限制并发后可以避免逐卡等待失效 URL。
        async def extract(record: dict) -> dict:
            return await GoogleTransparencyWorker._extract_preview(
                page, record["card"], record["src"], script_timeout_ms
            )

        batch_size = min(max_results or 2, 2)
        limited = None
        for start in range(0, len(records), batch_size):
            if limited or (max_results and len(result) >= max_results):
                break
            batch = records[start:start + batch_size]
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
                limited = preview.get("rateLimit") or limited
                is_video = (
                    preview["mediaType"] == "VIDEO"
                    or bool(preview["youtubeVideoId"])
                    or "videocam" in record["card_text"]
                    or record["has_video_icon"]
                )
                media_type = "VIDEO" if is_video else preview["mediaType"]
                media_url = GoogleTransparencyWorker._normalize_media_url(preview["mediaUrl"])
                preview_image_url = GoogleTransparencyWorker._normalize_media_url(preview["previewImageUrl"])
                if is_video and media_url == preview_image_url:
                    media_url = None
                if not preview.get("copyText") and preview.get("productName"):
                    preview["copyText"] = preview["productName"]
                if not _has_content(dict(preview, mediaUrl=media_url, previewImageUrl=preview_image_url)):
                    _count("skipped")
                    logger.info("跳过空素材 advertiserID=%s creativeID=%s", advertiser_id, record["creative_id"])
                    continue
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
                    "format": media_type or ("TEXT" if preview.get("copyText") else None),
                    # 标准列用于 CMS 直接展示，完整预览文案仍保存在 platformData 中。
                    "title": preview["productName"],
                    "copyText": preview["copyText"],
                    "versionId": record["version_id"],
                    "cardLabel": record["label"],
                    "creativeUrl": record["detail_url"],
                    "sourceUrl": page.url,
                    "previewScriptUrl": record["src"],
                    "mediaUrl": media_url,
                    "previewImageUrl": preview_image_url,
                    "landingUrl": preview.get("landingUrl"),
                    "youtubeVideoId": preview["youtubeVideoId"],
                    "creativeFunction": GoogleTransparencyWorker._infer_function(
                        " ".join(filter(None, [preview["previewText"], preview["productName"], preview["copyText"], record["label"]]))
                    ),
                    "platformData": platform_data,
                }
                result.append(item)
        selected = result if max_results is None else result[:max_results]
        return await GoogleTransparencyWorker._enrich_items(selected), limited

    @staticmethod
    async def _enrich_items(items):
        items = await enrich_youtube_metadata(items)
        for item in items:
            if not item.get("creativeFunction"):
                item["creativeFunction"] = GoogleTransparencyWorker._infer_function(
                    " ".join(filter(None, [item.get("title"), item.get("copyText"), item.get("youtubeTitle")]))
                )
            if not item.get("language"):
                ad_text = " ".join(filter(None, [item.get("title"), item.get("copyText")]))
                item["language"] = infer_google_language(item.get("youtubeTitle"), ad_text)
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
                if body_text and not result["copyText"]:
                    result["previewText"] = body_text[:2000]
                    parsed = GoogleTransparencyWorker._parse_preview_text(result["previewText"])
                    result["productName"] = parsed["productName"] or result["productName"]
                    result["packageName"] = parsed["packageName"] or result["packageName"]
                    result["copyText"] = parsed["copyText"]

                if not result.get("landingUrl"):
                    links = await content_frame.locator("a[href]").evaluate_all("els => els.map(el => el.href)")
                    result["landingUrl"] = next((dest for href in links if (dest := _destination(href))), None)
                videos = content_frame.locator("video")
                sources = content_frame.locator("video source[src], video media[src], media[src]")
                images = content_frame.locator("img")
                video_url = None
                if await videos.count():
                    video_url = GoogleTransparencyWorker._normalize_media_url(await videos.first.get_attribute("src"))
                    result["mediaType"] = "VIDEO"
                    result["youtubeVideoId"] = result["youtubeVideoId"] or GoogleTransparencyWorker._extract_youtube_video_id(video_url)
                    poster = GoogleTransparencyWorker._normalize_media_url(await videos.first.get_attribute("poster"))
                    if poster:
                        result["previewImageUrl"] = poster
                        result["youtubeVideoId"] = result["youtubeVideoId"] or GoogleTransparencyWorker._extract_youtube_video_id(poster)
                if not video_url and await sources.count():
                    video_url = GoogleTransparencyWorker._normalize_media_url(await sources.first.get_attribute("src"))
                    if video_url:
                        result["mediaType"] = "VIDEO"
                        result["youtubeVideoId"] = result["youtubeVideoId"] or GoogleTransparencyWorker._extract_youtube_video_id(video_url)
                if await images.count():
                    image_url = await images.first.get_attribute("src")
                    if image_url:
                        result["previewImageUrl"] = result["previewImageUrl"] or image_url
                        result["youtubeVideoId"] = result["youtubeVideoId"] or GoogleTransparencyWorker._extract_youtube_video_id(image_url)
                        if not video_url and result["mediaType"] is None:
                            result["mediaType"] = "IMAGE"
                if video_url and (not result["mediaUrl"] or script_media_type != "VIDEO"):
                    result["mediaUrl"] = video_url
                elif result["mediaType"] == "IMAGE" and not result["mediaUrl"]:
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
            host = urlsplit(preview_script_url).hostname
            async with _preview_slot(host):
                _count("requests")
                response = await page.context.request.get(
                    preview_script_url,
                    headers={"Referer": page.url, "User-Agent": "Mozilla/5.0"},
                    timeout=timeout_ms,
                )
                if response.status == 429:
                    _count("failed")
                    logger.info("Google 网页预览限流 host=%s", host)
                    _cool_down(host, response.headers.get("retry-after"))
                if response.ok:
                    _consecutive_limits.pop(host, None)
            if not response.ok:
                _count("failed")
                return result
            result.update(GoogleTransparencyWorker._parse_atc_preview(await response.text()))
            if not _has_content(result):
                _count("empty")
        except _RateLimited as exc:
            result["rateLimit"] = str(exc)
        except Exception:
            # 预览签名过期或脚本请求被拦截时，仍保留 iframe 解析结果。
            _count("failed")
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
        if not match:
            match = re.search(r"""(?:^|[\s"'(=])/(?:vi|vi_webp)/([A-Za-z0-9_-]{11})/""", text)
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
