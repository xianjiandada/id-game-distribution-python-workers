from app.workers.meta_library.public_ad import apply_transparency, enrich_card, index_ads
from app.workers.meta_library.public_ad import apply_sponsors, play_listing
from app.workers.meta_library.worker import MetaLibraryWorker
from urllib.parse import parse_qs, urlsplit


def test_carousel_video_and_copy_from_same_card_replace_dom_thumbnail():
    item = {"platformData": {}, "mediaType": "IMAGE", "mediaUrl": "https://cdn.example/thumb"}
    enrich_card(item, {"snapshot": {"body": "{{product.name}}", "cards": [
        {"original_image_url": "https://cdn.example/image", "title": "First"},
        {"video_hd_url": "https://cdn.example/hd", "video_sd_url": "https://cdn.example/sd",
         "body": {"text": "Play today"}, "title": "Second", "link_url": "https://example.org/play"},
    ]}})
    assert (item["mediaType"], item["mediaUrl"]) == ("VIDEO", "https://cdn.example/hd")
    assert item["platformData"]["mediaUrl"] == item["mediaUrl"]
    assert (item["copyText"], item["title"], item["landingUrl"]) == ("Play today", "Second", "https://example.org/play")


def test_snapshot_image_fallback_preserves_dom_video():
    snapshot = {"images": [{"original_image_url": "https://cdn.example/image"}]}
    item = {"platformData": {}, "mediaType": "VIDEO", "mediaUrl": "https://cdn.example/video"}
    enrich_card(item, {"snapshot": snapshot})
    assert item["mediaUrl"] == "https://cdn.example/video"
    image = {"platformData": {}}
    enrich_card(image, {"snapshot": snapshot})
    assert (image["mediaType"], image["mediaUrl"]) == ("IMAGE", "https://cdn.example/image")


def test_video_uses_matching_carousel_card_for_copy_and_landing_page():
    video = "https://cdn.example/second.mp4"
    item = {"platformData": {}}
    enrich_card(item, {"snapshot": {"videos": [{"video_sd_url": video}], "cards": [
        {"body": "First", "link_url": "https://first.example"},
        {"body": "Second", "video_sd_url": video, "link_url": "https://second.example"},
    ]}})
    assert (item["mediaUrl"], item["copyText"], item["landingUrl"]) == (video, "Second", "https://second.example")


def test_page_search_and_keyword_search_use_different_public_urls():
    def params(keyword):
        return parse_qs(urlsplit(MetaLibraryWorker._build_url({"keyword": keyword})).query)
    assert params("Tile Puzzle Classic")["search_type"] == ["keyword_unordered"]
    page = params("page:744112142119395")
    assert page["view_all_page_id"] == ["744112142119395"] and "q" not in page


def test_store_listing_requires_matching_package_and_preserves_regional_payers():
    html = '<script type="application/ld+json">{"@type":"SoftwareApplication","url":"https://play.google.com/store/apps/details/Tile?id=com.example.tile","name":"Tile","author":{"name":"Studio"}}</script>'
    assert play_listing(html, "com.example.tile")["developerName"] == "Studio"
    assert play_listing(html, "com.other") is None
    item = {"platformData": {}}
    apply_sponsors(item, {
        "verified_voice_context": {"ad_library_all_geo_fin_serv_info": {"finserv_data": [
            {"geo": "SINGAPORE_UNCATEGORIZED", "payer_info": {"name": "Agency"},
             "beneficiary_info": {"name": "Studio", "location": "CN"}}]}},
        "aaa_info": {"payer_beneficiary_data": [{"payer": "Studio", "beneficiary": "Studio"}]},
    })
    rows = item["platformData"]["sponsorDisclosures"]
    assert [(row["region"], row["payer"]["name"]) for row in rows] == [
        ("SINGAPORE_UNCATEGORIZED", "Agency"), ("EU", "Studio")]


def test_meta_uses_disclosed_page_id_and_keeps_active_ads_open():
    ad = {
        "ad_archive_id": "11", "page_id": "42", "is_active": True, "end_date": 1770000000,
        "publisher_platform": ["INSTAGRAM"], "snapshot": {
            "page_profile_uri": "https://www.facebook.com/1234/", "title": "આપના ફોટા",
            "link_url": "https://l.facebook.com/l.php?u=https%3A%2F%2Fplay.google.com%2Fstore%2Fapps%2Fdetails%3Fid%3Dcom.example.app",
            "cta_text": "Install now",
        },
    }
    ads = {}
    index_ads({"data": [ad]}, ads)
    item = {"platformData": {}, "copyText": "આપના ફોટા"}
    enrich_card(item, ads["11"])
    assert item["advertiserID"] == "42" and item["language"] == "gu"
    assert item["platformData"]["landingAppID"] == "com.example.app"
    assert item["platforms"] == ["INSTAGRAM"] and "deliveryStopTime" not in item


def test_meta_keeps_regional_reach_separate_and_excludes_zero_delivery():
    item = {"platformData": {}, "regions": ["AU"]}
    apply_transparency(item, {"uk_transparency": {"total_reach": 0}, "eu_transparency": {
        "eu_total_reach": 9, "age_country_gender_reach_breakdown": [
            {"country": "FR", "age_gender_breakdowns": [{"male": 6, "female": 3}]},
            {"country": "DE", "age_gender_breakdowns": [{"male": 0, "female": None}]},
        ],
    }})
    assert item["regions"] == ["AU", "FR"]
    data = item["platformData"]["transparency"]
    assert data["UK"]["reach"] == 0 and data["EU"]["reach"] == 9
