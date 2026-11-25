"""素材标准字段推断。"""

import re


def infer_language(text: str | None) -> str | None:
    """仅在文案中存在明确语言信号时返回语言代码。"""
    value = str(text or "")
    explicit = re.search(
        r"(?:lang(?:uage)?|locale)[\s:=_-]*([a-z]{2}(?:-[A-Z]{2})?)\b",
        value,
        re.IGNORECASE,
    )
    if explicit:
        return explicit.group(1).lower()
    named = re.search(
        r"\b(english|japanese|korean|spanish|portuguese|french|german|russian)\b",
        value,
        re.IGNORECASE,
    )
    if named:
        return {
            "english": "en",
            "japanese": "ja",
            "korean": "ko",
            "spanish": "es",
            "portuguese": "pt",
            "french": "fr",
            "german": "de",
            "russian": "ru",
        }[named.group(1).lower()]
    if re.search(r"[\u3040-\u30ff]", value):
        return "ja"
    if re.search(r"[\uac00-\ud7af]", value):
        return "ko"
    if re.search(r"[\u4e00-\u9fff]", value):
        return "zh"
    if re.search(r"[\u0400-\u04ff]", value):
        return "ru"
    if re.search(r"[\u0600-\u06ff]", value):
        return "ar"
    # 拉丁字母可能只是品牌名、产品名或按钮文案，无法据此确认英语。
    return None


def infer_function(text: str | None) -> str | None:
    """按明确的广告文案关键词归类素材用途，无法区分时留空。"""
    value = str(text or "").lower()
    recovery_terms = (
        "file recovery",
        "data recovery",
        "photo recovery",
        "video recovery",
        "recover",
        "restore",
        "undelete",
        "文件恢复",
        "恢复",
        "復元",
        "復旧",
        "データ復元",
    )
    cleaner_terms = (
        "cleaner",
        "cleanup",
        "cleaning",
        "clean",
        "junk remover",
        "storage cleaner",
        "清理",
        "垃圾",
        "释放空间",
        "クリーナー",
        "クリーンアップ",
    )
    recovery_hits = sum(term in value for term in recovery_terms)
    cleaner_hits = sum(term in value for term in cleaner_terms)
    if recovery_hits > cleaner_hits and recovery_hits:
        return "文件恢复"
    if cleaner_hits > recovery_hits and cleaner_hits:
        return "清理"
    return None
