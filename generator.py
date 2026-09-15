#!/usr/bin/env python3
"""Generate a durable public podcast feed from the Endorfin YouTube channel."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

CHANNEL_URL = "https://www.youtube.com/@endorfinworld/videos"
CHANNEL_ID = "UC672mE4Sjb6xKR0B507e56g"
DEFAULT_EXCLUDED_PLAYLIST = "PL758Jqgz2qfiE9O7F4ONeG8GBr8-RwruO"
MAX_EPISODES = 50


def parse_title(title: str) -> tuple[str, str]:
    """Split only high-confidence guest markers; otherwise preserve title."""
    title = re.sub(r"\s+", " ", title).strip()
    patterns = [
        r"\s+(?:\||[-–—])\s+(?:with|w/|featuring|feat\.?|ft\.?)\s+(.+)$",
        r"\s+(?:with|w/|featuring|feat\.?|ft\.?)\s+(.+)$",
    ]
    for pattern in patterns:
        match = re.search(pattern, title, flags=re.IGNORECASE)
        if match and re.fullmatch(r"[\w .,'&()/-]{2,100}", match.group(1).strip(), re.UNICODE):
            return title[: match.start()].rstrip(" |-|–—"), match.group(1).strip()
    match = re.search(r"\s+[-–—]\s+([A-Z][\w .'-]{2,80})$", title)
    if match:
        return title[: match.start()].rstrip(), match.group(1).strip()
    return title, ""


def _https(url: str) -> str:
    if not url:
        return ""
    return re.sub(r"^http://", "https://", url)


def _date(value: Any) -> str:
    if isinstance(value, str) and re.fullmatch(r"\d{8}", value):
        return f"{value[:4]}-{value[4:6]}-{value[6:]}"
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value
    raise ValueError(f"invalid upload date: {value!r}")


def build_feed(channel_url: str, entries: list[dict[str, Any]], excluded_playlist_id: str) -> dict[str, Any]:
    episodes = []
    for item in entries:
        video_id = item.get("id") or item.get("youtubeId")
        webpage_url = item.get("webpage_url") or item.get("url") or ""
        duration = item.get("duration")
        if not video_id or item.get("playlist_id") == excluded_playlist_id:
            continue
        if "/shorts/" in webpage_url or (duration is not None and duration <= 180):
            continue
        clean_title, guests = parse_title(str(item.get("title") or ""))
        episodes.append({
            "youtubeId": video_id,
            "date": _date(item.get("upload_date")),
            "title": clean_title,
            "guests": guests,
            "thumb": _https(str(item.get("thumbnail") or "")),
            "listTitle": str(item.get("list_title") or "Endorfin Podcast"),
        })
    episodes.sort(key=lambda x: (x["date"], x["youtubeId"]), reverse=True)
    return {"channelUrl": channel_url, "episodes": episodes[:MAX_EPISODES]}


def validate_feed(feed: dict[str, Any]) -> None:
    if set(feed) != {"channelUrl", "episodes"} or feed["channelUrl"] != CHANNEL_URL:
        raise ValueError("feed must contain the exact channelUrl and episodes keys")
    if not isinstance(feed["episodes"], list) or len(feed["episodes"]) > MAX_EPISODES:
        raise ValueError("episodes must be a list of at most 50 items")
    keys = {"youtubeId", "date", "title", "guests", "thumb", "listTitle"}
    for episode in feed["episodes"]:
        if set(episode) != keys or not episode["youtubeId"] or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", episode["date"]):
            raise ValueError(f"invalid episode: {episode!r}")
        if episode["thumb"] and not episode["thumb"].startswith("https://"):
            raise ValueError("thumbnail URLs must use HTTPS")


def _yt_json(url: str, yt_dlp: str = "yt-dlp") -> dict[str, Any]:
    command = [
        yt_dlp,
        "--dump-single-json",
        "--flat-playlist",
        "--no-warnings",
        "--skip-download",
        "--extractor-args",
        "youtube:player_client=tv_embedded",
        url,
    ]
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"yt-dlp failed for {url}: {result.stderr[-500:]}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"yt-dlp returned invalid JSON for {url}") from exc


def retrieve_entries(channel_url: str, excluded_playlist_id: str, yt_dlp: str = "yt-dlp") -> list[dict[str, Any]]:
    channel = _yt_json(channel_url, yt_dlp)
    excluded = _yt_json(f"https://www.youtube.com/playlist?list={excluded_playlist_id}", yt_dlp)
    excluded_ids = {x.get("id") for x in excluded.get("entries", []) if x.get("id")}
    results = []
    for flat in channel.get("entries", []):
        video_id = flat.get("id")
        if not video_id or video_id in excluded_ids:
            continue
        metadata = _yt_json(f"https://www.youtube.com/watch?v={video_id}", yt_dlp)
        metadata["playlist_id"] = ""
        results.append(metadata)
    return results


def write_feed_atomically(target: Path, feed: dict[str, Any]) -> None:
    validate_feed(feed)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target.parent, delete=False) as handle:
        temp_path = Path(handle.name)
        json.dump(feed, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    try:
        if target.exists():
            previous = target.with_name("feed.previous.json")
            with tempfile.NamedTemporaryFile("wb", dir=target.parent, delete=False) as backup:
                backup_path = Path(backup.name)
            shutil.copyfile(target, backup_path)
            os.replace(backup_path, previous)
        os.replace(temp_path, target)
    finally:
        temp_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("feed.json"))
    parser.add_argument("--channel-url", default=CHANNEL_URL)
    parser.add_argument("--excluded-playlist", default=DEFAULT_EXCLUDED_PLAYLIST)
    parser.add_argument("--yt-dlp", default="yt-dlp")
    args = parser.parse_args()
    entries = retrieve_entries(args.channel_url, args.excluded_playlist, args.yt_dlp)
    feed = build_feed(args.channel_url, entries, args.excluded_playlist)
    validate_feed(feed)
    write_feed_atomically(args.output, feed)
    print(f"Generated {len(feed['episodes'])} episodes in {args.output}")


if __name__ == "__main__":
    main()
