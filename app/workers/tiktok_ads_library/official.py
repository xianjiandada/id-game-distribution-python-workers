"""通过已获 research.adlib.basic 授权的应用查询 TikTok 商业内容库。"""

from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import httpx

from ...models import JobStatus, WorkerResult
from .search_response import SearchResults, cdn_url


QUERY_URL = "https://open.tiktokapis.com/v2/research/adlib/ad/query/"
FIELDS = ("ad.id,ad.first_shown_date,ad.last_shown_date,ad.videos,ad.image_urls,ad.reach,"
          "advertiser.business_id,advertiser.business_name")


def published_date(value: object) -> str | None:
    if value is None:
        return None
    return datetime.strptime(str(value), "%Y%m%d").strftime("%Y-%m-%dT00:00:00Z")


async def query_ads(client: httpx.AsyncClient, token: str, payload: dict, results: SearchResults) -> WorkerResult:
    """按任务窗口和地区查询，沿官方 search_id 分页并保留已成功的结果。"""
    query = parse_qs(urlsplit(results.url).query)
    dates = {}
    for bound, window, param in (("min", "windowStart", "start_time"), ("max", "windowEnd", "end_time")):
        date = (datetime.fromisoformat(payload[window].replace("Z", "+00:00")) if payload.get(window)
                else datetime.fromtimestamp(int(query[param][0]) / 1000, timezone.utc))
        dates[bound] = date.strftime("%Y%m%d")
    filters = {"ad_published_date_range": dates}
    countries = payload.get("countryCodes") or [results.region]
    if all(str(country).upper() != "ALL" for country in countries):
        filters["country_code_list"] = [str(country).upper() for country in countries]
    status = str(payload.get("activeStatus") or "ALL").upper()
    if status in {"ACTIVE", "INACTIVE"}:
        filters["ad_status"] = status
    media = str(payload.get("mediaType") or "ALL").upper()
    if media in {"IMAGE", "IMAGES", "VIDEO", "TEXT"}:
        filters["ad_type"] = "IMAGES" if media == "IMAGE" else media
    keyword = str(payload.get("keyword") or payload.get("advertiserName") or "").strip()
    body = {"filters": filters, "max_count": 10}
    if keyword:
        body["search_term"] = keyword
    if payload.get("advertiserID"):
        return results.result("TIKTOK_UNSUPPORTED_FILTER", "官方广告关键词查询未接入按广告主 ID 筛选")
    fetch_details = results.region.lower() == "all"
    cursors = set()
    while True:
        if results.limit:
            body["max_count"] = min(10, results.limit - len(results.items))
        try:
            response = await client.post(QUERY_URL, params={"fields": FIELDS}, json=body,
                                         headers={"Authorization": f"Bearer {token}"})
            if response.status_code in {401, 403, 429}:
                return results.result("TIKTOK_OFFICIAL_ACCESS", f"官方广告查询返回 HTTP {response.status_code}",
                                      JobStatus.WAITING_MANUAL)
            response.raise_for_status()
            page = response.json()
            if page["error"]["code"] != "ok":
                return results.result("TIKTOK_OFFICIAL_ERROR", f"官方查询返回 {page['error']['code']}")
            data = page["data"]
            if not isinstance(data["ads"], list) or data["has_more"] not in (True, False, "true", "false"):
                raise ValueError("官方查询返回的列表或分页状态无效")
            results.has_more = data["has_more"] in (True, "true")
            for row in data["ads"]:
                ad = row["ad"]
                ad_id = str(ad["id"])
                if not ad_id.isascii() or not ad_id.isdecimal():
                    raise ValueError("无效的广告 ID")
                if ad_id in results.items:
                    continue
                advertiser = row.get("advertiser") or {}
                videos = (cdn_url(v.get("url")) for v in ad.get("videos") or [] if isinstance(v, dict))
                images = (cdn_url(v) for v in ad.get("image_urls") or [])
                if fetch_details and not (ad.get("reach") or {}).get("unique_users_seen_by_country"):
                    # 全球搜索只返回总触达量时，详情可能提供各国触达明细。
                    try:
                        detail = await client.post(
                            "https://open.tiktokapis.com/v2/research/adlib/ad/detail/",
                            params={"fields": "ad.id,ad.reach"}, json={"ad_id": int(ad_id)},
                            headers={"Authorization": f"Bearer {token}"})
                        detail.raise_for_status()
                        disclosed = detail.json().get("data", {}).get("ad", {})
                        if str(disclosed.get("id")) == ad_id and disclosed.get("reach"):
                            ad["reach"] = disclosed["reach"]
                        elif detail.json().get("error", {}).get("code") != "ok":
                            fetch_details = False
                    except (httpx.HTTPError, ValueError):
                        fetch_details = False
                results.save(ad_id, row, next(filter(None, videos), None), next(filter(None, images), None),
                             advertiserID=advertiser.get("business_id"), advertiserName=advertiser.get("business_name"),
                             deliveryStartTime=published_date(ad.get("first_shown_date")),
                             deliveryStopTime=published_date(ad.get("last_shown_date")))
                if results.full:
                    break
            if results.full or not results.has_more:
                break
            cursor = data.get("search_id")
            if not cursor or cursor in cursors:
                break
            cursors.add(cursor)
            body["search_id"] = cursor
        except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError) as exc:
            return results.result("TIKTOK_OFFICIAL_ERROR", f"官方广告查询失败：{str(exc)[:250]}")
    return results.result()
