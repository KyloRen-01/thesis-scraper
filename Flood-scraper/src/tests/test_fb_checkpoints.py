"""Verify saved-post checkpoints across restarts and failed writes."""

import tempfile
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

from src.scraper import fb_flood_scraper as fb


class FacebookCheckpointTests(unittest.TestCase):
    def test_restart_imports_history_and_skips_old_posts_before_image_capture(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            raw = root / "facebook"
            checkpoint_path = root / "checkpoints" / "posts.sqlite3"
            old = {"post_url": "https://www.facebook.com/page/posts/123/", "text": "Baha in Davao"}
            fb.save_results([old], raw / "historical.csv")
            old_article, new_article = object(), object()
            urls = {
                old_article: "https://m.facebook.com/page/posts/123?tracking=changed",
                new_article: "https://www.facebook.com/page/posts/456",
            }
            driver = MagicMock()
            driver.find_elements.return_value = [old_article, new_article]
            first_output = raw / "first_run.csv"
            second_output = raw / "second_run.csv"

            with (
                patch.object(fb, "RAW_DIR", raw),
                patch.object(fb, "FACEBOOK", {"users": ["page"], "search_queries": []}),
                patch.object(fb, "init_driver", return_value=driver),
                patch.object(fb, "scroll_page"),
                patch.object(fb.time, "sleep"),
                patch.object(fb, "extract_post_url", side_effect=lambda _driver, article: urls[article]),
                patch.object(fb, "extract_caption", return_value="Baha in Davao") as captions,
                patch.object(fb, "extract_username", return_value="Example Page"),
                patch.object(fb, "extract_post_time", return_value=None),
                patch.object(fb, "collect_article_images", return_value=(
                    "data/raw/facebook/images/new.png", "downloaded", 1,
                )) as images,
            ):
                first = fb.scrape_facebook(output_path=first_output, checkpoint_path=checkpoint_path)
                self.assertEqual(len(first), 1)
                self.assertEqual(captions.call_count, 1)
                self.assertEqual(images.call_count, 1)
                second = fb.scrape_facebook(output_path=second_output, checkpoint_path=checkpoint_path)

            self.assertEqual(len(second), 0)
            self.assertEqual(images.call_count, 1)
            self.assertEqual(captions.call_count, 1)
            self.assertEqual(pd.read_csv(first_output)["image_count"].tolist(), [1])
            self.assertEqual(len(pd.read_csv(second_output)), 0)
            with closing(fb.FacebookPostCheckpoint(checkpoint_path)) as checkpoint:
                self.assertEqual(checkpoint.saved_keys(), {"facebook:post:123", "facebook:post:456"})

    def test_failed_csv_replace_does_not_mark_posts_saved(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "posts.csv"
            row = {"post_url": "https://www.facebook.com/page/posts/123", "text": "Baha in Davao"}
            with closing(fb.FacebookPostCheckpoint(root / "posts.sqlite3")) as checkpoint:
                with patch.object(Path, "replace", side_effect=OSError("file is locked")):
                    with self.assertRaises(OSError):
                        fb.save_results([row], output, post_checkpoint=checkpoint)
                self.assertEqual(checkpoint.saved_keys(), set())
                fb.save_results([row], None, post_checkpoint=checkpoint)
                self.assertEqual(checkpoint.saved_keys(), set())
                fb.save_results([row], output, post_checkpoint=checkpoint)
                self.assertEqual(checkpoint.saved_keys(), {"facebook:post:123"})

    def test_posts_without_urls_match_saved_author_and_caption(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            row = {"user": "Example Page", "text": "Baha  in Davao. See less", "post_timestamp": "Unknown"}
            with closing(fb.FacebookPostCheckpoint(root / "posts.sqlite3")) as checkpoint:
                fb.save_results([row], root / "posts.csv", post_checkpoint=checkpoint)
            with closing(fb.FacebookPostCheckpoint(root / "posts.sqlite3")) as checkpoint:
                known = checkpoint.saved_keys()
            self.assertIn(fb.saved_post_key({"user": "EXAMPLE PAGE", "text": "Baha in Davao."}), known)
            self.assertNotIn(fb.saved_post_key({"user": "Another Page", "text": "Baha in Davao."}), known)
            driver = MagicMock()
            driver.find_elements.return_value = [object()]
            results = []
            with (
                patch.object(fb, "extract_post_url", return_value=""),
                patch.object(fb, "extract_caption", return_value="Baha in Davao."),
                patch.object(fb, "extract_username", return_value="Example Page"),
                patch.object(fb, "collect_article_images") as images,
            ):
                fb.process_fb_articles(driver, results, known, "page", False, None, None, "2026-10-20")
            self.assertEqual(results, [])
            images.assert_not_called()

    def test_csv_recovers_checkpoint_after_database_write_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "posts.sqlite3"
            row = {"post_url": "https://www.facebook.com/page/posts/123", "text": "Baha in Davao"}
            with closing(fb.FacebookPostCheckpoint(path)) as checkpoint:
                with patch.object(checkpoint, "record_saved_rows", side_effect=sqlite3.OperationalError("locked")):
                    with self.assertRaises(sqlite3.OperationalError):
                        fb.save_results([row], root / "posts.csv", post_checkpoint=checkpoint)
                self.assertEqual(checkpoint.saved_keys(), set())
            with closing(fb.FacebookPostCheckpoint(path)) as checkpoint:
                checkpoint.import_history(root)
                self.assertEqual(checkpoint.saved_keys(), {"facebook:post:123"})

    def test_saved_posts_count_toward_50_inspected_posts(self):
        driver = MagicMock()
        driver.find_elements.return_value = [object() for _ in range(60)]
        known = {f"facebook:post:{number}" for number in range(60)}
        examined = set()
        with (
            patch.object(fb, "extract_post_url", side_effect=[
                f"https://www.facebook.com/page/posts/{number}" for number in range(60)
            ]) as links,
            patch.object(fb, "extract_caption") as captions,
            patch.object(fb, "collect_article_images") as images,
        ):
            results = []
            fb.process_fb_articles(
                driver, results, known, "page", False, None, None, "2026-10-20",
                seen_feed_articles=examined, max_posts_to_scan=50,
            )
        self.assertEqual(len(examined), 50)
        self.assertEqual(links.call_count, 50)
        self.assertEqual(results, [])
        captions.assert_not_called()
        images.assert_not_called()

    def test_reel_and_permalink_url_variants_use_post_ids(self):
        self.assertEqual(
            fb.facebook_url_key("https://www.facebook.com/reel/123/?tracking=1"),
            fb.facebook_url_key("https://m.facebook.com/page/videos/123"),
        )
        self.assertEqual(
            fb.facebook_url_key("https://www.facebook.com/permalink.php?story_fbid=123&id=999"),
            fb.facebook_url_key("https://web.facebook.com/page/posts/123#comments"),
        )
        self.assertEqual(fb.facebook_url_key("https://www.facebook.com/search/posts/?q=flood"), "")


if __name__ == "__main__":
    unittest.main()
