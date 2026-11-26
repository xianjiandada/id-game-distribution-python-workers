from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.models import JobStatus
from app.workers.google_transparency.worker import GoogleTransparencyWorker


@pytest.mark.asyncio
@pytest.mark.parametrize("status,expected_code", [(200, "GOOGLE_SEARCH_INPUT_UNAVAILABLE"), (429, "GOOGLE_RATE_LIMITED")])
async def test_company_search_can_open_during_preview_cooldown(monkeypatch, status, expected_code):
    from app.workers.google_transparency import worker as module

    search_host = "adstransparency.google.com"
    cooldowns = {"displayads-formats.googleusercontent.com": module.time.monotonic() + 600}
    monkeypatch.setattr(module, "_cooldowns", cooldowns)
    monkeypatch.setattr(module, "_consecutive_limits", {})
    response = SimpleNamespace(url=f"https://{search_host}/", status=status, ok=status == 200,
                               headers={"retry-after": "120"})
    page = MagicMock()
    page.goto = AsyncMock(return_value=response)
    page.wait_for_timeout = AsyncMock()
    page.locator.return_value.count = AsyncMock(return_value=0)
    browser = AsyncMock()
    browser.new_context.return_value.new_page.return_value = page
    manager = MagicMock()
    manager.__aenter__.return_value.chromium.launch = AsyncMock(return_value=browser)
    monkeypatch.setattr(module, "async_playwright", lambda: manager)

    worker = GoogleTransparencyWorker()
    payload = {"keyword": "Test company"}
    result = await worker.run(payload)
    assert result.errorCode == expected_code
    page.goto.assert_awaited_once()
    if status == 429:
        assert cooldowns[search_host] > module.time.monotonic() + 115
        assert (await worker.run(payload)).errorCode == expected_code
        page.goto.assert_awaited_once()
    else:
        assert search_host not in cooldowns


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


@pytest.mark.asyncio
async def test_daily_scan_skips_known_and_old_creatives_before_preview(monkeypatch):
    worker = GoogleTransparencyWorker()
    recent = {"1": "1900000000"}
    old = {"1": "1600000000"}
    rpc = AsyncMock(side_effect=[
        {"1": [{"2": "CR1", "7": recent}, {"2": "CR2", "7": old}], "2": "more"},
        {"1": [{"2": "CR3", "7": recent}], "2": "older"},
        {"1": [{"2": "CR4", "7": old}], "2": "stop"},
    ])
    preview = AsyncMock(return_value=([], {"copyText": "Recover files"}, {}))
    monkeypatch.setattr(worker, "_atc_rpc", rpc)
    monkeypatch.setattr(worker, "_atc_preview_urls", preview)
    result = await worker.run({"keyword": "AR09354817122947039233", "windowStart": "2026-11-25T00:00:00+08:00",
                               "knownCreativeIDs": ["CR1"]})
    assert [item["externalAdID"] for item in result.items] == ["CR3"]
    assert rpc.await_count == 3 and preview.await_count == 1


@pytest.mark.asyncio
async def test_candidate_budget_applies_even_if_every_row_is_known(monkeypatch):
    worker = GoogleTransparencyWorker()
    monkeypatch.setattr(worker, "_ATC_MAX_CREATIVES", 2)
    rpc = AsyncMock(return_value={"1": [{"2": "CR1"}, {"2": "CR2"}, {"2": "CR3"}], "2": "more"})
    preview = AsyncMock()
    monkeypatch.setattr(worker, "_atc_rpc", rpc)
    monkeypatch.setattr(worker, "_atc_preview_urls", preview)
    result = await worker.run({"keyword": "AR09354817122947039233", "knownCreativeIDs": ["CR1", "CR2"]})
    assert result.status == JobStatus.SUCCESS and result.items == []
    rpc.assert_awaited_once()
    preview.assert_not_awaited()


@pytest.mark.asyncio
async def test_detail_rate_limit_preserves_already_extracted_copy(monkeypatch):
    from app.workers.google_transparency.worker import _RateLimited
    worker = GoogleTransparencyWorker()
    monkeypatch.setattr(worker, "_atc_rpc", AsyncMock(return_value={"1": [{"2": "CR9"}]}))
    monkeypatch.setattr(worker, "_atc_preview_urls", AsyncMock(return_value=([], {"copyText": "Recover files"}, None)))
    monkeypatch.setattr(worker, "_atc_detail", AsyncMock(side_effect=_RateLimited("HTTP 429")))
    result = await worker.run({"keyword": "AR09354817122947039233"})
    assert result.status == JobStatus.PARTIAL_SUCCESS and result.restrictionType == "RATE_LIMITED"
    assert result.items[0]["copyText"] == "Recover files"


@pytest.mark.parametrize("markup,expected", [
    ("<VAST><AdTitle>Recover photos</AdTitle><Description><![CDATA[Restore <b>lost</b> photos]]>"
     "</Description></VAST>", "Recover photos\nRestore lost photos"),
    (r'<div data-asoch-targets=\"ochHeadline\">Save <span>files</span></div>'
     '<span data-asoch-targets="ochTitle">Save files</span>'
     '<div data-asoch-targets="ochDescription">Fast<br>recovery</div>', "Save files\nFast recovery"),
    ('<div data-asoch-targets="other">Tracking</div><Impression>https://pixel.test</Impression>'
     '<script><Description>internal</Description></script>'
     '<Description>https://pixel.test</Description>', None),
])
def test_explicit_preview_copy(markup, expected):
    assert GoogleTransparencyWorker._parse_atc_preview(markup)["copyText"] == expected


@pytest.mark.asyncio
async def test_rpc_summary_counts_requests_and_outcomes(monkeypatch, caplog):
    from app.workers.google_transparency import worker as module
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def slot(host):
        module._check_cooldown(host)
        yield

    worker = GoogleTransparencyWorker()
    rows = [{"2": str(i), "3": {"1": {"4": f"https://preview.test/{i}"}}} for i in range(7)]
    rows[1]["7"] = {"1": "1000"}
    monkeypatch.setattr(module, "_cooldowns", {})
    monkeypatch.setattr(module, "_consecutive_limits", {})
    monkeypatch.setattr(module, "_preview_slot", slot)
    monkeypatch.setattr(worker, "_atc_rpc", AsyncMock(return_value={"1": rows}))
    monkeypatch.setattr(worker, "_atc_detail", AsyncMock(return_value={}))
    get = AsyncMock(side_effect=[
        httpx.Response(404), httpx.Response(200, text="opaque"), httpx.Response(200, text="<AdTitle>Keep me</AdTitle>"),
        httpx.Response(429),
    ])
    monkeypatch.setattr(httpx.AsyncClient, "get", get)
    payload = {"keyword": "AR09354817122947039233", "knownCreativeIDs": ["0"], "windowStart": "2026-11-25"}
    with caplog.at_level("INFO", logger="uvicorn.error"):
        result = await worker.run(payload)
        await worker.run(payload)
    assert result.items[0]["format"] == "TEXT"
    logs = [msg for msg in caplog.messages if "抓取汇总" in msg]
    assert len(logs) == 2
    assert "候选=6 窗口外=1 已入库跳过=1 预览请求=4 预览失败=2 预览成功但无内容=1" in logs[0]
    assert "无内容跳过=2 HTTP429=1 冷却拦截=0 返回=1" in logs[0]
    assert "预览请求=0" in logs[1] and "HTTP429=0 冷却拦截=1 返回=0" in logs[1]


@pytest.mark.asyncio
async def test_web_preview_counts_are_isolated_per_run(monkeypatch, caplog):
    import asyncio
    from app.models import WorkerResult

    worker = GoogleTransparencyWorker()
    async def search(payload, keyword):
        response = SimpleNamespace(ok=True, status=200, text=AsyncMock(return_value=payload["markup"]))
        page = MagicMock()
        page.context.request.get = AsyncMock(return_value=response)
        preview = await worker._extract_script_preview(page, f"https://{keyword}.test/ad", 1000)
        return WorkerResult(items=[preview] if preview.get("copyText") else [])

    monkeypatch.setattr(worker, "_search_web", search)
    with caplog.at_level("INFO", logger="uvicorn.error"):
        await asyncio.gather(worker.run({"keyword": "empty", "markup": ""}),
                             worker.run({"keyword": "full", "markup": '<span data-asoch-targets="ochAppName">App</span>'}))
    logs = [msg for msg in caplog.messages if "抓取汇总" in msg]
    assert len(logs) == 2
    for msg in logs:
        assert "route=WEB" in msg and "预览请求=1" in msg
        empty = "keyword=empty" in msg
        assert f"预览成功但无内容={int(empty)}" in msg and f"返回={int(not empty)}" in msg
