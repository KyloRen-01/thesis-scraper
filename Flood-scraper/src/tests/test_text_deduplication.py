"""Offline checks for conservative, chronological Facebook deduplication."""

import csv
import io
import itertools
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from src.preprocessors import text_deduplication as dedup


COLUMNS = ["source", "query_page", "text", "post_timestamp", "scraped_timestamp", "user",
           "post_url", "image_paths", "image_count", "image_status"]
STAMP = "2026-10-20 08:00"
BODY = ("Flood in Davao City on October 20 2026 at 8 AM. "
        "Water remains knee deep along the highway and residents are waiting for assistance. "
        "Rescue teams are monitoring the area and advising motorists to use an alternative route. "
        "Local officials continue to assess the situation and coordinate assistance for affected residents. ")


def row(text=BODY, **changes):
    result = dict(zip(COLUMNS, ["Facebook", "Example", text, STAMP, "2026-10-20 12:00", "Example",
                               "https://www.facebook.com/Example/posts/123", "data/raw/facebook/images/a.png", "1", "downloaded"]))
    result.update(changes)
    return result


def write_input(directory, stamp, rows=None, columns=COLUMNS):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"fb_cleaned_floodData_{stamp}.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows([row()] if rows is None else rows)
    return path


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        return reader.fieldnames, list(reader)


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.matcher = dedup.LocationMatcher(Path(self.temp.name))
        self.engine = dedup.Deduplicator(self.matcher)

    def inspect_pair(self, first, second):
        self.engine.inspect(first, "first.csv", 2)
        return self.engine.inspect(second, "second.csv", 2)

    def test_indel_ratio_matches_reference_lcs_and_is_symmetric(self):
        def reference(a, b):
            dp = [0] * (len(b) + 1)
            for char in a:
                previous = dp[:]
                for j, other in enumerate(b, 1):
                    dp[j] = previous[j - 1] + 1 if char == other else max(previous[j], dp[j - 1])
            return 200 * dp[-1] / (len(a) + len(b)) if a or b else 100
        samples = ["", "a", "ab", "baba", "Davao", "Flood in Davao", "baha 1.5m", "Peñaplata"]
        for a, b in itertools.product(samples, repeat=2):
            self.assertAlmostEqual(dedup.similarity(a, b), reference(a, b))
            self.assertEqual(dedup.similarity(a, b), dedup.similarity(b, a))

    def test_url_identity_handles_mobile_tracking_story_and_reels(self):
        for url in ("https://m.facebook.com/Page/posts/123?ref=x", "https://facebook.com/reel/123/",
                    "https://www.facebook.com/permalink.php?story_fbid=123&id=456",
                    "https://www.facebook.com/watch/?v=123"):
            self.assertEqual(dedup.post_identity(url), "123")
        for url in ("https://facebook.com/Page", "https://evilfacebook.com/Page/posts/123", "bad", "https://facebook.com/search/?q=123"):
            self.assertEqual(dedup.post_identity(url), "")

    def test_exact_same_post_with_unknown_times_deduplicates_across_captures(self):
        result = self.inspect_pair(row(post_timestamp="Unknown"),
                                   row(text=BODY.upper(), post_timestamp="Unknown", scraped_timestamp="2026-11-01 10:00"))
        self.assertIsNotNone(result[1])
        self.assertEqual(result[2], "same_post_exact_text")

    def test_unknown_is_not_matching_time_for_different_urls(self):
        text = "Baha sa Davao City"
        result = self.inspect_pair(row(text, post_timestamp="Unknown"), row(text, post_timestamp="Unknown", post_url=""))
        self.assertIsNone(result[1])

    def test_scrape_time_does_not_supply_missing_event_time(self):
        result = self.inspect_pair(row("Flood in Davao City", post_timestamp="Unknown", post_url=""),
                                   row("Flood in Davao City", post_timestamp="Unknown", post_url=""))
        self.assertIsNone(result[1])
        self.assertEqual(result[0].time, ())

    def test_missing_location_is_not_equal_location(self):
        result = self.inspect_pair(row("Flood reported", post_url=""), row("Flood reported", post_url=""))
        self.assertIsNone(result[1])

    def test_caption_full_date_and_clock_supply_context(self):
        result = self.inspect_pair(row(post_timestamp="Unknown", post_url=""), row(post_timestamp="Unknown", post_url=""))
        self.assertIsNotNone(result[1])
        self.assertEqual(result[0].time, ("caption", "2026-10-20", "08:00:00"))

    def test_date_only_and_relative_time_do_not_supply_context(self):
        for text in ("Flood Davao on October 20 2026", "Flood Davao at 8 AM today", "Flood Davao on 2026-10-20"):
            self.assertEqual(dedup.time_evidence(row(text, post_timestamp="Unknown"), dedup.normalize(text)), ())

    def test_changed_known_post_time_keeps_identical_caption_even_same_url(self):
        result = self.inspect_pair(row(), row(post_timestamp="2026-10-20 10:00"))
        self.assertIsNone(result[1])

    def test_equivalent_timezone_timestamps_match(self):
        result = self.inspect_pair(row(post_timestamp="2026-10-20T00:00:00Z"), row(post_timestamp="2026-10-20T08:00:00+08:00"))
        self.assertIsNotNone(result[1])

    def test_same_text_location_and_time_without_same_url_is_duplicate(self):
        result = self.inspect_pair(row(), row(post_url="https://facebook.com/Other/posts/456"))
        self.assertIsNotNone(result[1])
        self.assertEqual(result[2], "same_location_time_and_content")

    def test_senior_prefix_full_thresholds_allow_safe_tail_only(self):
        result = self.inspect_pair(row(post_url=""), row(BODY + "Stay safe everyone", post_url=""))
        self.assertIsNotNone(result[1])
        self.assertGreaterEqual(result[3], 97)
        self.assertGreaterEqual(result[4], 90)

    def test_short_text_not_overmerged_by_tail_matching(self):
        result = self.inspect_pair(row("Flood Davao"), row("Flood Davao stay safe"))
        self.assertIsNone(result[1])

    def test_earliest_version_is_kept_even_if_later_caption_is_shorter(self):
        result = self.inspect_pair(row(BODY + "Stay safe everyone"), row())
        self.assertIsNotNone(result[1])
        self.assertEqual(result[1].file, "first.csv")

    def test_changed_word_order_is_not_treated_as_equivalent_facts(self):
        result = self.inspect_pair(row(BODY + "water reached bridge before school"),
                                   row(BODY + "water reached school before bridge"))
        self.assertIsNone(result[1])

    def test_changed_water_depth_negation_location_or_time_is_kept(self):
        for changed in (BODY.replace("knee", "waist"), BODY.replace("remains", "does not remain"),
                        BODY.replace("Davao", "Tagum"), BODY.replace("8 AM", "10 AM"),
                        BODY.replace("highway", "Matina highway"), BODY.replace("deep", "1.5m deep"),
                        BODY + "The bridge is now closed", BODY.replace("residents are waiting", "residents are not waiting")):
            with self.subTest(changed=changed):
                engine = dedup.Deduplicator(self.matcher)
                engine.inspect(row(), "a.csv", 2)
                self.assertIsNone(engine.inspect(row(changed), "b.csv", 2)[1])

    def test_fuzzy_unknown_time_is_retained_even_for_same_url(self):
        text = BODY.replace("on October 20 2026 at 8 AM", "today")
        result = self.inspect_pair(row(text, post_timestamp="Unknown"), row(text + "Stay safe", post_timestamp="Unknown"))
        self.assertIsNone(result[1])

    def test_different_structured_locations_are_not_dropped(self):
        result = self.inspect_pair(row(location="Matina"), row(location="Talomo"))
        self.assertIsNone(result[1])

    def test_image_files_and_reference_strings_are_never_merged(self):
        second = row(image_paths="data/raw/facebook/images/b.png")
        result = self.inspect_pair(row(), second)
        self.assertIsNone(result[1])
        self.assertEqual(result[2], "new_image_references")
        self.assertEqual(second["image_paths"], "data/raw/facebook/images/b.png")

    def test_subset_of_existing_image_references_can_be_removed(self):
        result = self.inspect_pair(row(image_paths="a.png;b.png"), row(image_paths="b.png"))
        self.assertIsNotNone(result[1])

    def test_blank_captions_are_never_dropped(self):
        result = self.inspect_pair(row(""), row(""))
        self.assertIsNone(result[1])
        self.assertEqual(result[2], "empty_text")

    def test_sources_are_not_conflated(self):
        self.assertIsNone(self.inspect_pair(row(), row(source="X"))[1])

    def test_lookup_matches_whole_place_names_and_city_aliases(self):
        root = Path(self.temp.name)
        (root / "refcitymun.csv").write_text("citymunDesc\nCITY OF DAVAO\nCITY OF TAGUM (Capital)\n", encoding="utf-8")
        matcher = dedup.LocationMatcher(root)
        self.assertEqual(matcher.extract({}, "city of davao and tagum city"), ("davao city", "tagum city"))
        self.assertEqual(matcher.extract({}, "davaocity"), ())


class FileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.inputs, self.outputs, self.lookup = root / "cleaned", root / "deduplicated", root / "lookup"
        self.lookup.mkdir()

    def run_dedup(self, **options):
        with redirect_stdout(io.StringIO()):
            return dedup.deduplicate_files(self.inputs, self.outputs, lookup_dir=self.lookup, **options)

    def test_files_are_sorted_by_parsed_dates_and_numeric_suffix(self):
        later = write_input(self.inputs, "01-01-2026_000000")
        oldest = write_input(self.inputs, "12-31-2025_235959")
        suffix10 = write_input(self.inputs, "01-01-2026_000000_10")
        suffix2 = write_input(self.inputs, "01-01-2026_000000_2")
        (self.inputs / "fb_cleaned_floodData_latest.csv").touch()
        self.assertEqual(dedup.discover_files(self.inputs), [oldest, later, suffix2, suffix10])

    def test_cross_file_dedup_schema_images_inputs_and_provenance(self):
        first = write_input(self.inputs, "10-09-2026_211231", [row(), row()])
        later = write_input(self.inputs, "10-20-2026_230340", [row(), row(image_paths="b.png")])
        original = {p: p.read_bytes() for p in (first, later)}
        records = self.run_dedup()
        self.assertEqual([r["kept_rows"] for r in records], [1, 1])
        self.assertEqual([r["removed_rows"] for r in records], [1, 1])
        for source, expected in ((first, [row()]), (later, [row(image_paths="b.png")])):
            target = self.outputs / dedup.output_name(source)
            self.assertEqual(read_csv(target), (COLUMNS, expected))
            self.assertEqual(source.read_bytes(), original[source])
        audit_path = self.outputs / "_metadata" / (dedup.output_name(later).removesuffix(".csv") + "_decisions.csv")
        _, audit = read_csv(audit_path)
        self.assertEqual(audit[0]["dedup_match_file"], first.name)
        self.assertEqual(audit[0]["dedup_match_row"], "2")
        self.assertEqual(audit[1]["dedup_reason"], "new_image_references")
        self.assertEqual(audit[0]["image_paths"], row()["image_paths"])
        for record in records:
            self.assertGreaterEqual(datetime.fromisoformat(record["completed_at"]), datetime.fromisoformat(record["started_at"]))
            self.assertEqual(datetime.fromisoformat(record["started_at"]).date(), datetime.now(dedup.PH_TIME).date())

    def test_next_and_reruns_reconstruct_prior_state_without_rewriting(self):
        first = write_input(self.inputs, "10-09-2026_211231")
        second = write_input(self.inputs, "10-20-2026_230340")
        self.assertEqual(len(self.run_dedup(next_only=True)), 1)
        target = self.outputs / dedup.output_name(first)
        before = (target.read_bytes(), target.stat().st_mtime_ns)
        result = self.run_dedup(next_only=True)
        self.assertEqual(result[0]["source_file"], second.name)
        self.assertEqual(result[0]["kept_rows"], 0)
        self.assertEqual(self.run_dedup(), [])
        self.assertEqual((target.read_bytes(), target.stat().st_mtime_ns), before)

    def test_changed_source_requires_complete_explicit_rebuild(self):
        first = write_input(self.inputs, "10-09-2026_211231")
        later = write_input(self.inputs, "10-20-2026_230340")
        self.run_dedup()
        write_input(self.inputs, "10-09-2026_211231", [row("Unrelated caption")])
        with self.assertRaisesRegex(ValueError, "--overwrite"):
            self.run_dedup()
        records = self.run_dedup(overwrite=True)
        self.assertEqual([r["kept_rows"] for r in records], [1, 1])

    def test_inserted_older_input_invalidates_downstream_before_writing(self):
        write_input(self.inputs, "10-20-2026_230340")
        self.run_dedup()
        earlier = write_input(self.inputs, "10-09-2026_211231")
        with self.assertRaisesRegex(ValueError, "--overwrite"):
            self.run_dedup()
        self.assertFalse((self.outputs / dedup.output_name(earlier)).exists())

    def test_changed_output_or_decision_file_is_protected(self):
        source = write_input(self.inputs, "10-09-2026_211231")
        self.run_dedup()
        target = self.outputs / dedup.output_name(source)
        target.write_bytes(target.read_bytes() + b"\n")
        with self.assertRaisesRegex(ValueError, "--overwrite"):
            self.run_dedup()
        self.run_dedup(overwrite=True)
        decision = next((self.outputs / "_metadata").glob("*_decisions.csv"))
        decision.write_bytes(decision.read_bytes() + b"\n")
        with self.assertRaisesRegex(ValueError, "--overwrite"):
            self.run_dedup()

    def test_lookup_changes_invalidate_previous_decisions(self):
        write_input(self.inputs, "10-09-2026_211231")
        self.run_dedup()
        (self.lookup / "refcitymun.csv").write_text("citymunDesc\nCITY OF DAVAO\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "--overwrite"):
            self.run_dedup()

    def test_header_only_outputs_are_valid(self):
        source = write_input(self.inputs, "10-09-2026_211231", [])
        self.run_dedup()
        self.assertEqual(read_csv(self.outputs / dedup.output_name(source)), (COLUMNS, []))

    def test_malformed_input_does_not_write_success(self):
        source = write_input(self.inputs, "10-09-2026_211231")
        for content in ('text,source\n"unterminated\n', 'text,source\nhello\n', 'body\nhello\n', 'text,text\na,b\n'):
            source.write_text(content, encoding="utf-8")
            with self.subTest(content=content), self.assertRaises((csv.Error, ValueError)):
                self.run_dedup()
            self.assertFalse((self.outputs / "_metadata/deduplication_manifest.csv").exists())

    def test_input_output_and_raw_paths_cannot_overlap(self):
        write_input(self.inputs, "10-09-2026_211231")
        for target in (self.inputs, self.inputs / "nested", self.inputs.parent, dedup.BASE_DIR / "data/raw/facebook"):
            with self.subTest(target=target), self.assertRaisesRegex(ValueError, "separate"):
                dedup.deduplicate_files(self.inputs, target, lookup_dir=self.lookup)

    def test_overwrite_cannot_be_partial(self):
        with self.assertRaisesRegex(ValueError, "without --next"):
            self.run_dedup(overwrite=True, next_only=True)

    def test_failed_output_commit_does_not_record_success(self):
        write_input(self.inputs, "10-09-2026_211231")
        with patch.object(Path, "replace", side_effect=OSError("locked")):
            with self.assertRaises(OSError):
                self.run_dedup()
        self.assertFalse((self.outputs / "_metadata/deduplication_manifest.csv").exists())
        self.assertEqual(list(self.outputs.glob("*.tmp")), [])

    def test_failed_audit_requires_explicit_recovery(self):
        source = write_input(self.inputs, "10-09-2026_211231")
        original = dedup._atomic_csv

        def fail_audit(path, columns, rows):
            if path.name.endswith("_decisions.csv"):
                raise OSError("audit is locked")
            return original(path, columns, rows)

        with patch.object(dedup, "_atomic_csv", side_effect=fail_audit):
            with self.assertRaises(OSError):
                self.run_dedup()
        self.assertTrue((self.outputs / dedup.output_name(source)).exists())
        self.assertFalse((self.outputs / "_metadata/deduplication_manifest.csv").exists())
        with self.assertRaisesRegex(ValueError, "--overwrite"):
            self.run_dedup()
        self.assertEqual(len(self.run_dedup(overwrite=True)), 1)

    def test_changing_input_during_processing_does_not_commit(self):
        source = write_input(self.inputs, "10-09-2026_211231")
        original_read = Path.read_bytes
        source_reads = 0

        def changed_read(path):
            nonlocal source_reads
            data = original_read(path)
            if path == source:
                source_reads += 1
                if source_reads > 1:
                    return data + b"\n"
            return data

        with patch.object(Path, "read_bytes", changed_read):
            with self.assertRaisesRegex(RuntimeError, "changed while reading"):
                self.run_dedup()
        self.assertFalse((self.outputs / dedup.output_name(source)).exists())


if __name__ == "__main__":
    unittest.main()
