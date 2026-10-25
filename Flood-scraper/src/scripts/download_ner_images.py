"""Replace the screenshots in the final NER CSVs with each post's downloaded image files.

Download: python src/scripts/download_ner_images.py           (Chrome opens; stop and rerun at any time)
Try:      python src/scripts/download_ner_images.py --limit 3
Retry:    python src/scripts/download_ner_images.py --retry   (posts that failed or were not found)
Apply:    python src/scripts/download_ner_images.py --apply   (backs up, then rewrites the image columns)

Every post in data/ner/ner_datetime_locations is opened with the scraper's logged-in Chrome
profile: by its link, or, without one, by searching Facebook for its caption, accepting only a
result from the same author whose text closely matches. Its images are saved from Facebook's photo
viewer (src/scraper/fb_image_download.py) to data/raw/facebook/downloaded_images, and each result
is appended to data/checkpoints/ner_image_downloads.jsonl, so a rerun skips finished posts.

--apply changes only the image columns. A post gets its downloaded files; a post whose images could
not be downloaded gets none, because its screenshots are not reliable images (some show caption
text). The NER details files get the same images, and the NER manifest gets a record of the new
files so later NER runs accept them (NER's --overwrite would rebuild them from the deduplicated
CSVs, screenshots included). The original files and download_log.csv (every post's old and new
images) are kept in data/archive/ner_datetime_locations_before_image_download_<time>.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import re
import shutil
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from selenium.common.exceptions import WebDriverException

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "src/xlmr-ner"))
import ner_utils as u
from src.scraper import fb_flood_scraper as fb
from src.scraper import fb_image_download as images

NER_DIR = PROJECT / "data/ner/ner_datetime_locations"
PROGRESS = PROJECT / "data/checkpoints/ner_image_downloads.jsonl"
ARCHIVE = PROJECT / "data/archive"
FINISHED = {"downloaded", "no_image_found", "video_post"}  # Results --retry leaves alone.
VIDEO_LINK = re.compile(r"facebook\.com/(?:reel|watch)/|/videos/", re.IGNORECASE)  # A video post has no photos.
MISSES_BEFORE_STOP = 12  # Linked posts not found in a row: Facebook has probably stopped showing posts.
LOG_COLUMNS = ["file", "row", "url", "page_source", "status", "old_image_paths", "new_image_paths", "image_sources"]


def ner_files() -> list[Path]:
    return sorted(NER_DIR.glob("fb_ner_datetime_locations_floodData_*.csv"))


def post_key(row: dict[str, str]) -> str:
    if fb.is_unique_post_url(row["url"]):
        return fb.facebook_url_key(row["url"])
    fingerprint = hashlib.sha256(f"{row['page_source']}\x1f{row['text_content']}".encode("utf-8")).hexdigest()
    return f"facebook:caption:{fingerprint[:24]}"


def image_list(value: str) -> list[str]:
    return [path.strip() for path in value.split(";") if path.strip()]


def ner_posts() -> list[dict]:
    """Each distinct post in the final NER CSVs, linked posts first."""
    posts: dict[str, dict] = {}
    for path in ner_files():
        for row in u.read_csv(path)[1]:
            old = image_list(row["image_paths"])
            # Reuse the screenshot's name, so each download sits beside the screenshot it replaces.
            name = re.sub(r"_\d+\.\w+$", "", Path(old[0]).name) if old else f"fb_{fb.safe_hash(post_key(row))}"
            posts.setdefault(post_key(row), {
                "key": post_key(row), "url": row["url"] if fb.is_unique_post_url(row["url"]) else "",
                "caption": row["text_content"], "author": row["page_source"], "old": old, "name": name,
            })
    return sorted(posts.values(), key=lambda post: not post["url"])


def load_progress() -> dict[str, dict]:
    results: dict[str, dict] = {}
    if PROGRESS.is_file():
        for line in PROGRESS.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue  # A line cut short when a run was stopped; that post is opened again.
            results[record["key"]] = record
    return results


def download_all(*, limit: int | None, retry: bool, login: bool, max_images: int) -> int:
    done = load_progress()
    todo = [post for post in ner_posts()
            if post["key"] not in done or (retry and done[post["key"]]["status"] not in FINISHED)]
    todo = todo[:limit] if limit else todo
    print(f"{len(todo)} post(s) to open; images go to {fb.relative_project_path(fb.IMAGE_DIR)}", flush=True)
    if not todo:
        return 0
    PROGRESS.parent.mkdir(parents=True, exist_ok=True)
    driver = fb.init_driver(headless=not fb.RUN_DEFAULTS["headed"], profile_dir=fb.DEFAULT_PROFILE_DIR)
    counts: Counter = Counter()
    misses = 0
    try:
        if login:
            fb.wait_for_manual_login(driver)
        with PROGRESS.open("a", encoding="utf-8") as progress:
            for number, post in enumerate(todo, 1):
                stem = fb.IMAGE_DIR / post["name"]
                # Without a recorded author, page_source holds the feed's hashtag or "Unknown".
                author = "" if post["author"].startswith("#") or post["author"].casefold() == "unknown" else post["author"]
                try:
                    if VIDEO_LINK.search(post["url"]):
                        result = {"paths": [], "sources": [], "status": "video_post"}
                    elif post["url"]:
                        result = images.download_post_images(driver, post["url"], post["caption"], stem, max_images)
                    else:
                        result = images.search_post_images(driver, post["caption"], author, stem, max_images)
                except WebDriverException as error:
                    result = {"paths": [], "sources": [], "status": "navigation_error",
                              "error": (str(error).strip().splitlines() or [""])[0][:200]}
                record = {"key": post["key"], "url": post["url"], "status": result["status"],
                          "paths": [fb.relative_project_path(path) for path in result["paths"]],
                          "sources": result["sources"], "error": result.get("error", ""),
                          "at": datetime.now().isoformat(timespec="seconds")}
                progress.write(json.dumps(record) + "\n")
                progress.flush()
                counts[result["status"]] += 1
                target = post["url"] or f"search ({post['author']})"
                print(f"[{number}/{len(todo)}] {result['status']:<20} {len(record['paths'])} image(s), "
                      f"{len(post['old'])} screenshot(s) before | {target}", flush=True)
                misses = misses + 1 if post["url"] and result["status"] in ("post_not_found", "navigation_error") else 0
                if misses >= MISSES_BEFORE_STOP:
                    print(f"Stopped: {misses} linked posts in a row were not found. Facebook may have logged out or "
                          "limited this account; check the Chrome profile, then rerun with --retry.", flush=True)
                    return 1
                time.sleep(random.uniform(1.0, 2.5))  # A steady, unhurried pace for Facebook.
    except images.FacebookLoginRequired as error:
        print(f"{error}\nThat is: python src/scripts/download_ner_images.py --retry --login", flush=True)
        return 1
    finally:
        driver.quit()
        print("Results:", dict(counts), flush=True)
    return 0


def apply() -> int:
    done = load_progress()
    posts = ner_posts()
    unopened = [post for post in posts if post["key"] not in done]
    if unopened:
        print(f"{len(unopened)} post(s) have not been opened yet; run the download first.")
        return 1
    new: dict[str, tuple[list[str], str]] = {}
    for post in posts:
        record = done[post["key"]]
        paths = record["paths"] if record["status"] in images.SAVED_STATUSES else []
        missing = [path for path in paths if not (PROJECT / path).is_file()]
        if missing:
            raise FileNotFoundError(f"Downloaded image is missing: {missing[0]}")
        new[post["key"]] = (paths, record["status"])

    manifest = NER_DIR / "_metadata/manifest.csv"
    plans = []
    for path in ner_files():
        columns, rows, checksum = u.read_csv(path)
        record = u.previous(path)
        details = u.details_path(path)
        if not record or record["output_sha256"] != checksum:
            raise ValueError(f"{path.name} does not match its NER manifest record; not changing it.")
        u.checked_details(path, record)
        detail_columns, detail_rows, _ = u.read_csv(details)
        if len(detail_rows) != len(rows) or any(
                (detail["post_url"], detail["image_paths"]) != (row["url"], row["image_paths"])
                for detail, row in zip(detail_rows, rows)):
            raise ValueError(f"{details.name} is not aligned with {path.name}; not changing it.")
        plans.append((path, columns, rows, record, details, detail_columns, detail_rows))
    for path in [manifest] + [target for plan in plans for target in (plan[0], plan[4])]:
        try:
            with path.open("r+b"):
                pass
        except PermissionError:
            print(f"{path.name} is open in another program (WPS, Excel?); close it and run --apply again.")
            return 1

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = ARCHIVE / f"ner_datetime_locations_before_image_download_{stamp}"
    (backup / "_metadata").mkdir(parents=True)
    shutil.copy2(manifest, backup / "_metadata" / manifest.name)
    log, counts = [], Counter()
    records = u.history(manifest)
    for path, columns, rows, record, details, detail_columns, detail_rows in plans:
        shutil.copy2(path, backup / path.name)
        shutil.copy2(details, backup / "_metadata" / details.name)
        for number, (row, detail) in enumerate(zip(rows, detail_rows), 1):
            paths, status = new[post_key(row)]
            log.append({"file": path.name, "row": number, "url": row["url"], "page_source": row["page_source"],
                        "status": status, "old_image_paths": row["image_paths"], "new_image_paths": ";".join(paths),
                        "image_sources": ";".join(done[post_key(row)]["sources"] if paths else [])})
            counts[status] += 1
            row["image_paths"] = detail["image_paths"] = ";".join(paths)
            detail.update(image_count=str(len(paths)), image_status=status)
        u._atomic_csv(details, detail_columns, detail_rows)
        u._atomic_csv(path, columns, rows)
        now = datetime.now(u.PH).isoformat(timespec="microseconds")
        records.append({**record, "output_sha256": u.digest(path.read_bytes()), "started_at": now,
                        "completed_at": now, "details_sha256": u.digest(details.read_bytes())})
        u._atomic_csv(manifest, u.MANIFEST, records)  # After each file, so a failure leaves earlier files audited.
    u._atomic_csv(backup / "download_log.csv", LOG_COLUMNS, log)
    print(f"Rewrote {len(plans)} NER CSV(s); backup and download_log.csv in {fb.relative_project_path(backup)}")
    print("Posts by result:", dict(counts))
    print("Images now:", sum(len(image_list(row["new_image_paths"])) for row in log),
          "| screenshots before:", sum(len(image_list(row["old_image_paths"])) for row in log))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="Rewrite the NER CSVs from the downloads (no browser).")
    parser.add_argument("--retry", action="store_true", help="Also reopen posts that failed or were not found.")
    parser.add_argument("--limit", type=int, help="Open at most this many posts (for a test).")
    parser.add_argument("--login", action="store_true", help="Sign in to Facebook in the Chrome window first.")
    parser.add_argument("--max-images", type=int, default=fb.RUN_DEFAULTS["max_images"],
                        help=f"Most images per post, as in the scraper (default: {fb.RUN_DEFAULTS['max_images']}).")
    args = parser.parse_args()
    if args.apply:
        return apply()
    return download_all(limit=args.limit, retry=args.retry, login=args.login, max_images=args.max_images)


if __name__ == "__main__":
    raise SystemExit(main())
