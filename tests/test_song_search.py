import json
import tempfile
import unittest
from pathlib import Path

from app import song_search


class SongSearchTests(unittest.TestCase):
    def test_normalize_search_text_matches_web_search_shape(self):
        self.assertEqual(song_search.normalize_search_text(" Yes! BanG_Dream! "), "yesbangdream")
        self.assertEqual(song_search.normalize_search_text("ＡｂＣ １２３"), "abc123")

    def test_filtered_song_records_matches_id_and_fuzzy_title(self):
        records = [
            song_search.create_song_record("10", {"musicTitle": ["Yes! BanG_Dream!"]}),
            song_search.create_song_record("20", {"musicTitle": ["STAR BEAT!"]}),
            song_search.create_song_record("30", {"musicTitle": ["Returns"]}),
        ]

        by_id = song_search.filtered_song_records(records, "20")
        by_title = song_search.filtered_song_records(records, "ybang")

        self.assertEqual([record.id for record in by_id], ["20"])
        self.assertEqual(by_title[0].id, "10")

    def test_filtered_song_records_matches_romaji_title(self):
        records = [
            song_search.create_song_record("40", {"musicTitle": ["きらきら星"]}),
            song_search.create_song_record("50", {"musicTitle": ["Returns"]}),
        ]

        by_romaji = song_search.filtered_song_records(records, "kirakirahoshi")

        self.assertEqual([record.id for record in by_romaji], ["40"])

    def test_load_song_records_sorts_numeric_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "all.1.json"
            path.write_text(
                json.dumps(
                    {
                        "20": {"musicTitle": ["Second"]},
                        "3": {"musicTitle": ["First"]},
                    }
                ),
                encoding="utf-8",
            )

            records = song_search.load_song_records(path)

        self.assertEqual([record.id for record in records], ["3", "20"])


if __name__ == "__main__":
    unittest.main()
