"""Offline tests for the PAGASA rule-based image classifier; no Gemini requests are made."""

import ast
import csv
import io
import json
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from google.genai import errors

from src.image_classification import image_classifier as ic

COLUMNS = ["source", "page_source", "text_content", "datetime", "location", "image_paths", "url"]


def observation(**changes):
    result = dict(flood_visible=True, image_kind="photo", water_extent="localized", water_at_entry_points=False,
                  structures_inundated=False, roads_impassable=False, evacuation="none",
                  description="Shallow water in a field.")
    result.update(changes)
    return result


PUBMAT = observation(flood_visible=False, image_kind="graphic", water_extent="none",
                     description="Flood advisory poster with Alert Level 3 text.")
MINOR = observation()
MODERATE = observation(water_extent="roads_or_near_structures", description="Water covers the road.")
MAJOR = observation(water_extent="widespread", structures_inundated=True, description="Houses submerged.")


class FakeObserver:
    """Answers with a fixed observation per image content, like Gemini would."""

    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def observe(self, image_bytes, mime_type):
        self.calls.append(image_bytes)
        answer = self.answers[image_bytes]
        if isinstance(answer, Exception):
            raise answer
        return answer, "gemini-test-001"


def write(path, columns, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def read(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def post(image_paths, url):
    return dict(zip(COLUMNS, ["Facebook", "Davao DRRMO", "baha sa davao city", "October 20, 2026",
                              "DAVAO CITY (citymun)", image_paths, url]))


class RuleTests(unittest.TestCase):
    def test_pagasa_scenarios(self):
        cases = [
            (PUBMAT, "No Flood"),
            (MINOR, "Minor Flooding"),
            (MODERATE, "Moderate Flooding"),
            (observation(water_at_entry_points=True), "Moderate Flooding"),
            (observation(evacuation="preparing"), "Moderate Flooding"),
            (MAJOR, "Major Flooding"),
            (observation(roads_impassable=True), "Major Flooding"),
            (observation(evacuation="forced_or_rescue"), "Major Flooding"),
            ({"unclassified_reason": "gemini_gave_no_valid_answer"}, ic.UNCLASSIFIED),
        ]
        for value, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(ic.severity(value), expected)

    def test_post_takes_the_highest_image_severity(self):
        self.assertEqual(ic.highest_severity(["Minor Flooding", "Major Flooding", "No Flood"]), "Major Flooding")
        self.assertEqual(ic.highest_severity([ic.UNCLASSIFIED, "Minor Flooding"]), "Minor Flooding")
        self.assertEqual(ic.highest_severity([ic.UNCLASSIFIED]), ic.UNCLASSIFIED)

    def test_array_lists_each_image_then_the_highest(self):
        text = ic.classification_array([("data/a.png", "No Flood"), ("data/b.png", "Moderate Flooding")])
        self.assertEqual(ast.literal_eval(text),
                         [("data/a.png", "No Flood"), ("data/b.png", "Moderate Flooding"), "Moderate Flooding"])

    def test_pubmat_with_a_flood_photo_is_kept_but_pubmat_only_is_not(self):
        posts = [dict(images=[dict(severity="No Flood"), dict(severity="Minor Flooding")]),
                 dict(images=[dict(severity="No Flood")]),
                 dict(images=[]),
                 dict(images=[dict(severity=ic.UNCLASSIFIED)])]
        kept, excluded = ic.keep_flood_posts(posts)
        self.assertEqual(kept, posts[:1])
        self.assertEqual([p["reason"] for p in excluded], ["no_flood_image", "no_images", "images_not_classified"])


class FileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.input = self.project / "ner"
        self.output = self.project / "classified"
        self.cache = self.project / "cache.sqlite3"
        images = self.project / "data/images"
        images.mkdir(parents=True)
        self.answers = {}
        for name, answer in [("pubmat", PUBMAT), ("minor", MINOR), ("moderate", MODERATE), ("major", MAJOR),
                             ("poster", PUBMAT)]:
            (images / f"{name}.png").write_bytes(name.encode())
            self.answers[name.encode()] = answer
        self.source = self.input / "fb_ner_datetime_locations_floodData_10-20-2026_230340.csv"
        write(self.source, COLUMNS, [
            post("data/images/pubmat.png;data/images/moderate.png", "https://www.facebook.com/a/posts/1"),
            post("data/images/poster.png", "https://www.facebook.com/a/posts/2"),
            post("", "https://www.facebook.com/a/posts/3"),
            post("data/images/minor.png;data/images/major.png;data/images/moderate.png",
                 "https://www.facebook.com/a/posts/4"),
        ])

    def run_classifier(self, observer, **options):
        with redirect_stdout(io.StringIO()):
            return ic.classify_files(self.input, self.output, cache_path=self.cache, observer=observer,
                                     project_dir=self.project, **options)

    def target(self):
        return self.output / "fb_image_classified_floodData_10-20-2026_230340.csv"

    def test_keeps_flood_posts_with_the_image_array(self):
        observer = FakeObserver(self.answers)
        self.assertEqual(len(self.run_classifier(observer)), 1)
        rows = read(self.target())
        self.assertEqual(list(rows[0]), COLUMNS + [ic.CLASSIFICATION_COLUMN])
        self.assertEqual([r["url"] for r in rows], ["https://www.facebook.com/a/posts/1", "https://www.facebook.com/a/posts/4"])
        self.assertEqual(ast.literal_eval(rows[0][ic.CLASSIFICATION_COLUMN]),
                         [("data/images/pubmat.png", "No Flood"), ("data/images/moderate.png", "Moderate Flooding"),
                          "Moderate Flooding"])
        self.assertEqual(ast.literal_eval(rows[1][ic.CLASSIFICATION_COLUMN])[-1], "Major Flooding")
        excluded = read(self.output / "_metadata" / (self.target().stem + "_excluded.csv"))
        self.assertEqual([r["exclusion_reason"] for r in excluded], ["no_flood_image", "no_images"])
        details = read(self.output / "_metadata" / (self.target().stem + "_details.csv"))
        self.assertEqual(len(details), 6)
        major = next(r for r in details if r["image_path"].endswith("major.png"))
        self.assertEqual((major["alert_level"], major["color_code"], major["post_kept"]), ("Alert Level 3", "Red", "true"))
        # moderate.png appears in two posts but is sent to Gemini once.
        self.assertEqual(len(observer.calls), 5)

    def test_reruns_skip_and_overwrite_reuses_cached_observations(self):
        self.run_classifier(FakeObserver(self.answers))
        before = self.target().read_bytes()
        observer = FakeObserver(self.answers)
        self.assertEqual(self.run_classifier(observer), [])
        self.assertEqual(len(self.run_classifier(observer, overwrite=True)), 1)
        self.assertEqual(observer.calls, [])
        self.assertEqual(self.target().read_bytes(), before)

    def test_changed_input_requires_overwrite(self):
        self.run_classifier(FakeObserver(self.answers))
        write(self.source, COLUMNS, [post("data/images/minor.png", "https://www.facebook.com/a/posts/9")])
        with self.assertRaisesRegex(ValueError, "--overwrite"):
            self.run_classifier(FakeObserver(self.answers))

    def test_quota_stop_saves_nothing_partial_and_resumes_from_cache(self):
        answers = dict(self.answers)
        answers[b"minor"] = ic.QuotaExhausted("daily quota")
        with self.assertRaises(ic.QuotaExhausted):
            self.run_classifier(FakeObserver(answers))
        self.assertFalse(self.target().exists())
        observer = FakeObserver(self.answers)
        self.assertEqual(len(self.run_classifier(observer)), 1)
        self.assertEqual(observer.calls, [b"minor", b"major"])

    def test_missing_image_file_is_unclassified(self):
        write(self.source, COLUMNS, [post("data/images/gone.png", "https://www.facebook.com/a/posts/5")])
        self.run_classifier(FakeObserver(self.answers))
        excluded = read(self.output / "_metadata" / (self.target().stem + "_excluded.csv"))
        self.assertEqual(excluded[0]["exclusion_reason"], "images_not_classified")
        self.assertEqual(ast.literal_eval(excluded[0][ic.CLASSIFICATION_COLUMN]),
                         [("data/images/gone.png", ic.UNCLASSIFIED), ic.UNCLASSIFIED])


class GeminiObserverTests(unittest.TestCase):
    def response(self, value=MINOR):
        return types.SimpleNamespace(parsed=ic.Observation(**value), text=json.dumps(value), model_version="gemini-x")

    def observer(self, outcomes, **options):
        calls = []

        def generate_content(**kwargs):
            calls.append(kwargs)
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        client = types.SimpleNamespace(models=types.SimpleNamespace(generate_content=generate_content))
        return ic.GeminiObserver("gemini-test", requests_per_minute=0, client=client, **options), calls

    @staticmethod
    def error(code, status):
        return errors.ClientError(code, {"error": {"code": code, "message": status.lower(), "status": status}})

    def test_rate_limits_are_retried_then_succeed(self):
        observer, calls = self.observer([self.error(429, "RESOURCE_EXHAUSTED"), self.response()])
        with patch.object(ic.time, "sleep"):
            self.assertEqual(observer.observe(b"x", "image/png"), (MINOR, "gemini-x"))
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["config"].response_schema, ic.Observation)

    def test_persistent_rate_limit_stops_the_run(self):
        observer, _ = self.observer([self.error(429, "RESOURCE_EXHAUSTED")] * ic.RETRIES)
        with patch.object(ic.time, "sleep"), self.assertRaises(ic.QuotaExhausted):
            observer.observe(b"x", "image/png")

    def test_invalid_key_or_unknown_model_stops_instead_of_labeling_images(self):
        for code, status, message in [(400, "INVALID_ARGUMENT", "400"), (404, "NOT_FOUND", "--model")]:
            with self.subTest(code=code):
                observer, _ = self.observer([self.error(code, status)])
                with self.assertRaisesRegex(ic.GeminiUnavailable, message):
                    observer.observe(b"x", "image/png")

    def test_request_cap_and_missing_key(self):
        observer, _ = self.observer([self.response()], max_requests=1)
        observer.observe(b"x", "image/png")
        with self.assertRaisesRegex(ic.QuotaExhausted, "max-requests"):
            observer.observe(b"x", "image/png")
        with patch.dict(ic.os.environ, {}, clear=True), self.assertRaisesRegex(ic.GeminiUnavailable, "GEMINI_API_KEY"):
            ic.GeminiObserver("gemini-test").client()

    def test_blocked_or_empty_answer_is_unclassified(self):
        empty = types.SimpleNamespace(parsed=None, text="", model_version="gemini-x")
        self.assertEqual(ic.GeminiObserver.parse(empty), {"unclassified_reason": "gemini_gave_no_valid_answer"})


if __name__ == "__main__":
    unittest.main()
