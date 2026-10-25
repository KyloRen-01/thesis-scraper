"""Offline attachment-count filtering tests; no AI/NER or image uploads."""

import csv
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from src.preprocessors import single_image_filter as f


COLUMNS = ["source", "text", "post_url", "image_paths", "image_count", "image_status"]


class SingleImageFilterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.input, self.output = self.root / "data/deduplicated", self.root / "data/single_image"
        self.input.mkdir(parents=True)
        self.images = self.root / "data/raw/facebook/images"
        self.images.mkdir(parents=True)
        (self.images / "one.png").touch()
        (self.images / "two.png").touch()

    def row(self, **changes):
        result = dict(zip(COLUMNS, ["Facebook", "BAHA sa Peñaplata", "https://facebook.com/Page/posts/123",
            "data/raw/facebook/images/one.png", "1", "downloaded"]))
        result.update(changes)
        return result

    def source(self, rows=None, stamp="10-20-2026_214749"):
        path = self.input / f"fb_deduplicate_floodData_{stamp}.csv"
        f._atomic_csv(path, COLUMNS, [self.row()] if rows is None else rows)
        return path

    def run_filter(self, **kwargs):
        with redirect_stdout(io.StringIO()):
            return f.filter_files(self.input, self.output, project_dir=self.root, **kwargs)

    def outputs(self):
        return sorted(self.output.glob("fb_*.csv"))

    def test_keeps_only_one_saved_image_and_preserves_all_cells(self):
        original = self.row(text="Short!\nUnicode Peñaplata", image_status="original-status")
        source = self.source([original, self.row(image_count="2", image_paths="data/raw/facebook/images/one.png;data/raw/facebook/images/two.png"),
                              self.row(image_count="0", image_paths="")])
        before = source.read_bytes()
        result = self.run_filter()[0]
        columns, rows, _ = f.read_source(self.outputs()[0])
        self.assertEqual(columns, COLUMNS)
        self.assertEqual(rows, [original])
        self.assertEqual((result["input_rows"], result["kept_rows"], result["excluded_rows"]), (3, 1, 2))
        self.assertEqual(source.read_bytes(), before)

    def test_reasons_for_bad_counts_mismatches_missing_and_unsafe_paths(self):
        for changes, reason in [({"image_count": "bad"}, "invalid_image_count"),
                                ({"image_count": "-1"}, "invalid_image_count"),
                                ({"image_count": "0"}, "image_count_path_mismatch"),
                                ({"image_paths": "missing.png"}, "missing_image_file"),
                                ({"image_paths": "../outside.png"}, "unsafe_image_path")]:
            self.assertEqual(f.decision(self.row(**changes), self.root), reason)

    def test_image_count_is_not_flood_content_classification(self):
        self.source([self.row(text="Weather advisory, not a flood photograph")])
        self.run_filter()
        self.assertEqual(len(f.read_source(self.outputs()[0])[1]), 1)

    def test_decisions_include_every_source_record_and_reason(self):
        self.source([self.row(), self.row(image_count="0", image_paths="")])
        self.run_filter()
        audit = next((self.output / "_metadata").glob("*_decisions.csv"))
        with audit.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual([r["image_filter_action"] for r in rows], ["keep", "exclude"])
        self.assertEqual([r["image_filter_source_row"] for r in rows], ["2", "3"])
        self.assertEqual(rows[1]["image_filter_reason"], "no_saved_image")

    def test_empty_and_all_excluded_files_get_headers(self):
        self.source([], "10-20-2026_212915")
        self.source([self.row(image_count="0", image_paths="")])
        self.run_filter()
        self.assertEqual(len(self.outputs()), 2)
        for path in self.outputs():
            columns, rows, _ = f.read_source(path)
            self.assertEqual((columns, rows), (COLUMNS, []))

    def test_chronological_order_and_next(self):
        first = self.source(stamp="12-31-2025_235959")
        second = self.source(stamp="01-01-2026_000000")
        self.assertEqual(self.run_filter(next_only=True)[0]["source_file"], first.name)
        self.assertEqual(self.run_filter(next_only=True)[0]["source_file"], second.name)
        self.assertEqual(self.run_filter(next_only=True), [])

    def test_rerun_leaves_outputs_and_audits_unchanged(self):
        self.source()
        self.run_filter()
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.output.rglob("*") if p.is_file()}
        self.assertEqual(self.run_filter(), [])
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before})

    def test_source_changes_require_explicit_overwrite(self):
        self.source()
        self.run_filter()
        self.source([self.row(text="Changed")])
        with self.assertRaisesRegex(ValueError, "overwrite"):
            self.run_filter()
        self.assertEqual(len(self.run_filter(overwrite=True)), 1)

    def test_missing_image_changes_invalidate_previous_decisions(self):
        self.source()
        self.run_filter()
        (self.images / "one.png").unlink()
        with self.assertRaisesRegex(ValueError, "overwrite"):
            self.run_filter()
        self.assertEqual(self.run_filter(overwrite=True)[0]["kept_rows"], 0)

    def test_edited_or_deleted_audit_is_not_trusted(self):
        self.source()
        self.run_filter()
        audit = next((self.output / "_metadata").glob("*_decisions.csv"))
        audit.unlink()
        with self.assertRaisesRegex(ValueError, "unaudited"):
            self.run_filter()

    def test_preserves_separate_records_that_share_an_image(self):
        self.source([self.row(), self.row(post_url="", text="Another record")])
        self.run_filter()
        summary = f.summarize(self.output)
        self.assertEqual((summary["kept_records"], summary["distinct_image_paths"], summary["records_without_post_url"]), (2, 1, 1))

    def test_input_and_protected_directories_cannot_be_outputs(self):
        self.source()
        for target in [self.input, self.input / "nested", self.root / "data", self.root / "data/ner", self.images]:
            with self.assertRaisesRegex(ValueError, "separate"):
                f.filter_files(self.input, target, project_dir=self.root)

    def test_malformed_or_reserved_headers_rejected(self):
        source = self.source()
        for columns, rows in [(["image_paths"], []),
                              (["image_count", "image_paths", "image_filter_action"], [])]:
            f._atomic_csv(source, columns, rows)
            with self.assertRaises(ValueError):
                self.run_filter()

    def test_manifest_failure_requires_recovery(self):
        self.source()
        original = f._atomic_csv
        def fail_manifest(path, columns, rows):
            if path.name == "single_image_manifest.csv":
                raise OSError("locked")
            return original(path, columns, rows)
        with patch.object(f, "_atomic_csv", side_effect=fail_manifest):
            with self.assertRaises(OSError):
                self.run_filter()
        with self.assertRaisesRegex(ValueError, "overwrite"):
            self.run_filter()
        self.assertEqual(len(self.run_filter(overwrite=True)), 1)


if __name__ == "__main__":
    unittest.main()
