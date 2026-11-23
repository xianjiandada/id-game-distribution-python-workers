import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.workers.tiktok_ads_library.official import query_ads
from app.workers.tiktok_ads_library.search_response import SearchResults
from app.workers.tiktok_ads_library.worker import TikTokAdsLibraryWorker


def test_tiktok_url_keeps_keyword_and_region() -> None:
    url = TikTokAdsLibraryWorker._build_url({"keyword": "Puzzle Game", "countryCodes": ["US"]})
    assert "library.tiktok.com/ads" in url
    fields = parse_qs(urlsplit(url).query, keep_blank_values=True)
    assert fields["adv_name"] == ["Puzzle Game"]
    assert fields["region"] == ["us"]
    assert fields["adv_biz_ids"] == [""]
    assert fields["ad_status"] == ["1"]
    assert "query" not in fields
    start = datetime.fromtimestamp(int(fields["start_time"][0]) / 1000, timezone.utc)
    end = datetime.fromtimestamp(int(fields["end_time"][0]) / 1000, timezone.utc)
    assert 28 <= (end - start).days <= 31


def test_tiktok_keeps_shared_search_url_verbatim() -> None:
    shared = (
        "https://library.tiktok.com/ads?region=all&start_time=1792722917724&end_time=1795401317724"
        "&adv_name=File%20Recovery&adv_biz_ids=&query_type=1&sort_type=last_shown_date,desc"
        "&ad_type=0&ad_status=1&ages=all&ad_reach=all&gender=ALL"
    )
    assert TikTokAdsLibraryWorker._build_url({"url": shared}) == shared


IMAGE = "https://p16-common-sign.tiktokcdn.com/photo.jpeg"
VIDEO = "https://v16.tiktokcdn.com/ad.mp4"


@pytest.mark.asyncio
async def test_web_search_uses_actual_ads_and_media():
    row = {"id": "1881112931918962", "name": "广告主", "title": "File Recovery", "image_urls": [IMAGE],
           "first_shown_date": 1793923200000, "rejection_info": {"affected_regions": ["GB"]}, "videos": []}
    body = {"code": 0, "has_more": False, "total": 5000, "data": [
        row, row, {"id": "2", "videos": [{"video_url": VIDEO, "cover_img": IMAGE}]},
        {"id": "3", "videos": [{"cover_img": IMAGE}], "image_urls": ["https://localhost/image"]},
    ]}
    first = AsyncMock(ok=True)
    first.json.return_value = body
    result = await TikTokAdsLibraryWorker._read_pages(AsyncMock(), first, asyncio.Queue(),
                                                      SearchResults(TikTokAdsLibraryWorker.BASE_URL, None, "web"), 1)
    assert result.status == "SUCCESS" and result.fetchedCount == 3
    photo, video, unavailable = result.items
    assert photo["mediaUrl"] == IMAGE and photo["mediaType"] == "IMAGE"
    assert photo["deliveryStartTime"] == "2026-11-06T00:00:00Z"
    assert "regions" not in photo and photo["platformData"]["ad"] == row
    assert video["mediaType"] == "VIDEO" and video["mediaUrl"] == VIDEO
    assert unavailable["mediaType"] == "UNKNOWN" and unavailable["mediaUrl"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("body,status", [
    ({"code": 0, "has_more": False, "data": []}, "SUCCESS"),
    ({"code": 1, "has_more": False, "data": []}, "FAILED"),
    ({"code": 0, "has_more": False, "data": [{}]}, "FAILED"),
    ({"code": 0, "has_more": True, "data": [{"id": "1"}]}, "PARTIAL_SUCCESS"),
])
async def test_web_search_status(body, status):
    first = AsyncMock(ok=True)
    first.json.return_value = body
    result = await TikTokAdsLibraryWorker._read_pages(AsyncMock(), first, asyncio.Queue(),
                                                      SearchResults(TikTokAdsLibraryWorker.BASE_URL, None, "web"), 1)
    assert result.status == status


@pytest.mark.asyncio
async def test_web_keeps_first_page_when_next_request_is_busy():
    worker = TikTokAdsLibraryWorker()
    results = SearchResults(worker.BASE_URL, None, "web")
    queue = asyncio.Queue()
    first = AsyncMock(ok=True)
    first.json.return_value = {"code": 0, "data": [{"id": "123"}], "has_more": True}
    queue.put_nowait(AsyncMock(ok=False, status=421))
    result = await worker._read_pages(AsyncMock(), first, queue, results, 1000)
    assert result.status == "PARTIAL_SUCCESS" and result.fetchedCount == 1
    assert result.restrictionType == "HTTP_421"


@pytest.mark.asyncio
async def test_official_query_filters_pagination_and_real_media_fields():
    payload = {"keyword": "File Recovery", "countryCodes": ["DE", "FR"], "mediaType": "VIDEO"}
    results = SearchResults(TikTokAdsLibraryWorker._build_url(payload), 2, "official_api")
    calls = []
    def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        index = len(calls)
        assert request.headers["Authorization"] == "Bearer test"
        return httpx.Response(200, json={"error": {"code": "ok"}, "data": {
            "ads": [{"ad": {"id": index, "videos": [{"url": VIDEO}], "first_shown_date": 20261101},
                     "advertiser": {"business_name": "Example"}}],
            "has_more": True, "search_id": str(index),
        }})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await query_ads(client, "test", payload, results)
    assert result.status == "SUCCESS" and result.fetchedCount == 2
    assert calls[0]["search_term"] == "File Recovery"
    assert calls[0]["filters"]["country_code_list"] == ["DE", "FR"]
    assert calls[1]["max_count"] == 1 and calls[1]["search_id"] == "1"
    assert result.items[0]["mediaUrl"] == VIDEO and result.items[0]["deliveryStartTime"].startswith("2026-11-01")
