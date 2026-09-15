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
import urllib.request
import xml.etree.ElementTree as ET
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
    return re.sub(r"^http://", "https://", url) if url else ""


def _date(value: Any) -> str:
    if isinstance(value, str) and re.fullmatch(r"\d{8}", value):
        return f"{value[:4]}-{value[4:6]}-{value[6:]}"
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value
    raise ValueError(f"invalid upload date: {value!r}")


def _optional_date(value: Any) -> str | None:
    try:
        return _date(value)
    except ValueError:
        return None


def _obvious_music(title: str) -> bool:
    return bool(re.search(r"\b(?:live\s+set|dj\s+set)\b", title, re.IGNORECASE))


def _excluded(item: dict[str, Any], excluded_ids: set[str], excluded_playlist_id: str) -> bool:
    video_id = item.get("id") or item.get("youtubeId")
    title = str(item.get("title") or "")
    url = str(item.get("webpage_url") or item.get("url") or "")
    return (
        not video_id
        or video_id in excluded_ids
        or item.get("playlist_id") == excluded_playlist_id
        or "/shorts/" in url
        or "#shorts" in title.lower()
        or _obvious_music(title)
        or (item.get("duration") is not None and item["duration"] <= 180)
    )


def merge_entries(
    videos_tab: list[dict[str, Any]],
    prior_feed: dict[str, Any],
    rss_entries: list[dict[str, Any]],
    excluded_ids: set[str],
    excluded_playlist_id: str,
) -> dict[str, Any]:
    """Build only from the videos tab; RSS and prior feed only date/enrich it."""
    prior = {item["youtubeId"]: item for item in prior_feed.get("episodes", [])}
    rss = {item.get("id") or item.get("youtubeId"): item for item in rss_entries}
    episodes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for flat in videos_tab:
        video_id = flat.get("id") or flat.get("youtubeId")
        if not isinstance(video_id, str) or _excluded(flat, excluded_ids, excluded_playlist_id) or video_id in seen:
            continue
        seen.add(video_id)
        old = prior.get(video_id, {})
        recent = rss.get(video_id, {})
        date = _optional_date(flat.get("upload_date")) or _optional_date(old.get("date")) or _optional_date(recent.get("upload_date"))
        if not date:
            raise RuntimeError(f"new videos-tab item {video_id} has no trustworthy date")
        title = str(flat.get("title") or recent.get("title") or old.get("title") or "").strip()
        clean_title, guests = parse_title(title)
        episodes.append({
            "youtubeId": video_id,
            "date": date,
            "title": clean_title,
            "guests": guests,
            "thumb": _https(str(flat.get("thumbnail") or recent.get("thumbnail") or old.get("thumb") or "")),
            "listTitle": clean_title,
        })
    episodes.sort(key=lambda item: (item["date"], item["youtubeId"]), reverse=True)
    return {"channelUrl": CHANNEL_URL, "episodes": episodes[:MAX_EPISODES]}


def build_feed(channel_url: str, entries: list[dict[str, Any]], excluded_playlist_id: str) -> dict[str, Any]:
    """Compatibility wrapper for callers with already date-enriched entries."""
    feed = merge_entries(entries, {"episodes": []}, [], set(), excluded_playlist_id)
    feed["channelUrl"] = channel_url
    return feed


def validate_feed(feed: dict[str, Any]) -> None:
    if set(feed) != {"channelUrl", "episodes"} or feed["channelUrl"] != CHANNEL_URL:
        raise ValueError("feed must contain the exact channelUrl and episodes keys")
    if not isinstance(feed["episodes"], list) or len(feed["episodes"]) > MAX_EPISODES:
        raise ValueError("episodes must be a list of at most 50 items")
    keys = {"youtubeId", "date", "title", "guests", "thumb", "listTitle"}
    for episode in feed["episodes"]:
        if set(episode) != keys or not episode["youtubeId"] or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", episode["date"]):
            raise ValueError(f"invalid episode: {episode!r}")
        if episode["listTitle"] != episode["title"]:
            raise ValueError("listTitle must equal title")
        if episode["thumb"] and not episode["thumb"].startswith("https://"):
            raise ValueError("thumbnail URLs must use HTTPS")


def _yt_json(url: str, yt_dlp: str = "yt-dlp") -> dict[str, Any]:
    command = [yt_dlp, "--dump-single-json", "--flat-playlist", "--no-warnings", "--skip-download", "--extractor-args", "youtube:player_client=tv_embedded", url]
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"yt-dlp failed for {url}: {result.stderr[-500:]}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"yt-dlp returned invalid JSON for {url}") from exc


def _rss_entries() -> list[dict[str, Any]]:
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"
    request = urllib.request.Request(url, headers={"User-Agent": "endorfin-podcast-feed/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        root = ET.fromstring(response.read())
    ns = {"atom": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015", "media": "http://search.yahoo.com/mrss/"}
    results = []
    for entry in root.findall("atom:entry", ns):
        video_id = entry.findtext("yt:videoId", namespaces=ns)
        published = entry.findtext("atom:published", namespaces=ns) or ""
        if not video_id or not published:
            raise RuntimeError("channel RSS entry missing video ID or publication date")
        thumb = entry.find("media:group/media:thumbnail", ns)
        results.append({"id": video_id, "upload_date": published[:10], "title": entry.findtext("atom:title", namespaces=ns) or "", "thumbnail": thumb.attrib.get("url", "") if thumb is not None else ""})
    if not results:
        raise RuntimeError("channel RSS returned no entries")
    return results


def retrieve_entries(channel_url: str, excluded_playlist_id: str, yt_dlp: str = "yt-dlp", prior_feed: dict[str, Any] | None = None) -> dict[str, Any]:
    """Retrieve the videos tab and merge trustworthy historical/RSS dates."""
    channel = _yt_json(channel_url, yt_dlp)
    videos = [item for item in channel.get("entries", []) if item]
    try:
        excluded = _yt_json(f"https://www.youtube.com/playlist?list={excluded_playlist_id}", yt_dlp)
        excluded_ids = {item.get("id") for item in excluded.get("entries", []) if item.get("id")}
    except RuntimeError:
        excluded_ids = set()
    return merge_entries(videos, prior_feed or {"episodes": []}, _rss_entries(), excluded_ids, excluded_playlist_id)


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
    previous_path = args.output.with_name("feed.previous.json")
    prior = json.loads(previous_path.read_text(encoding="utf-8")) if previous_path.exists() else {"episodes": []}
    try:
        feed = retrieve_entries(args.channel_url, args.excluded_playlist, args.yt_dlp, prior)
    except RuntimeError as exc:
        if "no trustworthy date" in str(exc):
            print(f"No feed replacement: {exc}")
            return
        raise
    write_feed_atomically(args.output, feed)
    print(f"Generated {len(feed['episodes'])} episodes in {args.output}")


if __name__ == "__main__":
    main()
