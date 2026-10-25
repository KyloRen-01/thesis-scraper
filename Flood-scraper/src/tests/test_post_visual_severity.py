"""Offline tests for post_visual_severity and the keep rule in the Claude image classifier; no API requests are made."""

import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from src.image_classification import image_classifier as ic
from src.image_classification import image_classifier_config as cfg

ANCHORS = {"minor": "adult:ankle", "moderate": "adult:knee", "major": "adult:waist"}
NAME = "10-20-2026_202045"


def answer(level=None, *, valid=True, presence=True):
    """An Opus answer whose rubric severity is `level` (minor/moderate/major), or a flood without
    a depth anchor (level None), a dry scene (presence False), or not valid evidence."""
    anchors = [{"anchor": ANCHORS[level], "on_road": True}] if level else []
    return {"is_valid_flood_evidence": valid, "flood_presence": presence if valid else None,
            "anchors": anchors if valid else [], "impact_cues": [], "road_visible": valid,
            "road_blocked": False, "depth_anchor_evidence": "", "visual_description": "A street."}


DRY, INVALID = answer(presence=False), answer(valid=False)


def read_csv(path):
    return list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8-sig"), newline="")))


def records(*answers):
    return ic.post_records([ic.final_record(f"img_{n}.jpg", a)[0] for n, a in enumerate(answers, 1)])[0]


class FakeClaude(ic.Claude):
    """Answers from a fixed table keyed by image bytes, like Opus would."""

    def __init__(self, answers):
        super().__init__(cfg.MODEL, max_usd=0, client=object())
        self.answers = answers

    def observe(self, images):
        self.requests += 1
        return [self.answers[data] for data, _ in images], "claude-opus-5-5-test"


def run_classifier(project: Path, posts: list[list[dict]]) -> Path:
    """Classify one combined NER file whose posts have these Opus answers; return the output folder."""
    (project / "data/raw/facebook/downloaded_images").mkdir(parents=True)
    source = project / "data/ner/ner_datetime_locations"
    source.mkdir(parents=True)
    table, rows = {}, []
    for post_number, answers in enumerate(posts, 1):
        paths = []
        for number, image_answer in enumerate(answers, 1):
            data = b"\x89PNG\r\n\x1a\n" + f"post {post_number} image {number}".encode()
            path = f"data/raw/facebook/downloaded_images/fb_{post_number:016d}_{number:02d}.png"
            (project / path).write_bytes(data)
            table[data], paths = image_answer, paths + [path]
        rows.append({"source": "facebook", "page_source": "page", "text_content": f"Baha sa Davao {post_number}",
                     "datetime": "2026-10-20T08:00:00+08:00", "location": "Davao City",
                     "image_paths": ";".join(paths), "url": f"https://www.facebook.com/p/{post_number}"})
    with (source / f"fb_ner_datetime_locations_floodData_{NAME}.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, ic.INPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    output = project / "data/image_classified"
    with redirect_stdout(io.StringIO()):
        ic.classify_files(source, output, cache_path=project / "cache.sqlite3", claude=FakeClaude(table),
                          project_dir=project)
    return output


class PostVisualSeverityTests(unittest.TestCase):
    def test_users_example_takes_major_and_keeps_each_image_level(self):
        post = records(answer("moderate"), answer("minor"), answer("major"), answer("moderate"), answer("major"))
        self.assertEqual([r["visual_severity"] for r in post], ["moderate", "minor", "major", "moderate", "major"])
        self.assertEqual({r["post_visual_severity"] for r in post}, {"major"})
        self.assertEqual(ic.post_visual_severity(post)[1], "highest image severity: major (images 3, 5)")

    def test_rated_image_outranks_dry_and_unassessable_images(self):
        post = records(DRY, INVALID, answer(), answer("minor"))
        self.assertEqual({r["post_visual_severity"] for r in post}, {"minor"})
        self.assertEqual([r["visual_severity"] for r in post], ["no_flood", "not_assessable", "not_assessable", "minor"])

    def test_flood_without_depth_is_not_assessable_rather_than_no_flood(self):
        self.assertEqual({r["post_visual_severity"] for r in records(DRY, answer())}, {"not_assessable"})

    def test_dry_photo_with_a_graphic_is_no_flood(self):
        self.assertEqual({r["post_visual_severity"] for r in records(DRY, INVALID)}, {"no_flood"})

    def test_no_assessable_image_is_not_assessable(self):
        post = ic.post_records([ic.final_record("a.jpg", INVALID)[0], ic.unclassified_record("b.jpg")])[0]
        self.assertEqual({r["post_visual_severity"] for r in post}, {"not_assessable"})

    def test_post_level_below_an_image_level_is_refused(self):
        record = records(answer("major"))[0]
        for wrong in ("moderate", "no_flood", "not_assessable"):
            with self.assertRaises(RuntimeError):
                ic.check_record({**record, "post_visual_severity": wrong})
        with self.assertRaises(RuntimeError):  # Floodwater shown, so the post can't be no_flood.
            ic.check_record({**records(answer())[0], "post_visual_severity": "no_flood"})


class SchemaFieldOrderTests(unittest.TestCase):
    def test_records_follow_the_schema_field_order(self):
        self.assertEqual(list(records(answer("major"))[0]), list(cfg.ALLOWED_VALUES))

    def test_reordered_schema_reorders_the_records(self):
        reordered = dict(reversed(list(cfg.ALLOWED_VALUES.items())))
        with patch.object(cfg, "ALLOWED_VALUES", reordered):
            self.assertEqual(list(records(answer("major"))[0]), list(reordered))

    def test_schema_field_the_script_does_not_fill_is_refused(self):
        with patch.object(cfg, "ALLOWED_VALUES", {**cfg.ALLOWED_VALUES, "water_colour": ["brown"]}):
            with self.assertRaises(RuntimeError):
                records(answer("major"))


class PostSettingsTests(unittest.TestCase):
    def test_post_level_follows_the_config_order(self):
        # Leaving not_assessable out of the order makes flood-without-depth images not count.
        with patch.object(cfg, "POST_RANK", {"no_flood": 0, "minor": 1, "moderate": 2, "major": 3}):
            self.assertEqual({r["post_visual_severity"] for r in records(DRY, answer())}, {"no_flood"})

    def test_settings_the_schema_does_not_allow_are_refused(self):
        bad = [("POST_SEVERITY_ORDER", ["no_flood", "severe"]), ("POST_SEVERITY_ORDER", ["minor", "minor"]),
               ("POST_SEVERITY_ORDER", []), ("POST_SEVERITY_IF_NO_IMAGE_COUNTS", "unknown"),
               ("POST_SEVERITY_COUNTS_IF_TRUE", "visual_description")]
        for name, value in bad:
            with self.subTest(name=name, value=value), patch.object(cfg, name, value):
                with self.assertRaises(ValueError):
                    cfg.check_post_settings(cfg.ALLOWED_VALUES)
        without_post_field = {key: value for key, value in cfg.ALLOWED_VALUES.items() if key != "post_visual_severity"}
        with self.assertRaises(ValueError):
            cfg.check_post_settings(without_post_field)
        cfg.check_post_settings(cfg.ALLOWED_VALUES)  # The project's schema and settings agree.


class KeepRuleTests(unittest.TestCase):
    """The user's filter (restated 2026-10-25): drop a post when every picture has is_valid_flood_evidence
    false and flood_presence false or null; keep it when at least one picture has either set to true."""

    def test_post_whose_pictures_are_all_invalid_is_dropped(self):
        self.assertFalse(cfg.keep_post(records(INVALID, INVALID)))
        self.assertFalse(cfg.keep_post(ic.post_records([ic.unclassified_record("a.jpg")])[0]))
        self.assertFalse(cfg.keep_post([]))

    def test_one_valid_or_flooded_picture_keeps_the_post(self):
        self.assertTrue(cfg.keep_post(records(INVALID, answer("minor"))))  # flood_presence true
        self.assertTrue(cfg.keep_post(records(INVALID, answer())))  # floodwater without a depth anchor
        self.assertTrue(cfg.keep_post(records(INVALID, DRY)))  # a real photo without floodwater

    def test_classifier_writes_kept_and_excluded_posts_by_the_rule(self):
        with tempfile.TemporaryDirectory() as temp:
            output = run_classifier(Path(temp), [[INVALID, INVALID], [INVALID, DRY], [answer("major")], []])
            kept = read_csv(output / f"fb_image_classified_floodData_{NAME}.csv")
            excluded = read_csv(output / f"_metadata/fb_image_classified_floodData_{NAME}_excluded.csv")
            self.assertEqual([row["url"][-1] for row in kept], ["2", "3"])
            self.assertEqual({row["url"][-1]: row["exclusion_reason"] for row in excluded},
                             {"1": "no_flood_evidence", "4": "no_images"})


class ClassifyFileTests(unittest.TestCase):
    def test_output_and_evidence_carry_the_post_level(self):
        levels = ["moderate", "minor", "major", "moderate", "major"]
        with tempfile.TemporaryDirectory() as temp:
            output = run_classifier(Path(temp), [[answer(level) for level in levels]])
            rows = read_csv(output / f"fb_image_classified_floodData_{NAME}.csv")
            post = json.loads(rows[0][cfg.IMAGE_COLUMN])
            self.assertEqual([r["visual_severity"] for r in post], levels)
            self.assertEqual({r["post_visual_severity"] for r in post}, {"major"})
            self.assertEqual([list(r) for r in post], [list(cfg.ALLOWED_VALUES)] * len(levels))
            evidence = [json.loads(line) for line in (output / f"_metadata/fb_image_classified_floodData_{NAME}"
                                                      "_evidence.jsonl").read_text("utf-8").splitlines()]
            self.assertEqual([e["record"] for e in evidence], post)
            self.assertEqual({e["decision_basis"]["post_visual_severity"] for e in evidence},
                             {"highest image severity: major (images 3, 5)"})
            manifest = read_csv(output / "_metadata/manifest.csv")
            self.assertEqual(manifest[0]["version"], cfg.OUTPUT_VERSION)


if __name__ == "__main__":
    unittest.main()
