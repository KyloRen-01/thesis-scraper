"""Focused checks for the combined Facebook post and image output."""

import io
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

from src.scraper import fb_flood_scraper as fb
from src.scraper import fb_image_download as images


class FacebookIntegrationTests(unittest.TestCase):
    def test_terminal_shows_only_running_post_count(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "posts.csv"
            terminal = io.StringIO()

            def fake_scrape(**kwargs):
                kwargs["on_progress"](2)
                return pd.DataFrame([{"source": "Facebook"}] * 2, columns=fb.OUTPUT_COLUMNS)

            with (
                patch("sys.argv", ["fb_flood_scraper.py", "--output", str(output_path)]),
                patch.object(fb, "scrape_facebook", side_effect=fake_scrape),
                redirect_stdout(terminal),
            ):
                self.assertEqual(fb.main(), 0)

            self.assertEqual(terminal.getvalue(), "\rPosts scraped: 0\rPosts scraped: 2\n")
            self.assertEqual(len(pd.read_csv(output_path)), 2)

    def test_plain_command_uses_configured_run_defaults(self):
        with patch("sys.argv", ["fb_flood_scraper.py"]):
            args = fb.parse_args()
        self.assertTrue(args.headed)
        self.assertEqual(args.total_time_limit, 7200.0)
        self.assertEqual(args.max_images, 8)
        self.assertEqual(args.max_posts_per_feed, 50)
        self.assertEqual(args.checkpoint_file, fb.DEFAULT_CHECKPOINT_PATH)
        self.assertIsNone(args.hours_page)
        self.assertIsNone(args.hours_search)

        with patch("sys.argv", ["fb_flood_scraper.py", "--headless", "--total-time-limit", "60", "--max-posts-per-feed", "25"]):
            overridden = fb.parse_args()
        self.assertFalse(overridden.headed)
        self.assertEqual(overridden.total_time_limit, 60.0)
        self.assertEqual(overridden.max_posts_per_feed, 25)

    def test_each_feed_stops_at_50_posts_and_retains_images_in_one_csv(self):
        driver = MagicMock()
        driver.find_elements.return_value = [object() for _ in range(60)]
        results = []
        seen_urls = set()
        urls = [f"https://www.facebook.com/page/posts/{number}" for number in range(120)]

        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "posts.csv"
            with (
                patch.object(fb, "extract_post_url", side_effect=urls),
                patch.object(fb, "extract_caption", return_value="Flood in Davao"),
                patch.object(fb, "extract_post_time", return_value=None),
                patch.object(fb, "extract_username", return_value="Example Page"),
                patch.object(fb, "collect_article_images", return_value=(
                    "data/raw/facebook/images/example.png", "downloaded", 1,
                )) as capture_images,
                patch.object(fb, "scroll_page") as scroll,
                patch.object(fb.time, "sleep"),
            ):
                for identifier in ("page-one", "page-two"):
                    fb.scrape_feed(
                        driver, results, seen_urls,
                        url=f"https://www.facebook.com/{identifier}/posts",
                        identifier=identifier,
                        is_search_query=False,
                        since_page=None,
                        since_query=None,
                        scraped_timestamp="2026-10-20 12:00",
                        scroll_times=80,
                        scroll_pause=1.5,
                        stalled_scrolls=5,
                        deadline=time.monotonic() + 60,
                        output_path=output,
                        feed_type="fb_page",
                        max_images=8,
                        max_posts_per_feed=50,
                    )

            scroll.assert_not_called()
            self.assertEqual(capture_images.call_count, 100)
            self.assertEqual(len(results), 100)
            saved = pd.read_csv(output)
            self.assertEqual(saved["query_page"].value_counts().to_dict(), {
                "page-one": 50, "page-two": 50,
            })
            self.assertEqual(saved["image_count"].tolist(), [1] * 100)

    def test_feed_counts_nonmatching_posts_toward_50_post_limit(self):
        driver = MagicMock()
        driver.find_elements.return_value = [object() for _ in range(60)]
        results = []

        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "posts.csv"
            with (
                patch.object(fb, "extract_post_url", side_effect=[
                    f"https://www.facebook.com/page/posts/{number}" for number in range(60)
                ]) as post_links,
                patch.object(fb, "extract_caption", side_effect=[
                    "Weather update in Davao" for _ in range(49)
                ] + ["Flood in Davao" for _ in range(11)]),
                patch.object(fb, "extract_post_time", return_value=None),
                patch.object(fb, "extract_username", return_value="Example Page"),
                patch.object(fb, "collect_article_images", return_value=(
                    "data/raw/facebook/images/example.png", "downloaded", 1,
                )) as capture_images,
                patch.object(fb, "scroll_page") as scroll,
                patch.object(fb.time, "sleep"),
            ):
                fb.scrape_feed(
                    driver, results, set(),
                    url="https://www.facebook.com/page/posts",
                    identifier="page",
                    is_search_query=False,
                    since_page=None,
                    since_query=None,
                    scraped_timestamp="2026-10-20 12:00",
                    scroll_times=80,
                    scroll_pause=1.5,
                    stalled_scrolls=5,
                    deadline=time.monotonic() + 60,
                    output_path=output,
                    feed_type="fb_page",
                    max_images=8,
                    max_posts_per_feed=50,
                )

            self.assertEqual(post_links.call_count, 50)
            scroll.assert_not_called()
            self.assertEqual(capture_images.call_count, 1)
            self.assertEqual(len(results), 1)
            self.assertEqual(pd.read_csv(output)["image_count"].tolist(), [1])

    def test_repeated_articles_across_batches_are_counted_once(self):
        driver = MagicMock()
        articles = [object() for _ in range(60)]
        driver.find_elements.side_effect = [articles[:30], articles]
        urls = {
            article: f"https://www.facebook.com/page/posts/{number}"
            for number, article in enumerate(articles)
        }
        results = []

        def one_more_batch(_driver, *args, **kwargs):
            kwargs["on_batch"]()
            self.assertTrue(kwargs["stop_when"]())

        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "posts.csv"
            with (
                patch.object(fb, "extract_post_url", side_effect=lambda _driver, article: urls[article]) as post_links,
                patch.object(fb, "extract_caption", return_value="Weather update in Davao"),
                patch.object(fb, "scroll_page", side_effect=one_more_batch) as scroll,
                patch.object(fb, "collect_article_images") as capture_images,
                patch.object(fb.time, "sleep"),
            ):
                fb.scrape_feed(
                    driver, results, set(),
                    url="https://www.facebook.com/page/posts",
                    identifier="page",
                    is_search_query=False,
                    since_page=None,
                    since_query=None,
                    scraped_timestamp="2026-10-20 12:00",
                    scroll_times=80,
                    scroll_pause=1.5,
                    stalled_scrolls=5,
                    deadline=time.monotonic() + 60,
                    output_path=output,
                    feed_type="fb_page",
                    max_images=8,
                    max_posts_per_feed=50,
                )

            self.assertEqual(post_links.call_count, 80)
            scroll.assert_called_once()
            capture_images.assert_not_called()
            self.assertEqual(len(results), 0)
            self.assertEqual(len(pd.read_csv(output)), 0)

    def test_scrolling_stops_after_feed_reaches_its_post_limit(self):
        driver = MagicMock()
        driver.execute_script.return_value = 100
        batches = []
        with patch.object(fb.time, "sleep"):
            fb.scroll_page(
                driver, scroll_times=80, pause=0.01, max_stalled_scrolls=5,
                on_batch=lambda: batches.append(1),
                stop_when=lambda: len(batches) == 1,
            )
        self.assertEqual(len(batches), 1)
        self.assertEqual(
            sum("window.scrollTo" in call.args[0] for call in driver.execute_script.call_args_list),
            3,
        )

    def test_location_filter_does_not_confuse_information_with_mati(self):
        self.assertFalse(fb.mentions_ph_location("Flood information for another country"))
        self.assertTrue(fb.mentions_ph_location("#DavaoCity flood alert"))

    def test_senior_location_references_include_provinces(self):
        references = fb.load_ph_locations()
        self.assertIn("DAVAO DE ORO", references)
        # data/lookup now covers Davao Region only.
        self.assertNotIn("NUEVA ECIJA", references)
        self.assertTrue(fb.mentions_ph_location("Baha sa Davao de Oro"))

    def test_reference_matched_post_keeps_its_image_fields(self):
        driver = MagicMock()
        driver.find_elements.return_value = [MagicMock()]
        results = []
        with (
            patch.object(fb, "extract_post_url", return_value=""),
            patch.object(fb, "extract_caption", return_value="Baha sa Kapalong"),
            patch.object(fb, "extract_post_time", return_value=None),
            patch.object(fb, "extract_username", return_value="Example Page"),
            patch.object(fb, "collect_article_images", return_value=(
                "data/raw/facebook/images/example.png", "downloaded", 1,
            )),
        ):
            fb.process_fb_articles(driver, results, set(), "test-feed", False, None, None, "2026-10-20 12:00")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["image_paths"], "data/raw/facebook/images/example.png")
        self.assertEqual(results[0]["image_count"], 1)

    def test_nested_caption_block_is_not_saved_twice(self):
        post = MagicMock()
        post.find_elements.return_value = [
            MagicMock(text="Flood in Davao. See less"),
            MagicMock(text="Flood in Davao."),
        ]
        with patch.object(fb, "expand_see_more"):
            self.assertEqual(fb.extract_caption(MagicMock(), post), "Flood in Davao.")
        post.find_elements.return_value.reverse()
        with patch.object(fb, "expand_see_more"):
            self.assertEqual(fb.extract_caption(MagicMock(), post), "Flood in Davao.")

    def test_author_falls_back_to_page_handle_from_post_link(self):
        article = MagicMock()
        article.find_elements.return_value = []
        self.assertEqual(
            fb.extract_username(article, "https://www.facebook.com/DavaoDRRMO/posts/pfbid123"),
            "DavaoDRRMO",
        )

    def test_post_link_validation_rejects_feed_and_empty_post_paths(self):
        self.assertTrue(fb.is_unique_post_url("https://www.facebook.com/DavaoDRRMO/posts/pfbid123"))
        self.assertFalse(fb.is_unique_post_url("https://www.facebook.com/DavaoDRRMO/posts/"))
        self.assertFalse(fb.is_unique_post_url("https://www.facebook.com/search/posts/?q=flood"))

    def test_missing_post_links_keep_distinct_articles_and_image_results(self):
        articles = [object(), object()]
        driver = MagicMock()
        driver.find_elements.return_value = articles
        results = []
        progress = []

        with (
            patch.object(fb, "extract_post_url", return_value=""),
            patch.object(fb, "extract_caption", side_effect=["Baha in Davao", "Flood in Manila"]),
            patch.object(fb, "extract_username", return_value="Unknown"),
            patch.object(fb, "extract_post_time", return_value=None),
            patch.object(fb, "collect_article_images", side_effect=[
                ("data/raw/facebook/images/a.png", "downloaded", 1),
                ("", "no_image_found", 0),
            ]) as capture,
        ):
            fb.process_fb_articles(
                driver, results, set(), "test-feed", False, None, None,
                "2026-10-20 12:00", max_images=8, on_progress=progress.append,
            )

        self.assertEqual(len(results), 2)
        self.assertEqual(progress, [1, 2])
        self.assertEqual([row["post_url"] for row in results], ["", ""])
        self.assertEqual([row["image_count"] for row in results], [1, 0])
        self.assertEqual(capture.call_count, 2)
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "posts.csv"
            fb.save_results(results, output)
            saved = pd.read_csv(output)
            self.assertEqual(saved["image_status"].tolist(), ["downloaded", "no_image_found"])

    def test_unknown_time_does_not_pass_recent_cutoff(self):
        driver = MagicMock()
        driver.find_elements.return_value = [object()]
        results = []
        with (
            patch.object(fb, "extract_post_url", return_value=""),
            patch.object(fb, "extract_caption", return_value="Baha in Davao"),
            patch.object(fb, "extract_username", return_value="Example Page"),
            patch.object(fb, "extract_post_time", return_value=None),
            patch.object(fb, "collect_article_images") as capture,
        ):
            fb.process_fb_articles(
                driver, results, set(), "test-feed", False,
                datetime.now(timezone(timedelta(hours=8))) - timedelta(days=1),
                None, "2026-10-20 12:00",
            )
        self.assertEqual(results, [])
        capture.assert_not_called()

    def test_modern_time_element_is_parsed_in_philippine_time(self):
        post = MagicMock()
        element = MagicMock()
        element.get_attribute.side_effect = lambda name: {
            "data-utime": None, "datetime": "2026-10-20T04:30:00Z"
        }.get(name)
        post.find_elements.return_value = [element]
        self.assertEqual(fb.format_readable(fb.extract_post_time(post)), "2026-10-20 12:30:00")

    def test_inline_images_come_from_the_matched_article_in_a_second_tab(self):
        driver, article = MagicMock(), MagicMock()
        media = {"photos": ["https://www.facebook.com/photo/?fbid=1&set=pcb.9"], "tiles": ["https://cdn.example/t.jpg"], "images": []}
        driver.execute_script.return_value = media
        with patch.object(fb, "download_media", return_value={"paths": [], "sources": [], "status": "no_image_found"}) as download:
            fields = fb.collect_article_images(driver, article, "https://www.facebook.com/page/posts/123", max_images=8)
        self.assertIs(driver.execute_script.call_args.args[1], article)
        self.assertEqual(download.call_args.args[1:], (
            media, fb.IMAGE_DIR / f"fb_{fb.safe_hash('https://www.facebook.com/page/posts/123')}", 8,
        ))
        self.assertTrue(download.call_args.kwargs["new_tab"])
        self.assertEqual(fields, ("", "no_image_found", 0))

    def test_downloaded_image_file_and_csv_fields_are_created_in_one_path(self):
        image = b"\xff\xd8\xff" + b"x" * 300
        response = MagicMock(status_code=200, headers={"content-type": "image/jpeg"}, content=image)
        driver = MagicMock()
        driver.execute_script.return_value = {"photos": [], "tiles": [], "images": ["https://cdn.example/a.jpg?oh=1&oe=2"]}
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(fb, "IMAGE_DIR", Path(temp_dir)),
                patch.object(fb, "relative_project_path", side_effect=lambda path: path.name),
                patch.object(images.requests, "get", return_value=response) as get,
            ):
                paths, status, count = fb.collect_article_images(
                    driver, MagicMock(), "https://www.facebook.com/page/posts/123", max_images=8
                )
            self.assertEqual((status, count), ("downloaded", 1))
            self.assertTrue(paths.endswith("_01.jpg"))
            self.assertEqual((Path(temp_dir) / paths).read_bytes(), image)
            # The signed query string is kept; Facebook refuses the image without it.
            self.assertEqual(get.call_args.args[0], "https://cdn.example/a.jpg?oh=1&oe=2")

    def test_csv_header_exists_even_if_browser_startup_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "posts.csv"
            with patch.object(fb, "init_driver", side_effect=RuntimeError("Chrome unavailable")):
                with self.assertRaises(RuntimeError):
                    fb.scrape_facebook(output_path=output, checkpoint_path=None)
            self.assertEqual(pd.read_csv(output).columns.tolist(), fb.OUTPUT_COLUMNS)


class FacebookImageRetryTests(unittest.TestCase):
    def row(self, number, status, url=None, text="Baha sa Davao"):
        return {
            "source": "Facebook", "query_page": "page", "text": text,
            "post_timestamp": "Unknown", "scraped_timestamp": "2026-10-20 21:00", "user": "Example Page",
            "post_url": url or f"https://www.facebook.com/page/posts/{number}",
            "image_paths": "data/raw/facebook/images/old.png" if status == "downloaded" else "",
            "image_count": 1 if status == "downloaded" else 0,
            "image_status": status,
        }

    def test_retry_changes_only_rows_that_gain_images(self):
        attempts = {
            "https://www.facebook.com/page/posts/2": ("data/raw/facebook/images/fb_new_01.png", "downloaded", 1),
            "https://www.facebook.com/page/posts/4": ("", "no_image_found", 0),
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            first, second = Path(temp_dir) / "first.csv", Path(temp_dir) / "second.csv"
            fb.save_results([
                self.row(1, "downloaded"),
                self.row(2, "no_image_found"),
                self.row(3, "no_image_found", url="https://www.facebook.com/page"),
                self.row(4, "no_image_found", text="NA"),
            ], first)
            fb.save_results([self.row(4, "not_checked_time_limit")], second)
            first_lines = first.read_text(encoding="utf-8-sig").splitlines()
            second_bytes = second.read_bytes()

            with patch.object(fb, "collect_post_page_images", side_effect=lambda _driver, url, **_: attempts[url]) as visit:
                updated = fb.add_missing_images(MagicMock(), [first, second], max_images=8)

            self.assertEqual(updated, 1)
            # Each post is opened once; the repeated link in the second CSV reuses the attempt.
            self.assertEqual([call.args[1] for call in visit.call_args_list], list(attempts))
            self.assertEqual(second.read_bytes(), second_bytes)
            lines = first.read_text(encoding="utf-8-sig").splitlines()
            self.assertEqual([lines[index] for index in (0, 1, 3, 4)], [first_lines[index] for index in (0, 1, 3, 4)])
            changed = pd.read_csv(first, keep_default_na=False).iloc[1]
            self.assertEqual(
                (changed["image_paths"], changed["image_count"], changed["image_status"]),
                ("data/raw/facebook/images/fb_new_01.png", 1, "downloaded"),
            )
            self.assertEqual(pd.read_csv(first, keep_default_na=False)["text"].iloc[3], "NA")
            self.assertEqual(sorted(path.name for path in Path(temp_dir).iterdir()), ["first.csv", "second.csv"])

    def test_retry_opens_the_post_and_finds_it_by_caption(self):
        driver = MagicMock()
        driver.current_url = "https://www.facebook.com/page/posts/2"
        with patch.object(images, "find_post_media", return_value=None) as find:
            fields = fb.collect_post_page_images(
                driver, "https://www.facebook.com/page/posts/2", max_images=8, caption="Baha sa Davao"
            )
        driver.get.assert_called_once_with("https://www.facebook.com/page/posts/2")
        find.assert_called_once_with(driver, "Baha sa Davao")
        self.assertEqual(fields, ("", "post_not_found", 0))

    def test_login_page_stops_the_retry_with_a_message(self):
        driver = MagicMock()
        driver.current_url = "https://www.facebook.com/login/?next=%2Fpage%2Fposts%2F2"
        with patch.object(fb.time, "sleep"), self.assertRaisesRegex(fb.FacebookLoginRequired, "--login"):
            fb.collect_post_page_images(driver, "https://www.facebook.com/page/posts/2", max_images=8)

        errors = io.StringIO()
        with (
            patch("sys.argv", ["fb_flood_scraper.py", "--retry-images"]),
            patch.object(fb, "retry_images", side_effect=fb.FacebookLoginRequired("Facebook showed a login page")),
            redirect_stdout(io.StringIO()),
            redirect_stderr(errors),
        ):
            self.assertEqual(fb.main(), 1)
        self.assertIn("[ERROR] Facebook showed a login page", errors.getvalue())

    def test_retry_images_option_runs_instead_of_feed_scraping(self):
        terminal = io.StringIO()

        def fake_retry(**kwargs):
            kwargs["on_progress"](3)
            return 3

        with (
            patch("sys.argv", ["fb_flood_scraper.py", "--retry-images", "--max-images", "4"]),
            patch.object(fb, "retry_images", side_effect=fake_retry) as retry,
            patch.object(fb, "scrape_facebook") as scrape,
            redirect_stdout(terminal),
        ):
            self.assertEqual(fb.main(), 0)
        scrape.assert_not_called()
        self.assertEqual(retry.call_args.kwargs["max_images"], 4)
        self.assertEqual(terminal.getvalue(), "\rRows given images: 0\rRows given images: 3\n")

        with (
            patch("sys.argv", ["fb_flood_scraper.py", "--retry-images", "--max-images", "0"]),
            redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            fb.parse_args()


if __name__ == "__main__":
    unittest.main()
