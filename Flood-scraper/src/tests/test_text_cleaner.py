"""Offline tests for chronological text cleaning and preserved post/image data."""

import csv
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from src.preprocessors import text_cleaner as cleaner


COLUMNS = ["source", "query_page", "text", "post_timestamp", "scraped_timestamp", "user",
           "post_url", "image_paths", "image_count", "image_status"]


def write_raw(directory, stamp, texts=None):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"fb_raw_floodData_{stamp}.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=COLUMNS)
        writer.writeheader()
        for text in (["Flood in Davao!!!"] if texts is None else texts):
            writer.writerow(dict(zip(COLUMNS, [
                "Facebook", "Example", text, "Unknown", "2026-10-09 21:12", "Example",
                "https://facebook.com/Example/posts/00123", "data/raw/facebook/images/a.png;data/raw/facebook/images/b.png",
                "2", "downloaded",
            ])))
    return path


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        return reader.fieldnames, list(reader)


class TextCleanerTests(unittest.TestCase):
    def test_urls_markup_emojis_and_decorative_punctuation_are_removed(self):
        text = '<p>\U0001f6a8 <b>BAHA!!!</b> sa Davao City... See more</p><!-- hidden --><script>bad()</script> https://t.co/abc www.example.com'
        self.assertEqual(cleaner.clean_text(text), "BAHA sa Davao City")

    def test_stylized_unicode_and_local_names_are_not_ascii_stripped(self):
        self.assertEqual(cleaner.clean_text("\U0001d403\U0001d41a\U0001d42f\U0001d41a\U0001d428: Pe\u00f1aplata, M\u2019lang!!!"), "Davao Pe\u00f1aplata M'lang")
        self.assertEqual(cleaner.clean_text("\u6d2a\u6c34 Davao"), "\u6d2a\u6c34 Davao")

    def test_short_reports_and_negation_are_retained(self):
        self.assertEqual(cleaner.clean_text("Walay baha sa Davao!"), "Walay baha sa Davao")
        self.assertEqual(cleaner.clean_text("Not flooded."), "Not flooded")
        self.assertEqual(cleaner.clean_text("Baha!"), "Baha")

    def test_hashtag_markers_removed_but_location_words_kept(self):
        self.assertEqual(cleaner.clean_text("#DavaoCity #DavaoDeOro #MINPRSD #Baha_PH"), "Davao City Davao De Oro MINPRSD Baha PH")

    def test_depths_dates_times_ranges_and_negative_values_keep_their_meaning(self):
        self.assertEqual(cleaner.clean_text("Depth: 1.5 m; .5 m; 1 \u2013 2 m at 09:00 on 10/20/2026. -0.2 m."),
                         "Depth 1.5 m 0.5 m 1-2 m at 09:00 on 10/20/2026 -0.2 m")
        self.assertEqual(cleaner.clean_text("Rain >=100 mm, water <2m, 80%, \u20b11,000."),
                         "Rain at least 100 mm water less than 2m 80 percent PHP 1,000")

    def test_encoded_html_and_entities_are_cleaned(self):
        self.assertEqual(cleaner.clean_text("&lt;b&gt;Baha&lt;/b&gt; &amp; ulan&nbsp;Davao"), "Baha and ulan Davao")

    def test_whitespace_and_invisible_noise_normalize_without_joining_words(self):
        self.assertEqual(cleaner.clean_text("flo\u200bod\tDavao\r\nCity\x00rescue"), "flood Davao City rescue")

    def test_empty_and_noise_only_values_remain_empty(self):
        for value in (None, "", "NaN", "null", "None", "\U0001f6a8\U0001f30a!!!", "<script>noise</script>"):
            with self.subTest(value=value):
                self.assertEqual(cleaner.clean_text(value), "")

    def test_cleaning_is_idempotent(self):
        for text in ("#DavaoCity <b>BAHA!!!</b> 1.5 m See less", "M'lang 09:00 -0.2 m", "Rain >=100mm; 80%"):
            once = cleaner.clean_text(text)
            self.assertEqual(cleaner.clean_text(once), once)


class TextCleanerFileTests(unittest.TestCase):
    def test_files_are_sorted_by_parsed_scrape_date_not_name_or_mtime(self):
        with tempfile.TemporaryDirectory() as temp:
            raw = Path(temp) / "raw"
            later = write_raw(raw, "01-01-2026_000000")
            first = write_raw(raw, "12-31-2025_235959")
            write_raw(raw, "01-01-2026_000000_10")
            second = write_raw(raw, "01-01-2026_000000_2")
            (raw / "fb_raw_floodData_latest.csv").touch()
            self.assertEqual(cleaner.discover_files(raw), [first, later, second, raw / "fb_raw_floodData_01-01-2026_000000_10.csv"])

    def test_one_output_per_raw_preserves_schema_rows_links_and_raw_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            raw, output = Path(temp) / "raw", Path(temp) / "cleaned"
            latest = write_raw(raw, "10-20-2026_230340", ["Same text!", "Same text!", "\U0001f6a8!!!"])
            earliest = write_raw(raw, "10-09-2026_211231", ["Baha!!!"])
            before = {p: p.read_bytes() for p in raw.iterdir()}
            with redirect_stdout(io.StringIO()):
                records = cleaner.clean_files(raw, output)
            self.assertEqual([r["source_file"] for r in records], [earliest.name, latest.name])
            for source in (earliest, latest):
                columns, cleaned = read_csv(output / cleaner.output_name(source))
                _, original = read_csv(source)
                self.assertEqual(columns, COLUMNS)
                self.assertEqual(len(cleaned), len(original))
                for a, b in zip(original, cleaned):
                    self.assertEqual({k: v for k, v in a.items() if k != "text"}, {k: v for k, v in b.items() if k != "text"})
                self.assertEqual(source.read_bytes(), before[source])
            self.assertEqual(records[1]["empty_cleaned_texts"], 1)
            _, manifest = read_csv(output / "_metadata/cleaning_manifest.csv")
            self.assertEqual(len(manifest), 2)
            for item in manifest:
                started = datetime.fromisoformat(item["cleaning_started_at"])
                finished = datetime.fromisoformat(item["cleaning_completed_at"])
                self.assertGreaterEqual(finished, started)
                self.assertEqual(started.utcoffset(), cleaner.PH_TIME.utcoffset(None))
                self.assertEqual(started.date(), datetime.now(cleaner.PH_TIME).date())
            self.assertLessEqual(manifest[0]["cleaning_completed_at"], manifest[1]["cleaning_started_at"])

    def test_next_mode_and_restart_skip_completed_files_without_rewriting(self):
        with tempfile.TemporaryDirectory() as temp:
            raw, output = Path(temp) / "raw", Path(temp) / "cleaned"
            first = write_raw(raw, "10-09-2026_211231")
            second = write_raw(raw, "10-20-2026_230340")
            with redirect_stdout(io.StringIO()):
                one = cleaner.clean_files(raw, output, next_only=True)
                first_output = output / cleaner.output_name(first)
                snapshot = (first_output.read_bytes(), first_output.stat().st_mtime_ns)
                two = cleaner.clean_files(raw, output, next_only=True)
                three = cleaner.clean_files(raw, output)
            self.assertEqual([r["source_file"] for r in one + two], [first.name, second.name])
            self.assertEqual(three, [])
            self.assertEqual((first_output.read_bytes(), first_output.stat().st_mtime_ns), snapshot)

    def test_changed_raw_or_edited_output_requires_explicit_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            raw, output = Path(temp) / "raw", Path(temp) / "cleaned"
            source = write_raw(raw, "10-09-2026_211231")
            with redirect_stdout(io.StringIO()):
                cleaner.clean_files(raw, output)
                write_raw(raw, "10-09-2026_211231", ["Baha na!"])
                with self.assertRaisesRegex(ValueError, "--overwrite"):
                    cleaner.clean_files(raw, output)
                cleaner.clean_files(raw, output, overwrite=True)
                target = output / cleaner.output_name(source)
                target.write_bytes(target.read_bytes().replace(b"Baha na", b"Edited"))
                with self.assertRaisesRegex(ValueError, "--overwrite"):
                    cleaner.clean_files(raw, output)

    def test_header_only_raw_produces_header_only_cleaned_csv(self):
        with tempfile.TemporaryDirectory() as temp:
            raw, output = Path(temp) / "raw", Path(temp) / "cleaned"
            source = write_raw(raw, "10-09-2026_211231", [])
            with redirect_stdout(io.StringIO()):
                cleaner.clean_files(raw, output)
            self.assertEqual(read_csv(output / cleaner.output_name(source)), (COLUMNS, []))

    def test_missing_text_or_malformed_csv_does_not_write_output(self):
        with tempfile.TemporaryDirectory() as temp:
            raw, output = Path(temp) / "raw", Path(temp) / "cleaned"
            source = write_raw(raw, "10-09-2026_211231")
            for text in ('source,body\nFacebook,hello\n', 'text,source\n"unterminated\n', 'text,source\nhello\n'):
                source.write_text(text, encoding="utf-8")
                with self.subTest(text=text), self.assertRaises((ValueError, csv.Error)):
                    cleaner.clean_files(raw, output)
                self.assertFalse((output / cleaner.output_name(source)).exists())

    def test_raw_and_output_directories_must_not_overlap(self):
        with tempfile.TemporaryDirectory() as temp:
            raw = Path(temp) / "raw"
            write_raw(raw, "10-09-2026_211231")
            for output in (raw, raw / "cleaned", Path(temp)):
                with self.assertRaisesRegex(ValueError, "separate"):
                    cleaner.clean_files(raw, output)

    def test_failed_csv_commit_never_writes_audit_success(self):
        with tempfile.TemporaryDirectory() as temp:
            raw, output = Path(temp) / "raw", Path(temp) / "cleaned"
            write_raw(raw, "10-09-2026_211231")
            with patch.object(Path, "replace", side_effect=OSError("file locked")):
                with self.assertRaises(OSError):
                    cleaner.clean_files(raw, output)
            self.assertFalse((output / "_metadata/cleaning_manifest.csv").exists())
            self.assertEqual(list(output.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
