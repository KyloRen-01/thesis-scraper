"""Check that the historical Facebook data move preserves image references."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.scripts import consolidate_facebook_raw as organizer


class ConsolidationTests(unittest.TestCase):
    def test_dated_csvs_get_image_fields_and_historical_data_is_archived(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            facebook = project / "data" / "raw" / "facebook"
            enriched = facebook / "with_images"
            enriched.mkdir(parents=True)
            (facebook / "images").mkdir()
            (facebook / "gempesaw_images").mkdir()
            (facebook / "_evaluation_sheets").mkdir()
            (facebook / "images" / "dated.png").write_bytes(b"image")
            (facebook / "gempesaw_images" / "old.png").write_bytes(b"image")

            dated_name = "fb_raw_floodData_10-20-2026_201500.csv"
            base_columns = ["source", "query_page", "text", "post_url"]
            dated_row = {
                "source": "Facebook", "query_page": "DavaoDRRMO",
                "text": "Flood in Davao", "post_url": "https://www.facebook.com/page/posts/1",
            }
            old_row = {
                "source": "Facebook", "query_page": "Gempesaw",
                "text": "Baha in Manila", "post_url": "https://www.facebook.com/page/posts/2",
            }
            organizer.write_csv(facebook / dated_name, base_columns, [dated_row])
            organizer.write_csv(facebook / "Gempesaw_FloodData.csv", base_columns, [old_row])
            organizer.write_csv(
                enriched / dated_name.replace(".csv", "_with_images.csv"),
                base_columns + list(organizer.IMAGE_COLUMNS),
                [{**dated_row, "image_paths": "data/raw/facebook/images/dated.png", "image_count": "1", "image_status": "downloaded"}],
            )
            organizer.write_csv(
                enriched / "Gempesaw_FloodData_with_images.csv",
                base_columns + list(organizer.IMAGE_COLUMNS),
                [{**old_row, "image_paths": "data/raw/facebook/gempesaw_images/old.png", "image_count": "1", "image_status": "downloaded"}],
            )
            organizer.write_csv(
                facebook / "_evaluation_sheets" / "image_manifest.csv",
                ["Path"], [{"Path": str(facebook / "gempesaw_images" / "old.png")}],
            )

            with (
                patch.object(organizer, "PROJECT", project),
                patch.object(organizer, "FACEBOOK", facebook),
                patch.object(organizer, "ENRICHED", enriched),
            ):
                pairs = organizer.plan()
                archive = organizer.consolidate(pairs)

            self.assertEqual(sorted(item.name for item in facebook.iterdir()), [dated_name, "images"])
            columns, rows = organizer.read_csv(facebook / dated_name)
            self.assertIn("image_paths", columns)
            self.assertEqual(rows[0]["image_paths"], "data/raw/facebook/images/dated.png")
            self.assertTrue((archive / "original_root_csv" / dated_name).is_file())
            self.assertTrue((archive / "gempesaw_images" / "old.png").is_file())
            _, old_rows = organizer.read_csv(archive / "other_merged_csv" / "Gempesaw_FloodData.csv")
            self.assertTrue((project / old_rows[0]["image_paths"]).is_file())
            _, manifest_rows = organizer.read_csv(archive / "evaluation_sheets" / "image_manifest.csv")
            self.assertEqual(manifest_rows[0]["Path"], str(archive / "gempesaw_images" / "old.png"))


if __name__ == "__main__":
    unittest.main()
