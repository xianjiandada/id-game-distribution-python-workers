"""解析 TikTok 网页返回的广告搜索正文。"""

import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlencode, urlsplit

from ...models import JobStatus, WorkerResult


def cdn_url(value: object) -> str | None:
    """归档只接受 TikTok CDN 的 HTTPS 文件地址。"""
    if not isinstance(value, str) or re.search(r"[\s\\\x00-\x1f]", value):
        return None
    try:
        parts = urlsplit(value)
        if (parts.scheme == "https" and parts.username is None and parts.password is None
                and parts.port in (None, 443) and parts.path.startswith("/")
                and (parts.hostname or "").endswith((".tiktokcdn.com", ".tiktokcdn-eu.com",
                                                      ".tiktokcdn-us.com", ".tiktokv.com"))):
            return value
    except ValueError:
        pass
    return None


def shown_date(value: object) -> str | None:
    if isinstance(value, int) and 0 < value < 253402300800000:
        return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat().replace("+00:00", "Z")
    return None


class SearchResults:
    """按稳定广告 ID 合并搜索页，只统计实际解析到的素材。"""

    def __init__(self, url: str, limit: int | None, origin: str):
        self.url = url
        self.region = parse_qs(urlsplit(url).query).get("region", ["all"])[0]
        self.limit = limit
        self.origin = origin
        self.items: dict[str, dict] = {}
        self.has_more = False

    @property
    def full(self) -> bool:
        return self.limit is not None and len(self.items) >= self.limit

    def add_page(self, body: object) -> None:
        if not isinstance(body, dict) or type(body.get("code")) is not int or body["code"] != 0:
            raise ValueError("TikTok 搜索正文缺少成功状态 code=0")
        if not isinstance(body.get("data"), list) or not isinstance(body.get("has_more"), bool):
            raise ValueError("TikTok 搜索正文缺少广告列表或分页状态")
        self.has_more = body["has_more"]
        for ad in body["data"]:
            if self.full:
                break
            if not isinstance(ad, dict) or not re.fullmatch(r"[0-9]{1,30}", str(ad.get("id"))):
                raise ValueError("TikTok 广告缺少有效 ID")
            ad_id = str(ad["id"])
            if ad_id in self.items:
                continue
            videos = ad.get("videos") or []
            images = ad.get("image_urls") or []
            if not isinstance(videos, list) or not isinstance(images, list):
                raise ValueError("TikTok 广告媒体列表格式错误")
            video = next((url for v in videos if isinstance(v, dict) and (url := cdn_url(v.get("video_url")))), None)
            image = next((url for v in images if (url := cdn_url(v))), None)
            self.save(ad_id, ad, video, image, advertiserName=ad.get("name"), title=ad.get("title"),
                      deliveryStartTime=shown_date(ad.get("first_shown_date")),
                      deliveryStopTime=shown_date(ad.get("last_shown_date")))

    def save(self, ad_id: str, raw: dict, video: str | None, image: str | None, **fields) -> None:
        detail = "https://library.tiktok.com/ads/detail/?" + urlencode({"ad_id": ad_id, "region": self.region})
        reach = (raw.get("ad") or raw).get("reach") or {}
        breakdown = reach.get("unique_users_seen_by_country") if isinstance(reach, dict) else None
        if isinstance(breakdown, dict):
            fields["regions"] = sorted(code for code in breakdown if re.fullmatch("[A-Z]{2}", code)
                                       and str(breakdown[code]).strip() not in ("", "0", "None"))
        self.items[ad_id] = {
            "media": "TIKTOK_AD_LIBRARY_WEB", "externalAdID": ad_id, "identityConfidence": "STABLE",
            "mediaType": "VIDEO" if video else "IMAGE" if image else "UNKNOWN",
            "mediaUrl": video or image, "sourceUrl": self.url, "snapshotUrl": detail,
            "platformData": {"captureMethod": self.origin, "requestRegion": self.region, "ad": raw},
            **fields,
        }

    def result(self, error: str | None = None, message: str | None = None,
               empty_status: JobStatus = JobStatus.FAILED) -> WorkerResult:
        if not error and self.has_more and not self.full:
            error, message = "TIKTOK_MORE_RESULTS", "当前已取得的搜索页仍有下一页"
        items = list(self.items.values())
        status = (JobStatus.PARTIAL_SUCCESS if items else empty_status) if error else JobStatus.SUCCESS
        return WorkerResult(status=status, items=items, fetchedCount=len(items), newCount=len(items),
                            errorCode=error, errorMessage=message)
