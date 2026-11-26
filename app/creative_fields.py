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
    for script, language in ((r"[\u0a95-\u0ab9]", "gu"), (r"[\u0c15-\u0c39]", "te"),
                             (r"[\u0995-\u09b9]", "bn")):
        if len(re.findall(script, value)) >= 3:
            return language
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
    if re.search(r"[ãõ]|\b(segurança|antivírus)\b", value, re.IGNORECASE):
        return "pt"
    if re.search(r"[ñ¿¡]", value, re.IGNORECASE):
        return "es"
    if re.search(
        r"\b(archivos|datos|eliminados|eliminadas|"
        r"limpiar|limpieza|espacio|almacenamiento|basura)\b",
        value,
        re.IGNORECASE,
    ):
        return "es"
    if re.search(
        r"\b(arquivos|limpeza|espaço|espaco|memória|limpador)\b",
        value,
        re.IGNORECASE,
    ):
        return "pt"
    if re.search(r"\b(fichiers|supprimés|récupérer|nettoyage)\b", value, re.IGNORECASE):
        return "fr"
    if (re.search(r"\b(the|with|your|from|and|files?|photos?|recover|recovery|deleted|cleaner|clean|"
                  r"phone|tracker|security|control|volume|remove|sweep|storage|backup|restore)\b", value, re.I)
            and len(set(re.findall(r"\b[a-z]{3,}\b", value.lower()))) >= 2
            and not re.search(r"\b(install|sponsored|learn more|downloads)\b", value, re.IGNORECASE)):
        return "en"
    return None


def infer_google_language(title: str | None, ad_text: str | None) -> str | None:
    """广告文件名中的语言码可用作补充，普通标题继续按文案判断。"""
    title = title or ""
    if re.search(r"\d", title) and re.search(r"[-_.]|\b\d+x\d+\b", title, re.I):
        code = re.search(r"(?:^|[-_.\s])(en|es|pt|th|vi|id|ar|ja|jp|ko|zh|cn|tw|fr|de|it|ru|tr|hi)"
                         r"(?=$|[-_.\s])", title, re.I)
        if code:
            value = code.group(1).lower()
            return {"cn": "zh", "tw": "zh", "jp": "ja"}.get(value, value)
        if "_" in title or re.search(r"\.(mp4|mov|webm)$", title, re.I):
            return infer_language(ad_text)
    return infer_language(ad_text) or infer_language(title)


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
