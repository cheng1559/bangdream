from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Sequence


DEFAULT_SONG_LIST_PATH = Path("charts") / "all.1.json"
MAX_FILTERED_SONGS = 80


@dataclass(frozen=True)
class SongRecord:
    id: str
    numeric_id: int
    title: str
    label: str
    search_targets: tuple[str, ...]


def get_song_titles(song: dict[str, Any]) -> list[str]:
    titles = song.get("musicTitle") or []
    if not isinstance(titles, list):
        return []
    return [title.strip() for title in titles if isinstance(title, str) and title.strip()]


def normalize_search_text(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value if value is not None else "")).lower()
    return "".join(char for char in normalized if char.isalnum())


@lru_cache(maxsize=1)
def _romaji_converter() -> Any | None:
    try:
        import pykakasi
    except ImportError:
        return None
    return pykakasi.kakasi()


def romanize_title(value: str) -> str:
    converter = _romaji_converter()
    if converter is None:
        return ""

    converted = converter.convert(value)
    return " ".join(
        item.get("hepburn", "")
        for item in converted
        if isinstance(item, dict) and item.get("hepburn")
    )


def title_search_targets(title: str) -> tuple[str, ...]:
    targets = [normalize_search_text(title)]
    romaji = romanize_title(title)
    if romaji:
        targets.append(normalize_search_text(romaji))
    return tuple(dict.fromkeys(target for target in targets if target))


def fuzzy_text_score(query: str, target: str) -> int:
    if not query or not target:
        return 0

    direct_index = target.find(query)
    if direct_index >= 0:
        return 10000 - direct_index * 10 - len(target)

    query_index = 0
    previous_match = -1
    score = 0

    for target_index, char in enumerate(target):
        if query_index >= len(query):
            break
        if char != query[query_index]:
            continue

        score += 18 if previous_match == target_index - 1 else 8
        previous_match = target_index
        query_index += 1

    return score - len(target) if query_index == len(query) else 0


def create_song_record(song_id: str | int, song: dict[str, Any]) -> SongRecord:
    titles = get_song_titles(song)
    title = titles[0] if titles else "Untitled Song"
    record_id = str(song_id)
    search_targets = tuple(
        dict.fromkeys(
            [
                normalize_search_text(record_id),
                *[
                    target
                    for song_title in titles
                    for target in title_search_targets(song_title)
                ],
            ]
        )
    )
    return SongRecord(
        id=record_id,
        numeric_id=int(record_id),
        title=title,
        label=f"{record_id}. {title}",
        search_targets=search_targets,
    )


def load_song_records(path: Path = DEFAULT_SONG_LIST_PATH) -> list[SongRecord]:
    with path.open("r", encoding="utf-8") as file:
        songs = json.load(file)
    if not isinstance(songs, dict):
        raise ValueError("Song list JSON top-level value must be an object.")

    records = [
        create_song_record(song_id, song)
        for song_id, song in songs.items()
        if isinstance(song, dict) and isinstance(song.get("musicTitle"), list)
    ]
    return sorted(records, key=lambda record: record.numeric_id)


def score_song_record(record: SongRecord, normalized_query: str) -> int:
    if not normalized_query:
        return 1
    return max((fuzzy_text_score(normalized_query, target) for target in record.search_targets), default=0)


def filtered_song_records(
    records: Sequence[SongRecord],
    query: str,
    limit: int = MAX_FILTERED_SONGS,
) -> list[SongRecord]:
    normalized_query = normalize_search_text(query)
    if not normalized_query:
        return list(records[:limit])

    scored = [
        (record, score)
        for record in records
        if (score := score_song_record(record, normalized_query)) > 0
    ]
    scored.sort(key=lambda item: (-item[1], item[0].numeric_id))
    return [record for record, _score in scored[:limit]]
