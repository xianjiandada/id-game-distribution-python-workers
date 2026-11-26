"""Meta 公开广告卡片与地区透明度数据的字段映射。"""

import json
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

from ...creative_fields import infer_language


def index_ads(document, ads):
    """列表初次加载与翻页响应使用同一种广告快照，按广告库 ID 匹配。"""
    if isinstance(document, list):
        for child in document:
            index_ads(child, ads)
    elif isinstance(document, dict):
        if document.get("ad_archive_id") and isinstance(document.get("snapshot"), dict):
            ads[str(document["ad_archive_id"])] = document
        else:
            for child in document.values():
                index_ads(child, ads)


def json_documents(text):
    for line in text.splitlines():
        try:
            yield json.loads(line.removeprefix("for (;;);"))
        except ValueError:
            continue


def landing_page(url):
    if not url:
        return None, None
    try:
        parts = urlsplit(url)
        if parts.hostname in {"l.facebook.com", "lm.facebook.com"} and parts.path == "/l.php":
            url = parse_qs(parts.query).get("u", [""])[0]
            parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            return None, None
        if parts.hostname == "play.google.com" and parts.path == "/store/apps/details":
            return url, parse_qs(parts.query).get("id", [None])[0]
        if parts.hostname == "apps.apple.com":
            match = re.search(r"/id(\d+)(?:/|$)", parts.path)
            if match:
                return url, match.group(1)
    except ValueError:
        return None, None
    return url, None


def _text(value):
    if isinstance(value, dict):
        value = value.get("text")
    if isinstance(value, str) and value.strip() and "{{" not in value:
        return value.strip()
    return None


def enrich_card(item, ad):
    snapshot = ad.get("snapshot") or {}
    meta = item["platformData"]
    for field, value in {
        "advertiserID": ad.get("page_id") or snapshot.get("page_id"),
        "advertiserName": snapshot.get("page_name"), "advertiserUrl": snapshot.get("page_profile_uri"),
        "copyText": _text(snapshot.get("body")), "title": _text(snapshot.get("title")),
        "description": _text(snapshot.get("link_description")),
        "platforms": ad.get("publisher_platform"),
    }.items():
        if value:
            item[field] = value
    url, app_id = landing_page(snapshot.get("link_url") or item.get("landingUrl"))
    if url:
        item["landingUrl"] = url
    item["language"] = infer_language(item.get("copyText")) or infer_language(item.get("title"))
    regions = ad.get("targeted_or_reached_countries") or []
    item["regions"] = [v for v in regions if isinstance(v, str) and re.fullmatch("[A-Z]{2}", v)]
    for key, field in (("start_date", "deliveryStartTime"), ("end_date", "deliveryStopTime")):
        timestamp = ad.get(key)
        if isinstance(timestamp, int) and timestamp > 0 and (key == "start_date" or ad.get("is_active") is False):
            item[field] = datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
    data = {
        "isActive": ad.get("is_active"), "ctaType": snapshot.get("cta_type"),
        "ctaText": snapshot.get("cta_text"), "displayFormat": snapshot.get("display_format"),
        "landingAppID": app_id, "pageCategories": snapshot.get("page_categories"),
        "pageLikes": snapshot.get("page_like_count"), "variantCount": ad.get("collation_count"),
        "advertiserUrl": item.get("advertiserUrl"), "landingUrl": url,
        "spend": ad.get("spend"), "currency": ad.get("currency"),
        "reachEstimate": ad.get("reach_estimate"),
        "impressions": (ad.get("impressions_with_index") or {}).get("impressions_text"),
    }
    meta.update({key: value for key, value in data.items() if value not in (None, "", [])})


def apply_transparency(item, details):
    transparency = {}
    countries = set(item.get("regions", []))
    for area, key, reach_key in (("EU", "eu_transparency", "eu_total_reach"), ("UK", "uk_transparency", "total_reach")):
        source = details.get(key) or {}
        if not source:
            continue
        reach = source.get(reach_key)
        breakdown = source.get("age_country_gender_reach_breakdown") or []
        for row in breakdown:
            country = row.get("country")
            if country and re.fullmatch("[A-Z]{2}", country) and any(
                isinstance(group.get(sex), int) and group[sex] > 0
                for group in row.get("age_gender_breakdowns") or [] for sex in ("male", "female", "unknown")
            ):
                countries.add(country)
        fields = {"reach": reach, "ageAudience": source.get("age_audience"),
                  "genderAudience": source.get("gender_audience"), "locationAudience": source.get("location_audience"),
                  "reachBreakdown": breakdown}
        disclosed = {k: v for k, v in fields.items() if v not in (None, "", [], {})}
        if disclosed:
            transparency[area] = disclosed
    if transparency:
        item["platformData"]["transparency"] = transparency
        item["regions"] = sorted(countries)


def apply_sponsors(item, details):
    """保留每个披露地区及其受益方和付费方，不跨地区合并主体。"""
    context = details.get("verified_voice_context") or {}
    info = context.get("ad_library_all_geo_fin_serv_info") or {}
    disclosures = []
    for entry in info.get("finserv_data") or []:
        row = {"region": entry.get("geo")}
        for role in ("beneficiary", "payer"):
            party = entry.get(role + "_info") or {}
            if party.get("name"):
                row[role] = {key: party[key] for key in ("name", "location", "website") if party.get(key)}
        if row.get("payer") or row.get("beneficiary"):
            disclosures.append(row)
    for entry in (details.get("aaa_info") or {}).get("payer_beneficiary_data") or []:
        row = {role: {"name": entry[role]} for role in ("beneficiary", "payer") if entry.get(role)}
        if row:
            disclosures.append(dict(row, region="EU"))
    if disclosures:
        item["platformData"]["sponsorDisclosures"] = disclosures


def play_listing(html, package):
    for script in re.findall(r'<script\b[^>]*\btype=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.S | re.I):
        for data in json_documents(script.strip()):
            if not isinstance(data, dict) or data.get("@type") != "SoftwareApplication":
                continue
            canonical = urlsplit(data.get("url") or "")
            if canonical.hostname != "play.google.com" or parse_qs(canonical.query).get("id") != [package]:
                continue
            author = data.get("author") or {}
            return {"productName": data.get("name"), "developerName": author.get("name"),
                    "category": data.get("applicationCategory"), "packageName": package,
                    "sourceUrl": "https://play.google.com/store/apps/details?id=" + package}
    return None
