from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import httpx


BESTDORI_API = "https://bestdori.com/api"
DIFFICULTIES = ("easy", "normal", "hard", "expert", "special")
USER_AGENT = "bangdream-chart-downloader/1.0"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Download all Bestdori charts into local files.")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("charts"),
        help="Output directory. Defaults to charts.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=12,
        help="Maximum concurrent chart requests. Defaults to 12.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing chart files. Existing files are skipped by default.",
    )
    return parser


async def fetch_json(client: httpx.AsyncClient, path: str) -> Any:
    response = await client.get(f"{BESTDORI_API}/{path}")
    response.raise_for_status()
    return response.json()


async def fetch_songs(client: httpx.AsyncClient) -> dict[str, Any]:
    songs = await fetch_json(client, "songs/all.1.json")
    if not isinstance(songs, dict):
        raise ValueError("Bestdori songs/all.1.json did not return an object.")
    return songs


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp_path.replace(path)


def write_chart(path: Path, chart: Any) -> None:
    if not isinstance(chart, list):
        raise ValueError("Chart JSON top-level value is not an array.")
    write_json(path, chart)


async def download_chart(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    output_dir: Path,
    song_id: int,
    difficulty: str,
    overwrite: bool,
) -> str:
    output_path = output_dir / difficulty / f"{song_id}.json"
    if output_path.exists() and not overwrite:
        return "skipped"

    async with semaphore:
        try:
            chart = await fetch_json(client, f"charts/{song_id}/{difficulty}.json")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return "missing"
            print(
                f"HTTP {exc.response.status_code}: {song_id} {difficulty}",
                file=sys.stderr,
            )
            return "failed"
        except (httpx.RequestError, json.JSONDecodeError, ValueError) as exc:
            print(f"Failed: {song_id} {difficulty}: {exc}", file=sys.stderr)
            return "failed"

    try:
        write_chart(output_path, chart)
    except ValueError as exc:
        print(f"Invalid chart: {song_id} {difficulty}: {exc}", file=sys.stderr)
        return "failed"
    return "downloaded"


async def download_all(output_dir: Path, concurrency: int, overwrite: bool) -> dict[str, int]:
    limits = httpx.Limits(max_connections=max(1, concurrency), max_keepalive_connections=max(1, concurrency))
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    timeout = httpx.Timeout(30.0)
    async with httpx.AsyncClient(headers=headers, timeout=timeout, limits=limits, follow_redirects=True) as client:
        songs = await fetch_songs(client)
        write_json(output_dir / "all.1.json", songs)
        song_ids = sorted(int(song_id) for song_id in songs)
        semaphore = asyncio.Semaphore(max(1, concurrency))
        tasks = [
            download_chart(client, semaphore, output_dir, song_id, difficulty, overwrite)
            for song_id in song_ids
            for difficulty in DIFFICULTIES
        ]

        counts = {"downloaded": 0, "skipped": 0, "missing": 0, "failed": 0}
        total = len(tasks)
        for index, task in enumerate(asyncio.as_completed(tasks), start=1):
            status = await task
            counts[status] += 1
            if index % 100 == 0 or index == total:
                print(
                    f"{index}/{total} "
                    f"downloaded={counts['downloaded']} "
                    f"skipped={counts['skipped']} "
                    f"missing={counts['missing']} "
                    f"failed={counts['failed']}"
                )
        return counts


def main() -> int:
    args = build_parser().parse_args()
    try:
        counts = asyncio.run(
            download_all(
                output_dir=args.output,
                concurrency=args.concurrency,
                overwrite=args.overwrite,
            )
        )
    except (httpx.RequestError, httpx.HTTPStatusError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if counts["failed"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
