"""素材标准字段解析测试，验证列表展示所依赖的类型和竞品产品名。"""

from app.workers.google_transparency.worker import GoogleTransparencyWorker


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
