"""YouTube 视频元数据提取与本地缓存。"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import subprocess
import sys
from urllib.parse import quote
from pathlib import Path
from typing import Any

import httpx

from .config import settings

_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")
_FETCH_LIMIT = 4


def youtube_video_url(video_id: str | None) -> str | None:
    """返回经过校验的 YouTube 观看地址。"""
    value = str(video_id or "").strip()
    return f"https://www.youtube.com/watch?v={value}" if _ID_PATTERN.fullmatch(value) else None


def _ensure_table(connection: sqlite3.Connection) -> None:
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS youtube_video_meta (
            video_id TEXT PRIMARY KEY,
            title TEXT,
            view_count INTEGER,
            views_checked_at TEXT,
            duration_seconds INTEGER,
            language TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    if "views_checked_at" not in {row[1] for row in connection.execute("PRAGMA table_info(youtube_video_meta)")}:
        connection.execute("ALTER TABLE youtube_video_meta ADD COLUMN views_checked_at TEXT")
        connection.execute("UPDATE youtube_video_meta SET view_count=NULL WHERE view_count=0")
    if "duration_seconds" not in {row[1] for row in connection.execute("PRAGMA table_info(youtube_video_meta)")}:
        connection.execute("ALTER TABLE youtube_video_meta ADD COLUMN duration_seconds INTEGER")


def _lookup(video_ids: list[str], database_path: str) -> dict[str, dict[str, Any]]:
    if not video_ids:
        return {}
    if database_path != ":memory:":
        Path(database_path).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database_path, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        _ensure_table(connection)
        placeholders = ",".join("?" for _ in video_ids)
        rows = connection.execute(
            "SELECT video_id, title, view_count, duration_seconds, language, "
            "views_checked_at > datetime('now', '-1 day') AS fresh "
            f"FROM youtube_video_meta WHERE video_id IN ({placeholders})",
            video_ids,
        ).fetchall()
    return {str(row["video_id"]): dict(row) for row in rows}


def _store(video_id: str, metadata: dict[str, Any], database_path: str) -> None:
    if database_path != ":memory:":
        Path(database_path).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database_path, timeout=30) as connection:
        _ensure_table(connection)
        connection.execute(
            "INSERT INTO youtube_video_meta "
            "(video_id, title, view_count, duration_seconds, language, views_checked_at) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(video_id) DO UPDATE SET title=COALESCE(excluded.title, title), "
            "view_count=COALESCE(excluded.view_count, view_count), language=COALESCE(excluded.language, language), "
            "duration_seconds=COALESCE(excluded.duration_seconds, duration_seconds), "
            "views_checked_at=CURRENT_TIMESTAMP, "
            "updated_at=CURRENT_TIMESTAMP",
            (
                video_id,
                metadata.get("title") or None,
                metadata.get("view_count"),
                metadata.get("duration_seconds"),
                metadata.get("language") or None,
            ),
        )


def _fetch_oembed(video_id: str) -> dict[str, Any] | None:
    """通过 YouTube oEmbed 获取公开标题，避免依赖页面脚本和 yt-dlp。"""
    url = "https://www.youtube.com/oembed?format=json&url=" + quote(
        f"https://www.youtube.com/watch?v={video_id}", safe=""
    )
    try:
        response = httpx.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=20,
            follow_redirects=True,
        )
        if response.status_code != 200:
            return None
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    title = str(payload.get("title") or "").strip()
    if not title:
        return None
    return {
        "title": title,
        "author_name": str(payload.get("author_name") or "").strip(),
    }


def _view_count(value: Any) -> int | None:
    digits = str(value)
    return int(digits) if re.fullmatch(r"[0-9]{1,18}", digits) else None


def _fetch_sync(video_id: str) -> dict[str, Any] | None:
    """读取原视频播放器公开的累计播放量，保留仅有标题的部分结果。"""
    try:
        response = httpx.get(youtube_video_url(video_id), headers={"User-Agent": "Mozilla/5.0"},
                             timeout=12, follow_redirects=True)
        if response.status_code == 429:
            return None
        if response.is_success:
            marker = re.search(r"\bytInitialPlayerResponse\s*=\s*", response.text)
            if marker:
                player, _ = json.JSONDecoder().raw_decode(response.text, marker.end())
                details = player.get("videoDetails") if isinstance(player, dict) else None
                if isinstance(details, dict) and details.get("videoId") == video_id:
                    return {"title": details.get("title"), "view_count": _view_count(details.get("viewCount")),
                            "duration_seconds": _view_count(details.get("lengthSeconds")) or None}
    except (httpx.HTTPError, ValueError):
        pass
    oembed = _fetch_oembed(video_id)
    if oembed:
        return oembed
    command = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--dump-single-json",
        "--skip-download",
        "--no-warnings",
        "--no-playlist",
        "--ignore-errors",
        f"https://www.youtube.com/watch?v={video_id}",
    ]
    try:
        process = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=35,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    output = (process.stdout or "").strip()
    if not output:
        return None
    try:
        payload = json.loads(output.splitlines()[-1])
    except (IndexError, ValueError):
        return None
    if not isinstance(payload, dict) or not payload.get("title"):
        return None
    return {
        "title": str(payload.get("title") or "").strip(),
        "view_count": _view_count(payload.get("view_count")),
        "duration_seconds": _view_count(payload.get("duration")) or None,
        "language": str(payload.get("language") or payload.get("original_language") or "").strip(),
    }


async def enrich(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """为素材补充 YouTube URL、标题和播放量，并写入本地缓存。"""
    ids = list(dict.fromkeys(str(item.get("youtubeVideoId") or "").strip() for item in items))
    ids = [video_id for video_id in ids if _ID_PATTERN.fullmatch(video_id)]
    if not ids:
        return items
    cached = await asyncio.to_thread(_lookup, ids, settings.database_path)
    missing = [video_id for video_id in ids if not cached.get(video_id, {}).get("fresh")]
    semaphore = asyncio.Semaphore(_FETCH_LIMIT)

    async def fetch(video_id: str) -> tuple[str, dict[str, Any] | None]:
        async with semaphore:
            return video_id, await asyncio.to_thread(_fetch_sync, video_id)

    fetched = await asyncio.gather(*(fetch(video_id) for video_id in missing))
    for video_id, metadata in fetched:
        await asyncio.to_thread(_store, video_id, metadata or {}, settings.database_path)
        cached.setdefault(video_id, {}).update({k: v for k, v in (metadata or {}).items() if v is not None and v != ""})
    for item in items:
        video_id = str(item.get("youtubeVideoId") or "").strip()
        if not _ID_PATTERN.fullmatch(video_id):
            continue
        item["youtubeVideoUrl"] = youtube_video_url(video_id)
        metadata = cached.get(video_id)
        if metadata:
            item["youtubeTitle"] = metadata.get("title") or None
            item["youtubeViewCount"] = metadata.get("view_count")
            item["youtubeDurationSeconds"] = metadata.get("duration_seconds")
            if metadata.get("language"):
                item["language"] = metadata["language"]
    return items
