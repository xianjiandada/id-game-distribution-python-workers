"""国家/地区可选参数测试，确保空值按全部地区查询。"""

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


def test_google_country_defaults_to_anywhere() -> None:
    """Google 未填写国家/地区时，应查询全部地区。"""
    assert GoogleTransparencyWorker._resolve_region({}) == "ANYWHERE"


def test_google_uses_first_selected_region() -> None:
    """Google 只接受一个地区时，使用输入列表的第一个代码。"""
    assert GoogleTransparencyWorker._resolve_region({"countryCodes": ["SG", "HK"]}) == "SG"


def test_google_all_alias_maps_to_anywhere() -> None:
    """前端的“全部地区”标记 ALL 应转换为 Google 的 ANYWHERE。"""
    assert GoogleTransparencyWorker._resolve_region({"countryCodes": ["ALL"]}) == "ANYWHERE"
