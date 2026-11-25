"""素材标准字段解析测试，验证列表展示所依赖的类型和竞品产品名。"""

from app.workers.google_transparency.worker import GoogleTransparencyWorker
from app.workers.meta_library.worker import MetaLibraryWorker


def test_google_preview_extracts_product_and_description() -> None:
    """Google 预览文案应提取产品名，保留描述，并识别可确认的包名。"""
    result = GoogleTransparencyWorker._parse_preview_text(
        "Visit advertiser\nBlock Puzzle Classic\nSOFISH GAMES\n"
        "Learn More\nInstall\ncom.sofish.blockpuzzle\nDownloads"
    )

    assert result["productName"] == "Block Puzzle Classic"
    assert result["packageName"] == "com.sofish.blockpuzzle"
    assert "SOFISH GAMES" in (result["copyText"] or "")


def test_google_preview_ignores_controls_when_no_content() -> None:
    """只有安装控件时不应把控件文本误当成竞品产品名。"""
    result = GoogleTransparencyWorker._parse_preview_text("Install\nLearn More\nSponsored")

    assert result == {"productName": None, "packageName": None, "copyText": None}


def test_google_extracts_youtube_video_id_and_language() -> None:
    assert GoogleTransparencyWorker._extract_youtube_video_id(
        "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg"
    ) == "dQw4w9WgXcQ"
    assert GoogleTransparencyWorker._infer_language("日本語の広告") == "ja"


def test_google_page_labels_do_not_determine_language() -> None:
    """Google 当前界面的中文卡片标签不应被当成素材语言。"""
    assert GoogleTransparencyWorker._infer_language("SoEasy Tile Install Install") is None


def test_google_infers_function_from_ad_copy() -> None:
    assert GoogleTransparencyWorker._infer_function("Recover deleted photos") == "文件恢复"
    assert GoogleTransparencyWorker._infer_function("Phone cleaner and junk remover") == "清理"


def test_meta_common_fields_use_same_language_and_function_rules() -> None:
    assert MetaLibraryWorker._parse_started_date("Started running on Nov 20, 2026") == "2026-11-20"
