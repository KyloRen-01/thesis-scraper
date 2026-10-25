"""Verify that post images are found in the right post and saved as downloaded files."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.scraper import fb_image_download as images

SET = "pcb.900"


class FakeViewer:
    """Facebook's photo viewer for one multi-photo post, looping back to the first photo."""

    def __init__(self, fbids):
        self.fbids, self.index = fbids, 0
        self.opened = []

    @property
    def current_url(self):
        return f"https://www.facebook.com/photo/?fbid={self.fbids[self.index]}&set={SET}"

    def get(self, url):
        self.opened.append(url)
        self.index = self.fbids.index(images.photo_id(url)[0])

    def find_elements(self, *_):
        return ["next"]

    def execute_script(self, script, *_):
        if script == images.VIEWER_IMAGE:
            return f"https://cdn.example/{self.fbids[self.index]}.jpg?oh=1"
        self.index = (self.index + 1) % len(self.fbids)  # The "Next photo" click.


def link(fbid):
    return f"https://www.facebook.com/photo/?fbid={fbid}&set={SET}&__cft__[0]=x"


class FacebookImageDownloadTests(unittest.TestCase):
    def test_cleaned_caption_matches_the_post_text_on_the_page(self):
        caption = "baha sa davao city heavy rainfall warning no 2 issued at 08:00 pm"
        page = "City Government\n𝗛𝗲𝗮𝘃𝘆 𝗥𝗮𝗶𝗻𝗳𝗮𝗹𝗹 𝗪𝗮𝗿𝗻𝗶𝗻𝗴 No. 2 #BahaSaDavao City. Issued at 08:00 PM"
        self.assertEqual(images.caption_score(images.key_words(caption), page), 1.0)

    def test_the_opened_post_in_the_dialog_wins_over_the_page_behind_it(self):
        behind, opened = MagicMock(), MagicMock()
        caption = "Flooding occurred on Avenue Street in Green Meadows Subdivision"
        driver = MagicMock()
        driver.execute_script.side_effect = lambda script, *args: (
            [[behind, caption, False], [opened, f"SunStar Davao\n{caption}", True]]
            if script == images.POST_TEXTS else {"photos": [link(1)], "tiles": ["t"], "images": []}
        )
        with patch.object(images.time, "sleep"):
            media = images.find_post_media(driver, caption)
        self.assertEqual(media["photos"], [link(1)])
        self.assertIs(driver.execute_script.call_args.args[1], opened)

    def test_search_results_need_the_same_author_and_a_close_caption(self):
        caption = "Flooding occurred on Avenue Street in Green Meadows Subdivision Barangay Tugbok"
        other_page, other_text, wanted = MagicMock(), MagicMock(), MagicMock()
        driver = MagicMock()
        driver.execute_script.side_effect = lambda script, *args: (
            [[other_page, f"Another Page\n{caption}", False],
             [other_text, "SunStar Davao\nFlooding occurred on Avenue Street", False],
             [wanted, f"SunStar Davao\n{caption}", False]]
            if script == images.POST_TEXTS else {"photos": [], "tiles": [], "images": ["https://cdn.example/a.jpg"]}
        )
        with patch.object(images.time, "sleep"):
            images.find_post_media(driver, caption, author="SunStar Davao", search=True)
        self.assertIs(driver.execute_script.call_args.args[1], wanted)

    def test_search_result_caption_is_expanded_before_it_is_compared(self):
        caption = "Aftermath of the Talomo River flooding yesterday; the revetment stood its ground against erosion"
        result, shown = MagicMock(), {"text": "Davao Commuter\nJuly 25\nAftermath of the Talomo… See more"}

        def run(script, *args):
            if script == images.POST_TEXTS:
                return [[result, shown["text"], False]]
            if script == "arguments[0].click();":
                shown["text"] = f"Davao Commuter\nJuly 25\n{caption}"
                return None
            return {"photos": [], "tiles": [], "images": ["https://cdn.example/a.jpg"]}

        driver = MagicMock()
        driver.execute_script.side_effect = run
        with patch.object(images.time, "sleep"):
            media = images.find_post_media(driver, caption, author="Davao Commuter", search=True)
        self.assertEqual(media["images"], ["https://cdn.example/a.jpg"])
        result.find_element.assert_called_once()

    def test_search_tries_the_captions_distinctive_words_when_its_opening_words_find_nothing(self):
        caption = "Baha sa Davao: flooding along Quimpo Boulevard after heavy rains this afternoon"
        found = {"photos": [], "tiles": [], "images": ["https://cdn.example/a.jpg"]}
        driver = MagicMock()
        with (
            patch.object(images, "find_post_media", side_effect=[None, found]) as find,
            patch.object(images, "download_media", return_value={"status": "downloaded"}) as download,
        ):
            result = images.search_post_images(driver, caption, "", Path("fb_x"), 8)
        self.assertEqual(result["status"], "downloaded")
        self.assertEqual([call.args[0] for call in driver.get.call_args_list],
                         [images.search_url(query) for query in images.search_queries(caption)])
        self.assertEqual(len(set(images.search_queries(caption))), 2)
        self.assertTrue(all(call.kwargs["search"] for call in find.call_args_list))
        self.assertIs(download.call_args.args[1], found)

    def test_post_that_never_appears_is_not_found(self):
        driver = MagicMock()
        driver.execute_script.return_value = [[MagicMock(), "Someone else's post about a birthday", True]]
        clock = iter(range(100))
        with patch.object(images.time, "sleep"), patch.object(images.time, "monotonic", side_effect=lambda: next(clock)):
            self.assertIsNone(images.find_post_media(driver, "Flooding in Barangay Tugbok Davao City"))

    def test_multi_photo_post_is_walked_past_its_visible_tiles_until_it_repeats(self):
        viewer = FakeViewer(["11", "12", "13", "14", "15", "16"])
        with patch.object(images.time, "sleep"):
            urls = images.full_size_urls(viewer, [link("11"), link("12"), link("13"), link("14")], max_images=8)
        self.assertEqual(urls, [f"https://cdn.example/{fbid}.jpg?oh=1" for fbid in ["11", "12", "13", "14", "15", "16"]])
        self.assertEqual(viewer.opened, [link("11")])

    def test_walk_stops_at_the_image_limit(self):
        viewer = FakeViewer([str(fbid) for fbid in range(20, 31)])
        with patch.object(images.time, "sleep"):
            self.assertEqual(len(images.full_size_urls(viewer, [link("20")], max_images=8)), 8)

    def test_non_image_responses_are_refused(self):
        login_page = MagicMock(status_code=200, headers={"content-type": "text/html"}, content=b"<html>" * 100)
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(images.requests, "get", return_value=login_page),
            patch.object(images.time, "sleep"),
        ):
            with self.assertRaisesRegex(ValueError, "text/html"):
                images.download("https://cdn.example/a.jpg?oh=1", Path(temp_dir) / "fb_x_01")
            self.assertEqual(list(Path(temp_dir).iterdir()), [])

    def test_failed_viewer_falls_back_to_the_posts_own_tiles(self):
        jpeg = MagicMock(status_code=200, headers={"content-type": "image/jpeg"}, content=b"\xff\xd8\xff" + b"x" * 300)
        missing = MagicMock(status_code=403, headers={"content-type": "text/plain"}, content=b"")
        media = {"photos": [link(1), link(2)], "tiles": ["https://cdn.example/t1.jpg", "https://cdn.example/t2.jpg"], "images": []}
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(images, "full_size_urls", return_value=[]),
            patch.object(images.requests, "get", side_effect=lambda url, **_: jpeg if url.endswith("t1.jpg") else missing),
            patch.object(images.time, "sleep"),
        ):
            result = images.download_media(MagicMock(), media, Path(temp_dir) / "fb_x", 8)
            self.assertEqual(result["status"], "partially_downloaded")
            self.assertEqual([path.name for path in result["paths"]], ["fb_x_01.jpg"])
            self.assertEqual(result["sources"], ["https://cdn.example/t1.jpg"])

    def test_video_only_post_has_no_image(self):
        result = images.download_media(MagicMock(), {"photos": [], "tiles": [], "images": []}, Path("fb_x"), 8)
        self.assertEqual(result, {"paths": [], "sources": [], "status": "no_image_found"})

    def test_login_page_while_opening_a_post_stops_with_a_message(self):
        driver = MagicMock()
        driver.current_url = "https://www.facebook.com/login/?next=%2Fpage%2Fposts%2F2"
        with self.assertRaisesRegex(images.FacebookLoginRequired, "--login"):
            images.download_post_images(driver, "https://www.facebook.com/page/posts/2", "Baha", Path("fb_x"), 8)


if __name__ == "__main__":
    unittest.main()
