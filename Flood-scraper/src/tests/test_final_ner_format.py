"""Compact final schema, provenance-preserving backup, and checkpoint tests."""

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(SRC / "scripts"))
from preprocessors.format_ner_posts import FINAL_COLUMNS, format_post, page_source, display_datetime
import format_final_ner_csvs as migration
from src.tests.test_xlmr_ner import FakeNER, COLUMNS, row, write, lookup, u, runner


class FormatTests(unittest.TestCase):
    def test_exact_requested_schema_and_order(self):
        self.assertEqual(list(format_post(row())),
            ["source", "page_source", "text_content", "datetime", "location", "image_paths", "url"])

    def test_author_from_url_not_search_keyword(self):
        self.assertEqual(format_post(row(query_page="#FloodAlert", post_url="https://www.facebook.com/civildefensedavao/posts/123"))["page_source"], "Civil Defense Davao")

    def test_missing_url_uses_recorded_author_not_query(self):
        self.assertEqual(format_post(row(post_url="", user="Local Reporter", query_page="#Baha"))["page_source"], "Local Reporter")
        self.assertEqual(format_post(row(post_url="", user="Unknown", query_page="#Baha"))["page_source"], "Unknown")

    def test_url_shapes_do_not_create_fake_page_names(self):
        self.assertEqual(page_source("https://facebook.com/profile.php?id=123"), "123")
        self.assertEqual(page_source("https://facebook.com/permalink.php?story_fbid=9&id=123"), "123")
        self.assertEqual(page_source("https://facebook.com/watch/?v=9", "Actual Author"), "Actual Author")
        self.assertEqual(page_source("https://facebook.com/groups/456/posts/7"), "group:456")
        self.assertEqual(page_source("https://evilfacebook.com/gmanews/posts/1"), "Unknown")
        self.assertEqual(page_source("javascript:alert(1)", "Reporter"), "Reporter")

    def test_short_alert_and_unicode_not_discarded(self):
        result = format_post(row("BAHA sa Peñaplata! 🚨 See more https://example.com"))
        self.assertEqual(result["text_content"], "baha sa peñaplata")

    def test_no_datetime_fallback_to_scraping_or_current_time(self):
        self.assertEqual(format_post(row(event_time_primary="Unknown", scraped_timestamp="2026-10-22"))["datetime"], "")

    def test_datetime_precedence_preserves_iso_precision(self):
        result = format_post(row(event_time_primary_iso="2026-10-20", event_time_primary="October 20 2026"))
        self.assertEqual(result["datetime"], "October 20, 2026")

    def test_requested_datetime_display_and_24_hour_clock(self):
        self.assertEqual(display_datetime("2025-08-30T21:00:00+08:00"), "August 30, 2025 21:00:00")
        self.assertEqual(display_datetime("2025-08-30T13:00:00Z"), "August 30, 2025 21:00:00")

    def test_display_preserves_date_precision_and_unknowns(self):
        self.assertEqual(display_datetime("2025-08-30"), "August 30, 2025")
        self.assertEqual(display_datetime("Unknown"), "")
        self.assertEqual(display_datetime(""), "")
        self.assertEqual(display_datetime("August 30, 2025 21:00:00"), "August 30, 2025 21:00:00")

    def test_display_timezone_rollover(self):
        self.assertEqual(display_datetime("2025-08-30T20:00:00Z"), "August 31, 2025 04:00:00")

    def test_all_image_references_url_and_location_unchanged(self):
        original = row(image_paths="a.png; b.png", post_url="https://facebook.com/post/1?tracking=a", location="DAVAO CITY (citymun)")
        result = format_post(original)
        for source, target in [("image_paths", "image_paths"), ("post_url", "url"), ("location", "location")]:
            self.assertEqual(original[source], result[target])

    def test_blank_text_not_filtered_and_reformatting_idempotent(self):
        result = format_post(row("", location="", event_time_primary=""))
        self.assertEqual(result["text_content"], "")
        self.assertEqual(format_post(result), result)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.input, self.output, self.references = (self.root / name for name in ("input", "ner", "lookup"))
        self.input.mkdir()
        lookup(self.references)
        self.source = self.input / "fb_deduplicate_floodData_10-20-2026_230340.csv"
        write(self.source, COLUMNS, [row(), row("Baha sa Davao City", image_paths="different.png")])
        self.models = {"datetime_recognizer": FakeNER(), "location_recognizer": FakeNER([("Davao City", "LOC")])}
        with redirect_stdout(io.StringIO()):
            runner.run_pipeline(input_dir=self.input, output_dir=self.output, lookup_dir=self.references, **self.models)
        self.final = u.target(self.source, "combined", self.output)
        # Reconstruct the legacy wide CSV with an honest output checksum.
        date_columns, dates, _ = u.read_csv(u.details_path(u.target(self.source, "datetime", self.output)))
        loc_columns, locations, _ = u.read_csv(u.details_path(u.target(self.source, "locations", self.output)))
        columns = list(dict.fromkeys(date_columns + loc_columns))
        rich = [{**date, **location} for date, location in zip(dates, locations)]
        write(self.final, columns, rich)
        manifest = self.final.parent / "_metadata/manifest.csv"
        records = u.history(manifest)
        records[-1]["output_sha256"] = u.digest(self.final.read_bytes())
        records[-1]["recipe_sha256"] = "legacy-format"
        write(manifest, u.MANIFEST, records)

    def migrate(self, **kwargs):
        with redirect_stdout(io.StringIO()):
            return migration.migrate(input_dir=self.input, output_dir=self.output,
                archive_dir=self.root / "archive", **kwargs)

    def test_dry_run_does_not_write(self):
        before = self.final.read_bytes()
        result = self.migrate()
        self.assertEqual(result["changed"], 1)
        self.assertEqual(self.final.read_bytes(), before)
        self.assertFalse((self.root / "archive").exists())

    def test_backup_schema_rows_images_and_idempotent_pipeline(self):
        before = self.final.read_bytes()
        source_before = self.source.read_bytes()
        intermediates = {u.target(self.source, stage, self.output): u.target(self.source, stage, self.output).read_bytes()
                         for stage in ("datetime", "locations")}
        result = self.migrate(apply=True)
        self.assertEqual((Path(result["backup"]) / self.final.name).read_bytes(), before)
        columns, rows, _ = u.read_csv(self.final)
        self.assertEqual(columns, FINAL_COLUMNS)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["image_paths"], "different.png")
        self.assertEqual(self.source.read_bytes(), source_before)
        self.assertEqual({p: p.read_bytes() for p in intermediates}, intermediates)
        self.assertEqual(self.migrate(apply=True)["changed"], 0)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(runner.run_pipeline(input_dir=self.input, output_dir=self.output,
                lookup_dir=self.references, **self.models), [])

    def test_edited_final_rejected_before_backup_or_overwrite(self):
        self.final.write_bytes(self.final.read_bytes() + b"\n")
        before = self.final.read_bytes()
        with self.assertRaisesRegex(ValueError, "Stale"):
            self.migrate(apply=True)
        self.assertEqual(self.final.read_bytes(), before)
        self.assertFalse((self.root / "archive").exists())


if __name__ == "__main__":
    unittest.main()
