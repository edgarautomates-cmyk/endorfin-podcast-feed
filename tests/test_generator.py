import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from generator import (
    build_feed,
    merge_entries,
    parse_title,
    validate_feed,
    write_feed_atomically,
)
import generator


CHANNEL_URL = "https://www.youtube.com/@endorfinworld/videos"
PLAYLIST_ID = "PL758Jqgz2qfiE9O7F4ONeG8GBr8-RwruO"


def test_parse_title_extracts_guest_from_deterministic_patterns():
    assert parse_title("Endorfin Podcast #12 | with Jane Doe") == ("Endorfin Podcast #12", "Jane Doe")
    assert parse_title("Endorfin Podcast — John Smith") == ("Endorfin Podcast", "John Smith")


def test_parse_title_handles_actual_latvian_channel_patterns():
    assert parse_title("Pelnām vairāk, bet jūtamies nabadzīgāki. Kāpēc tā notiek? I Vita Solo, Lelde Matroze") == (
        "Pelnām vairāk, bet jūtamies nabadzīgāki. Kāpēc tā notiek?",
        "Vita Solo, Lelde Matroze",
    )
    assert parse_title("Kāpēc vientulība pieaug? I Juris Grave un Rita Grīnberga") == (
        "Kāpēc vientulība pieaug?",
        "Juris Grave un Rita Grīnberga",
    )
    assert parse_title("40 skolotāji piecēlās pret vienu skolnieku! Šodien pasaule mācās no viņa | Raimonds Elbakjans") == (
        "40 skolotāji piecēlās pret vienu skolnieku! Šodien pasaule mācās no viņa",
        "Raimonds Elbakjans",
    )


def test_parse_title_only_splits_sentence_dot_for_strong_name_suffix():
    assert parse_title("Saruna par drosmi. Vita Solo") == ("Saruna par drosmi.", "Vita Solo")
    assert parse_title("Šis ir parasts teikums. Kāpēc mēs baidāmies") == (
        "Šis ir parasts teikums. Kāpēc mēs baidāmies",
        "",
    )


def test_parse_title_leaves_uncertain_guest_blank():
    assert parse_title("Endorfin Podcast #7") == ("Endorfin Podcast #7", "")


def test_build_feed_excludes_shorts_and_music_playlist_and_sorts_newest():
    entries = [
        {"id": "old", "upload_date": "20240101", "title": "Old episode", "duration": 1800, "playlist_id": "x", "thumbnail": "http://img/old"},
        {"id": "short", "upload_date": "20250101", "title": "Short", "duration": 45, "playlist_id": "x", "thumbnail": "http://img/short"},
        {"id": "music", "upload_date": "20260101", "title": "Music", "duration": 1800, "playlist_id": PLAYLIST_ID, "thumbnail": "http://img/music"},
        {"id": "new", "upload_date": "20251231", "title": "New episode", "duration": 2400, "playlist_id": "x", "thumbnail": "http://img/new"},
    ]
    feed = build_feed(CHANNEL_URL, entries, PLAYLIST_ID)
    assert [e["youtubeId"] for e in feed["episodes"]] == ["new", "old"]
    assert feed["episodes"][0]["thumb"].startswith("https://")


def test_rss_only_shorts_are_not_an_episode_universe():
    videos = [{"id": "podcast", "title": "Podcast", "upload_date": "20240101"}]
    rss = [
        {"id": "podcast", "title": "Podcast", "upload_date": "20240101"},
        {"id": "rss-short", "title": "Short", "upload_date": "20250101", "webpage_url": "https://youtube.com/shorts/rss-short"},
    ]
    feed = merge_entries(videos, {"channelUrl": CHANNEL_URL, "episodes": []}, rss, set(), PLAYLIST_ID)
    assert [item["youtubeId"] for item in feed["episodes"]] == ["podcast"]


def test_videos_tab_items_are_kept_even_when_rss_does_not_list_them():
    videos = [{"id": "tab-item", "title": "Tab item", "upload_date": "20240101"}]
    feed = merge_entries(videos, {"channelUrl": CHANNEL_URL, "episodes": []}, [], set(), PLAYLIST_ID)
    assert feed["episodes"][0]["youtubeId"] == "tab-item"


def test_live_set_is_excluded_without_playlist_lookup():
    videos = [
        {"id": "music", "title": "Edgar Kozak Live set", "upload_date": "20240101"},
        {"id": "podcast", "title": "Podcast", "upload_date": "20240102"},
    ]
    feed = merge_entries(videos, {"channelUrl": CHANNEL_URL, "episodes": []}, [], set(), PLAYLIST_ID)
    assert [item["youtubeId"] for item in feed["episodes"]] == ["podcast"]


def test_undateable_new_videos_tab_item_fails_closed():
    videos = [{"id": "new", "title": "New item"}]
    with pytest.raises(RuntimeError, match="trustworthy date"):
        merge_entries(videos, {"channelUrl": CHANNEL_URL, "episodes": []}, [], set(), PLAYLIST_ID)


def test_merge_stops_after_50_candidates_before_old_undateable_item():
    videos = [{"id": "music", "title": "Live set", "upload_date": "20260101", "duration": 2400}] + [
        {"id": f"episode-{i}", "title": f"Episode {i}", "upload_date": f"2026{(i // 28) + 1:02d}{(i % 28) + 1:02d}"}
        for i in range(49)
    ] + [
        {"id": "Zq-7jLNq0o8", "title": "Old item without date"},
        {"id": "episode-50", "title": "Outside candidate window", "upload_date": "20240101"},
    ]
    feed = merge_entries(videos, {"channelUrl": CHANNEL_URL, "episodes": []}, [], set(), PLAYLIST_ID)
    assert len(feed["episodes"]) == 49
    assert "Zq-7jLNq0o8" not in {item["youtubeId"] for item in feed["episodes"]}


def test_main_uses_current_feed_as_prior_enrichment(tmp_path: Path, monkeypatch):
    current = {"channelUrl": CHANNEL_URL, "episodes": [{"youtubeId": "current", "title": "Current"}]}
    backup = {"channelUrl": CHANNEL_URL, "episodes": [{"youtubeId": "backup", "title": "Backup"}]}
    (tmp_path / "feed.json").write_text(json.dumps(current), encoding="utf-8")
    (tmp_path / "feed.previous.json").write_text(json.dumps(backup), encoding="utf-8")
    captured = {}
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["generator.py"])
    monkeypatch.setattr(generator, "retrieve_entries", lambda *args: captured.setdefault("prior", args[3]) or {"channelUrl": CHANNEL_URL, "episodes": []})
    monkeypatch.setattr(generator, "write_feed_atomically", lambda *args: None)
    generator.main()
    assert captured["prior"] == current


def test_main_propagates_no_trustworthy_date_failure(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["generator.py"])
    monkeypatch.setattr(generator, "retrieve_entries", lambda *args: (_ for _ in ()).throw(RuntimeError("no trustworthy date")))
    with pytest.raises(RuntimeError, match="no trustworthy date"):
        generator.main()


def test_list_title_equals_episode_title():
    feed = merge_entries(
        [{"id": "one", "title": "Podcast with Guest", "upload_date": "20240101"}],
        {"channelUrl": CHANNEL_URL, "episodes": []}, [], set(), PLAYLIST_ID,
    )
    assert feed["episodes"][0]["listTitle"] == feed["episodes"][0]["title"]


def test_validate_feed_rejects_bad_shape():
    with pytest.raises(ValueError):
        validate_feed({"channelUrl": CHANNEL_URL, "episodes": [{"youtubeId": ""}]})


def test_write_feed_retains_previous_only_after_success(tmp_path: Path):
    target = tmp_path / "feed.json"
    target.write_text('{"old": true}\n')
    good = {"channelUrl": CHANNEL_URL, "episodes": []}
    write_feed_atomically(target, good)
    assert json.loads(target.read_text()) == good
    assert json.loads((tmp_path / "feed.previous.json").read_text()) == {"old": True}


def test_write_feed_failure_leaves_existing_files_untouched(tmp_path: Path):
    target = tmp_path / "feed.json"
    target.write_text('{"old": true}\n')
    with pytest.raises(ValueError):
        write_feed_atomically(target, {"channelUrl": CHANNEL_URL, "episodes": [{"youtubeId": ""}]})
    assert target.read_text() == '{"old": true}\n'
    assert not (tmp_path / "feed.previous.json").exists()
