"""国家/地区可选参数测试，确保空值按全部地区查询。"""

import pytest
from unittest.mock import AsyncMock

from app.models import JobStatus, WorkerResult
from app.workers.countries import collect_countries
from app.workers.meta_library.worker import MetaLibraryWorker
from app.workers.google_transparency.worker import GoogleTransparencyWorker


def test_meta_country_defaults_to_all_regions() -> None:
    """未填写国家代码时，Meta URL 应使用 ALL。"""
    url = MetaLibraryWorker._build_url({"keyword": "螺丝"})
    assert "country=ALL" in url


def test_meta_empty_country_list_defaults_to_all_regions() -> None:
    """Java 传入空数组时，也必须按全部地区查询。"""
    url = MetaLibraryWorker._build_url({"keyword": "螺丝", "countryCodes": []})
    assert "country=ALL" in url


def test_meta_url_uses_platform_status_parameter() -> None:
    url = MetaLibraryWorker._build_url({"activeStatus": "INACTIVE", "mediaType": "VIDEO"})
    assert "active_status=inactive" in url
    assert "media_type=video" in url


def test_google_country_defaults_to_anywhere() -> None:
    """Google 未填写国家/地区时，应查询全部地区。"""
    assert GoogleTransparencyWorker._resolve_region({}) == "ANYWHERE"


def test_google_accepts_single_region() -> None:
    """每个地区请求单独带入一个国家。"""
    assert GoogleTransparencyWorker._resolve_region({"countryCodes": ["SG"]}) == "SG"


def test_google_all_alias_maps_to_anywhere() -> None:
    """前端的“全部地区”标记 ALL 应转换为 Google 的 ANYWHERE。"""
    assert GoogleTransparencyWorker._resolve_region({"countryCodes": ["ALL"]}) == "ANYWHERE"

@pytest.mark.asyncio
async def test_multicountry_merges_hits_and_keeps_partial_results():
    worker = AsyncMock()
    worker.run.side_effect = [
        WorkerResult(items=[{"externalAdID": "shared"}, {"externalAdID": "jp"}]),
        WorkerResult(items=[{"externalAdID": "us"}, {"externalAdID": "shared"}]),
        WorkerResult(status=JobStatus.WAITING_MANUAL, errorMessage="region unavailable"),
    ]
    result = await collect_countries(worker, {"countryCodes": ["JP", "US", "DE", "JP"], "maxResults": 2}, None, 5)
    assert [call.args[0]["countryCodes"] for call in worker.run.await_args_list] == [["JP"], ["US"], ["DE"]]
    assert result.status == JobStatus.PARTIAL_SUCCESS and "DE" in result.errorMessage
    assert [(i["externalAdID"], i["regions"]) for i in result.items] == [("shared", ["JP", "US"]), ("us", ["US"])]


@pytest.mark.asyncio
async def test_worldwide_retains_only_disclosed_regions():
    worker = AsyncMock()
    worker.run.return_value = WorkerResult(items=[{"externalAdID": "one"}, {"externalAdID": "two", "regions": ["JP"]}])
    result = await collect_countries(worker, {"countryCodes": ["ALL"]}, None, 5)
    assert [i["regions"] for i in result.items] == [[], ["JP"]]
    assert all(i["requestRegions"] == [] for i in result.items)
