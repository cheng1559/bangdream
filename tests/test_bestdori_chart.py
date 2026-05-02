import argparse
import json
import sys
from collections import Counter
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


API_BASE = "https://bestdori.com/api"
DIFFICULTIES = {"easy", "normal", "hard", "expert", "special"}


def fetch_json(url: str):
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "bangdream-bestdori-api-test/1.0",
        },
    )
    with urlopen(request, timeout=15) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return json.loads(response.read().decode(charset))


def fetch_chart(song_id: int, difficulty: str):
    url = f"{API_BASE}/charts/{song_id}/{difficulty}.json"
    return url, fetch_json(url)


def summarize_chart(chart):
    note_types = Counter(item.get("type", "<missing>") for item in chart)
    playable_notes = sum(
        count
        for note_type, count in note_types.items()
        if note_type not in {"BPM", "System"}
    )
    bpm_values = [
        item.get("bpm")
        for item in chart
        if item.get("type") == "BPM" and item.get("bpm") is not None
    ]
    last_beat = max((item.get("beat", 0) for item in chart), default=0)

    return {
        "objects": len(chart),
        "playable_notes_estimate": playable_notes,
        "last_beat": last_beat,
        "bpm": bpm_values,
        "types": dict(note_types),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch an official BanG Dream chart from Bestdori's raw API."
    )
    parser.add_argument("song_id", nargs="?", type=int, default=1)
    parser.add_argument("difficulty", nargs="?", default="expert")
    parser.add_argument(
        "--save",
        metavar="PATH",
        help="Write the raw chart JSON to a file.",
    )
    args = parser.parse_args()

    difficulty = args.difficulty.lower()
    if difficulty not in DIFFICULTIES:
        print(
            f"Invalid difficulty: {args.difficulty}. "
            f"Use one of: {', '.join(sorted(DIFFICULTIES))}",
            file=sys.stderr,
        )
        return 2

    try:
        url, chart = fetch_chart(args.song_id, difficulty)
    except HTTPError as exc:
        if exc.code == 404:
            print(
                f"No chart found: song_id={args.song_id}, difficulty={difficulty} ({url})",
                file=sys.stderr,
            )
        else:
            print(f"HTTP error {exc.code}: {exc.reason}", file=sys.stderr)
        return 1
    except URLError as exc:
        print(f"Network error: {exc.reason}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(f"Response was not valid JSON: {exc}", file=sys.stderr)
        return 1

    summary = summarize_chart(chart)
    print(f"Fetched: {url}")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("First 5 objects:")
    print(json.dumps(chart[:5], ensure_ascii=False, indent=2))

    if args.save:
        with open(args.save, "w", encoding="utf-8") as file:
            json.dump(chart, file, ensure_ascii=False, indent=2)
        print(f"Saved raw chart JSON to: {args.save}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
