#!/usr/bin/env python3
"""Optional backfill for older CSVs; new runs use fb_flood_scraper.py.

The collector uses the existing logged-in Selenium profile and only processes
images visible on the loaded Facebook post. By default it adds the image
columns to the existing raw CSVs, matching the main scraper's layout.

Rows with page-level or hashtag-level URLs are recorded as
``not_unique_post_url`` because an image cannot be assigned to one row safely
from those URLs.
"""

from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

import pandas as pd
from selenium.common.exceptions import TimeoutException, WebDriverException

try:
    from .fb_flood_scraper import (
        BASE_DIR, DEFAULT_PROFILE_DIR, find_image_elements, init_driver,
        is_unique_post_url, normalize_url, relative_project_path, safe_hash,
        save_image_element,
    )
except ImportError:
    from fb_flood_scraper import (
        BASE_DIR, DEFAULT_PROFILE_DIR, find_image_elements, init_driver,
        is_unique_post_url, normalize_url, relative_project_path, safe_hash,
        save_image_element,
    )

FACEBOOK_RAW_DIR = BASE_DIR / "data" / "raw" / "facebook"
IMAGE_DIR = FACEBOOK_RAW_DIR / "images"
ENRICHED_DIR = FACEBOOK_RAW_DIR / "with_images"
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"

IMAGE_COLUMNS = ["image_paths", "image_count", "image_status"]


def save_enriched(df: pd.DataFrame, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False, encoding="utf-8-sig")


def collect_row_images(
    driver,
    post_url: str,
    *,
    max_images: int,
    cache: dict[str, tuple[str, str, int]],
) -> tuple[str, str, int]:
    """Return ``image_paths``, ``image_status``, and image count for one row."""

    normalized = normalize_url(post_url)
    if not normalized:
        return "", "missing_post_url", 0
    if not is_unique_post_url(normalized):
        return "", "not_unique_post_url", 0
    if normalized in cache:
        return cache[normalized]

    try:
        driver.get(normalized)
    except TimeoutException:
        logging.warning("Timed out loading %s; checking the rendered page.", normalized)
    except WebDriverException as error:
        result = ("", f"navigation_error:{type(error).__name__}", 0)
        cache[normalized] = result
        return result

    time.sleep(3)
    current_url = str(driver.current_url).lower()
    if any(marker in current_url for marker in ("/login", "/checkpoint", "/recover")):
        result = ("", "login_required_or_checkpoint", 0)
        cache[normalized] = result
        return result

    try:
        image_elements = find_image_elements(driver, max_images)
    except WebDriverException as error:
        result = ("", f"image_scan_error:{type(error).__name__}", 0)
        cache[normalized] = result
        return result

    if not image_elements:
        result = ("", "no_image_found", 0)
        cache[normalized] = result
        return result

    paths: list[str] = []
    failures = 0
    post_hash = safe_hash(normalized)
    for image_index, (image_element, image_url) in enumerate(image_elements, start=1):
        image_stem = IMAGE_DIR / f"fb_{post_hash}_{image_index:02d}"
        try:
            path = save_image_element(driver, image_element, image_stem)
            paths.append(relative_project_path(path))
        except (OSError, ValueError, WebDriverException) as error:
            failures += 1
            logging.warning("Could not download image for %s: %s", normalized, error)

    if paths and not failures:
        status = "downloaded"
    elif paths:
        status = "partially_downloaded"
    else:
        status = "download_error"

    result = (";".join(paths), status, len(paths))
    cache[normalized] = result
    return result


def output_path_for(input_path: Path, *, in_place: bool) -> Path:
    if in_place:
        return input_path
    return ENRICHED_DIR / f"{input_path.stem}_with_images.csv"


def process_csv(
    driver,
    input_path: Path,
    *,
    max_images: int,
    cache: dict[str, tuple[str, str, int]],
    in_place: bool,
    checkpoint_every: int,
) -> Path:
    df = pd.read_csv(input_path)
    for column in IMAGE_COLUMNS:
        if column not in df.columns:
            df[column] = "" if column != "image_count" else 0

    total = len(df)
    output_path = output_path_for(input_path, in_place=in_place)
    logging.info("Processing %s (%s row(s))", input_path.name, total)

    for row_number, (index, row) in enumerate(df.iterrows(), start=1):
        if str(df.at[index, "image_status"]).strip() in {"downloaded", "partially_downloaded"}:
            continue

        paths, status, count = collect_row_images(
            driver,
            row.get("post_url", ""),
            max_images=max_images,
            cache=cache,
        )
        df.at[index, "image_paths"] = paths
        df.at[index, "image_count"] = count
        df.at[index, "image_status"] = status

        if row_number == 1 or row_number % checkpoint_every == 0:
            save_enriched(df, output_path)
            logging.info(
                "%s: checkpoint %s/%s rows -> %s",
                input_path.name,
                row_number,
                total,
                output_path,
            )

    save_enriched(df, output_path)
    logging.info("Completed %s -> %s", input_path.name, output_path)
    return output_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download visible Facebook post images and enrich raw CSV files."
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=FACEBOOK_RAW_DIR,
        help=f"Directory containing raw Facebook CSVs (default: {FACEBOOK_RAW_DIR}).",
    )
    parser.add_argument(
        "--image-dir",
        type=Path,
        default=IMAGE_DIR,
        help=f"Directory for downloaded images (default: {IMAGE_DIR}).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ENRICHED_DIR,
        help=f"Directory for optional enriched CSV copies (default: {ENRICHED_DIR}).",
    )
    parser.add_argument("--headed", action="store_true", help="Show Chrome while collecting images.")
    parser.add_argument(
        "--login",
        action="store_true",
        help="Open Facebook for manual login before collection; implies --headed.",
    )
    parser.add_argument(
        "--profile-dir",
        type=Path,
        default=DEFAULT_PROFILE_DIR,
        help=f"Persistent Chrome profile directory (default: {DEFAULT_PROFILE_DIR}).",
    )
    parser.add_argument(
        "--max-images",
        type=int,
        default=8,
        help="Maximum images to download per post (default: 8).",
    )
    parser.add_argument(
        "--checkpoint-every",
        type=int,
        default=10,
        help="Save enriched CSV after this many rows (default: 10).",
    )
    parser.add_argument(
        "--in-place",
        dest="in_place",
        action="store_true",
        default=True,
        help="Update the original raw CSVs (default).",
    )
    parser.add_argument(
        "--copy",
        dest="in_place",
        action="store_false",
        help="Write separate *_with_images.csv copies under --output-dir instead.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)

    global IMAGE_DIR, ENRICHED_DIR
    IMAGE_DIR = args.image_dir.resolve()
    ENRICHED_DIR = args.output_dir.resolve()
    input_dir = args.input_dir.resolve()
    files = sorted(input_dir.glob("*.csv"))
    if not files:
        logging.error("No CSV files found in %s", input_dir)
        return 1

    driver = init_driver(
        headless=not args.headed and not args.login,
        profile_dir=args.profile_dir,
    )
    cache: dict[str, tuple[str, str, int]] = {}

    try:
        if args.login:
            driver.get("https://www.facebook.com/")
            print(
                "\nA Facebook Chrome window is open. Complete login manually, "
                "then return to this terminal and press Enter."
            )
            input()

        for input_path in files:
            process_csv(
                driver,
                input_path,
                max_images=max(1, args.max_images),
                cache=cache,
                in_place=args.in_place,
                checkpoint_every=max(1, args.checkpoint_every),
            )
    finally:
        driver.quit()

    logging.info("Image collection completed; %s unique post URL(s) inspected.", len(cache))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
