"""Checks for the local image-labeling page: item order, shared samples, saved labels, and the HTTP routes."""

import csv
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from src.image_classification import label_images as labeling

COLUMNS = ["source", "page_source", "text_content", "datetime", "location", "image_paths", "url"]


def write_posts(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(dict(zip(COLUMNS, ["Facebook", "Davao DRRMO", f"baha {number}", "", "DAVAO CITY", images,
                                            f"https://www.facebook.com/a/posts/{number}"]))
                         for number, images in enumerate(rows))


class LabelingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.input = self.root / "ner"
        write_posts(self.input / "fb_ner_datetime_locations_floodData_10-20-2026_230340.csv",
                    ["data/a.png;data/b.png", "", "data/c.png"])

    def test_items_skip_posts_without_images_and_keep_post_positions(self):
        items = labeling.build_items(self.input)
        self.assertEqual([(i["path"], i["post"], i["image"], i["images"]) for i in items],
                         [("data/a.png", 1, 1, 2), ("data/b.png", 1, 2, 2), ("data/c.png", 2, 1, 1)])

    def test_sample_is_the_same_for_every_rater_with_the_same_seed(self):
        write_posts(self.input / "fb_ner_datetime_locations_floodData_10-20-2026_230340.csv",
                    [f"data/{n}.png" for n in range(30)])
        first = labeling.build_items(self.input, sample=5, seed=7)
        self.assertEqual(len(first), 5)
        self.assertEqual(first, labeling.build_items(self.input, sample=5, seed=7))

    def test_labels_are_saved_and_reloaded(self):
        path = self.root / "labels" / "labels_tester.csv"
        item = labeling.build_items(self.input)[0]
        store = labeling.LabelStore(path)
        store.set(item, "2")
        reloaded = labeling.LabelStore(path)
        self.assertEqual(reloaded.labels["data/a.png"]["severity"], "Moderate Flooding")
        reloaded.set(item, None)
        self.assertEqual(labeling.LabelStore(path).labels, {})

    def test_page_routes_serve_images_and_save_labels(self):
        image = self.root / "data/a.png"
        image.parent.mkdir(parents=True)
        image.write_bytes(b"\x89PNG fake image")
        items = labeling.build_items(self.input)
        store = labeling.LabelStore(self.root / "labels" / "labels_tester.csv")
        with patch.object(labeling, "ROOT", self.root):
            server = ThreadingHTTPServer(("127.0.0.1", 0), labeling.make_handler(items, store, "tester"))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            base = f"http://127.0.0.1:{server.server_port}"

            def post(body):
                request = urllib.request.Request(base + "/label", data=json.dumps(body).encode(),
                                                 headers={"Content-Type": "application/json"})
                return urllib.request.urlopen(request)

            self.assertIn(b"Flood severity labeling", urllib.request.urlopen(base + "/").read())
            data = json.loads(urllib.request.urlopen(base + "/items").read())
            self.assertEqual((len(data["items"]), data["labels"]), (3, {}))
            self.assertEqual(urllib.request.urlopen(base + "/image/0").read(), image.read_bytes())
            self.assertEqual(json.loads(post({"index": 0, "key": "3"}).read()), {"labeled": 1})
            self.assertEqual(store.labels["data/a.png"]["severity"], "Major Flooding")
            self.assertEqual(json.loads(urllib.request.urlopen(base + "/items").read())["labels"], {"0": "3"})
            for bad in [{"index": 0, "key": "5"}, {"index": 99, "key": "1"}]:
                with self.assertRaises(urllib.error.HTTPError):
                    post(bad)
            with self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen(base + "/image/1")  # b.png does not exist on disk


if __name__ == "__main__":
    unittest.main()
