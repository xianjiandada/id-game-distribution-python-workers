import sqlite3
from dataclasses import replace

import httpx
import pytest

from app import youtube_meta as meta


@pytest.mark.asyncio
async def test_enrich_replaces_placeholder_and_caches_verified_zero(monkeypatch, tmp_path):
    path = str(tmp_path / "worker.db")
    monkeypatch.setattr(meta, "settings", replace(meta.settings, database_path=path))
    video = "kuSTA4K0FaU"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE youtube_video_meta(video_id TEXT PRIMARY KEY, title TEXT, view_count INTEGER,"
                   " language TEXT, updated_at TEXT)")
        db.execute("INSERT INTO youtube_video_meta VALUES(?, 'Old title', 0, 'en', CURRENT_TIMESTAMP)", (video,))
    assert meta._lookup([video], path)[video]["view_count"] is None
    calls = []

    def watch(*args, **kwargs):
        calls.append(args)
        return httpx.Response(200, text='var ytInitialPlayerResponse = {"videoDetails":'
                              '{"videoId":"kuSTA4K0FaU","title":"Title","viewCount":"0","lengthSeconds":"15"}};')

    monkeypatch.setattr(meta.httpx, "get", watch)
    for _ in range(2):
        items = await meta.enrich([{"youtubeVideoId": video}, {"youtubeVideoId": video}])
        assert all(item["youtubeViewCount"] == 0 and item["youtubeDurationSeconds"] == 15 for item in items)
        assert all(item["language"] == "en" for item in items)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_only_title_keeps_unknown_stats_null(monkeypatch, tmp_path):
    monkeypatch.setattr(meta, "settings", replace(meta.settings, database_path=str(tmp_path / "worker.db")))
    monkeypatch.setattr(meta, "_fetch_sync", lambda video: {"title": "Available title"})
    item, = await meta.enrich([{"youtubeVideoId": "kuSTA4K0FaU"}])
    assert item["youtubeViewCount"] is None and item["youtubeDurationSeconds"] is None
