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
    connection.execute(
        """CREATE TABLE IF NOT EXISTS youtube_video_meta (
            video_id TEXT PRIMARY KEY,
            title TEXT,
            duration_seconds INTEGER,
            view_count INTEGER,
            language TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )"""
    )


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
            "SELECT video_id, title, duration_seconds, view_count, language "
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
            "(video_id, title, duration_seconds, view_count, language) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(video_id) DO UPDATE SET title=excluded.title, "
            "duration_seconds=excluded.duration_seconds, view_count=excluded.view_count, "
            "language=excluded.language, updated_at=CURRENT_TIMESTAMP",
            (
                video_id,
                metadata.get("title") or "",
                int(metadata.get("duration_seconds") or 0),
                int(metadata.get("view_count") or 0),
                metadata.get("language") or "",
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


def _fetch_sync(video_id: str) -> dict[str, Any] | None:
    """优先用 oEmbed 取标题，失败时再尝试 yt-dlp。"""
    oembed = _fetch_oembed(video_id)
    if oembed:
        # oEmbed 的标题足够用于页面展示和语言判断，其他数值字段继续由 yt-dlp 补充。
        return {**oembed, "duration_seconds": 0, "view_count": 0, "language": ""}
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
        "duration_seconds": int(float(payload.get("duration") or 0)),
        "view_count": int(payload.get("view_count") or 0),
        "language": str(payload.get("language") or payload.get("original_language") or "").strip(),
    }


async def enrich(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """为素材补充 YouTube URL、标题、时长和播放量，并写入本地缓存。"""
    ids = list(dict.fromkeys(str(item.get("youtubeVideoId") or "").strip() for item in items))
    ids = [video_id for video_id in ids if _ID_PATTERN.fullmatch(video_id)]
    if not ids:
        return items
    cached = await asyncio.to_thread(_lookup, ids, settings.database_path)
    missing = [video_id for video_id in ids if video_id not in cached]
    semaphore = asyncio.Semaphore(_FETCH_LIMIT)

    async def fetch(video_id: str) -> tuple[str, dict[str, Any] | None]:
        async with semaphore:
            return video_id, await asyncio.to_thread(_fetch_sync, video_id)

    fetched = await asyncio.gather(*(fetch(video_id) for video_id in missing))
    for video_id, metadata in fetched:
        if metadata:
            cached[video_id] = metadata
            await asyncio.to_thread(_store, video_id, metadata, settings.database_path)
    for item in items:
        video_id = str(item.get("youtubeVideoId") or "").strip()
        if not _ID_PATTERN.fullmatch(video_id):
            continue
        item["youtubeVideoUrl"] = youtube_video_url(video_id)
        metadata = cached.get(video_id)
        if metadata:
            item["youtubeTitle"] = metadata.get("title") or None
            item["youtubeDurationSeconds"] = int(metadata.get("duration_seconds") or 0) or None
            item["youtubeViewCount"] = int(metadata.get("view_count") or 0)
            if metadata.get("language"):
                item["language"] = metadata["language"]
    return items
