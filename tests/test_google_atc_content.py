from unittest.mock import AsyncMock

import httpx
import pytest

from app.models import JobStatus
from app.workers.google_transparency.worker import GoogleTransparencyWorker


@pytest.mark.asyncio
@pytest.mark.parametrize("next_page", [True, False])
async def test_empty_previews_do_not_fill_the_quota(monkeypatch, next_page):
    worker = GoogleTransparencyWorker()
    empty = {"2": "CR1", "3": {"1": {"4": "https://preview.test/empty"}}}
    pages = [{"1": [empty]}]
    if next_page:
        pages[0]["2"] = "next"
        landing = {"2": "CR2", "3": {"1": {"4": "https://preview.test/landing"}}}
        pages.append({"1": [empty, landing]})
    rpc = AsyncMock(side_effect=pages)
    monkeypatch.setattr(worker, "_atc_rpc", rpc)
    detail = AsyncMock(return_value={"17": [{"1": 2724}, {"1": "2484"}, {"1": 2724}, {"1": 999999}]})
    monkeypatch.setattr(worker, "_atc_detail", detail)

    async def get_preview(client, url):
        text = '{"finalUrl":"https://store.test/product"}' if url.endswith("/landing") else ""
        return httpx.Response(200 if text else 404, text=text)

    monkeypatch.setattr(httpx.AsyncClient, "get", get_preview)
    result = await worker.run({"keyword": "AR09354817122947039233", "maxResults": 1})
    assert result.status == JobStatus.SUCCESS
    assert result.fetchedCount == len(result.items) == int(next_page)
    if next_page:
        assert result.items[0]["externalAdID"] == "CR2"
        assert result.items[0]["landingUrl"] == "https://store.test/product"
        assert result.items[0]["regions"] == ["ES", "MX"]
    assert detail.await_count == int(next_page)
    assert rpc.await_count == len(pages)


def test_atc_country_filter_matches_the_public_search():
    worker = GoogleTransparencyWorker()
    assert "8" not in worker._atc_search_payload("AR1")["3"]
    assert worker._atc_search_payload("AR1", region="US")["3"]["8"] == [2840]
    assert worker._atc_regions({}) == []

@pytest.mark.parametrize("markup, key, expected", [
    ('<img src="https://tpc.googlesyndication.com/archive/simgad/123">', "mediaUrl",
     "https://tpc.googlesyndication.com/archive/simgad/123"),
    ("<div>Recover deleted photos</div><script>ignored</script>", "copyText", "Recover deleted photos"),
])
@pytest.mark.asyncio
async def test_inline_preview_without_script(monkeypatch, markup, key, expected):
    worker = GoogleTransparencyWorker()
    monkeypatch.setattr(worker, "_atc_rpc", AsyncMock(return_value={"1": [{"2": "CR42", "3": {"3": {"2": markup}}}]}))
    monkeypatch.setattr(worker, "_atc_detail", AsyncMock(return_value={}))
    result = await worker.run({"keyword": "AR09354817122947039233", "maxResults": 1})
    assert result.items[0][key] == expected


@pytest.mark.asyncio
async def test_rate_limited_preview_is_excluded_and_host_cools_down(monkeypatch):
    worker = GoogleTransparencyWorker()
    creative = {"2": "CR42", "3": {"1": {"4": "https://preview.test/ad.js"}}}
    monkeypatch.setattr(worker, "_atc_rpc", AsyncMock(return_value={"1": [creative]}))
    monkeypatch.setattr(worker, "_atc_detail", AsyncMock(return_value={}))
    from app.workers.google_transparency import worker as module
    monkeypatch.setattr(module, "_cooldowns", {})
    get = AsyncMock(return_value=httpx.Response(429, headers={"Retry-After": "60"}))
    monkeypatch.setattr(httpx.AsyncClient, "get", get)
    result = await worker.run({"keyword": "AR09354817122947039233", "maxResults": 1})
    assert result.items == [] and result.fetchedCount == 0
    assert result.status == JobStatus.FAILED and result.restrictionType == "RATE_LIMITED"
    repeated = await worker.run({"keyword": "AR09354817122947039233", "maxResults": 1})
    assert repeated.items == []
    assert get.await_count == 1


def test_google_snapshot_alone_is_not_content():
    from app.workers.google_transparency.worker import _has_content
    assert not _has_content({"snapshotUrl": "https://adstransparency.google.com/creative/CR1"})
    for field in ("mediaUrl", "previewImageUrl", "youtubeVideoId", "copyText", "landingUrl"):
        assert _has_content({field: "value"})
