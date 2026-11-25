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
        r"(?:^|[^a-z])(english|japanese|korean|spanish|portuguese|french|german|russian|"
        r"英语|英文|西班牙语|西语|葡萄牙语|日语|韩语|法语|德语|俄语|中文|汉语)(?:$|[^a-z])",
        value,
        re.IGNORECASE,
    )
    if named:
        return {
            "english": "en",
            "英语": "en",
            "英文": "en",
            "japanese": "ja",
            "日语": "ja",
            "korean": "ko",
            "韩语": "ko",
            "spanish": "es",
            "西班牙语": "es",
            "西语": "es",
            "portuguese": "pt",
            "葡萄牙语": "pt",
            "french": "fr",
            "法语": "fr",
            "german": "de",
            "德语": "de",
            "russian": "ru",
            "俄语": "ru",
            "中文": "zh",
            "汉语": "zh",
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
    if re.search(r"[áéíóúñ¿¡]", value, re.IGNORECASE):
        return "es"
    if re.search(r"[ãõçà]", value, re.IGNORECASE):
        return "pt"
    if re.search(
        r"\b(archivos|fotos|datos|eliminados|eliminadas|recuperar|restaurar|"
        r"limpiar|limpieza|espacio|almacenamiento|basura)\b",
        value,
        re.IGNORECASE,
    ):
        return "es"
    if re.search(
        r"\b(arquivos|limpeza|espaço|espaco|celular|memória|memoria|"
        r"liberar|limpador|recuperar|restaurar)\b",
        value,
        re.IGNORECASE,
    ):
        return "pt"
    # 参考 ATC 项目仅对成句文案兜底英文，避免把产品名或控件误判为英语。
    if (len(re.findall(r"[A-Za-z]{3,}", value)) >= 2
            and not re.search(r"\b(install|sponsored|learn more|downloads)\b", value, re.IGNORECASE)):
        return "en"
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
