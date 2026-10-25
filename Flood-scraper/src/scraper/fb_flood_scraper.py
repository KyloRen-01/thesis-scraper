#!/usr/bin/env python3
"""Collect flood-related Facebook posts and their images in one run.

This follows the senior project's Facebook scraper structure:

* visit configured page timelines;
* visit configured flood-related hashtag and post-search feeds;
* scroll to trigger Facebook's lazy loading;
* extract visible post text, timestamps, authors, and URLs;
* apply flood and Philippine-location filters;
* download each kept post's image files (fb_image_download.py: full-size
  photos from Facebook's photo viewer, not screenshots);
* checkpoint post details and image paths in one CSV.

With ``--retry-images``, it instead reopens saved posts that have no images
and adds their images to the existing CSVs.

The scraper does not attempt to bypass login, CAPTCHA, or other access
controls. It only processes content visible to the Selenium browser session.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import logging
import os
import re
import sqlite3
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs, quote_plus, urlparse

import pandas as pd
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By

try:
    from .fb_image_download import FacebookLoginRequired, download_media, download_post_images, post_media
    from .fb_scraping_config import FACEBOOK, KEYWORDS, PH_LOCATIONS
except ImportError:
    # Allows direct execution with: py src/scraper/fb_flood_scraper.py
    from fb_image_download import FacebookLoginRequired, download_media, download_post_images, post_media
    from fb_scraping_config import FACEBOOK, KEYWORDS, PH_LOCATIONS


BASE_DIR = Path(__file__).resolve().parents[2]
RAW_DIR = BASE_DIR / "data" / "raw" / "facebook"
RAW_DIR.mkdir(parents=True, exist_ok=True)
# Downloaded image files. The older "images" folder holds the screenshots earlier runs saved.
IMAGE_DIR = RAW_DIR / "downloaded_images"
POST_KINDS = {"posts", "permalink", "videos", "reel"}

PH_TIME = timezone(timedelta(hours=8))
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
DEFAULT_PROFILE_DIR = BASE_DIR / ".facebook_chrome_profile"
DEFAULT_CHECKPOINT_PATH = BASE_DIR / "data" / "checkpoints" / "facebook_posts.sqlite3"
# Edit these defaults for the plain `python src\scraper\fb_flood_scraper.py` run.
# Command-line options can still override them for a single run.
RUN_DEFAULTS = {
    "hours_page": None,
    "hours_search": None,
    "scroll_times": 80,
    "scroll_pause": 1.5,
    "stalled_scrolls": 5,
    "total_time_limit": 7200.0,
    "headed": True,
    "max_images": 8,
    "max_posts_per_feed": 50,
}
OUTPUT_COLUMNS = [
    "source", "query_page", "text", "post_timestamp", "scraped_timestamp",
    "user", "post_url", "image_paths", "image_count", "image_status",
]
IMAGE_COLUMNS = ["image_paths", "image_count", "image_status"]
SAVED_IMAGE_STATUSES = {"downloaded", "partially_downloaded"}
LOCATION_REFERENCE_FILES = {
    "refcitymun.csv": "citymunDesc",
    "refprovince.csv": "provDesc",
    "refregion.csv": "regDesc",
}


def load_ph_locations(reference_dir: Path = BASE_DIR / "data" / "lookup") -> set[str]:
    """Use the senior scraper's Philippine place references plus local terms."""

    locations = {location.strip() for location in PH_LOCATIONS if location.strip()}
    for filename, field in LOCATION_REFERENCE_FILES.items():
        path = reference_dir / filename
        if not path.is_file():
            logging.warning("Philippine location reference is missing: %s", path)
            continue
        with path.open("r", encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source)
            if field not in (reader.fieldnames or []):
                raise ValueError(f"Missing {field} column in {path}")
            for row in reader:
                name = (row[field] or "").strip()
                if name:
                    locations.add(name)
                    # Region references also contain common names in brackets,
                    # e.g. REGION XI (DAVAO REGION).
                    locations.update(part.strip() for part in re.findall(r"\(([^)]+)\)", name))
    return locations


LOCATION_PATTERN = re.compile(
    r"(?<!\w)(?:" + "|".join(
        re.escape(location) for location in sorted(load_ph_locations(), key=len, reverse=True)
    ) + r")(?!\w)",
    re.IGNORECASE,
)


def is_unique_post_url(value: Any) -> bool:
    """Return whether a Facebook URL identifies a post instead of a feed."""

    parsed = urlparse(str(value or ""))
    host = (parsed.hostname or "").lower()
    if host != "facebook.com" and not host.endswith(".facebook.com"):
        return False
    parts = [part for part in parsed.path.split("/") if part]
    return bool(
        (len(parts) >= 2 and parts[-2].lower() in POST_KINDS)
        or (len(parts) == 3 and [part.lower() for part in parts[:2]] == ["share", "p"])
    )


def normalize_url(value: Any) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    parsed = urlparse(value)
    return parsed._replace(query="", fragment="").geturl()


def safe_hash(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]


def facebook_url_key(value: Any) -> str:
    """Identify a Facebook post across mobile hosts, tracking URLs, and reels."""

    parsed = urlparse(str(value or "").strip())
    host = (parsed.hostname or "").lower()
    if host != "facebook.com" and not host.endswith(".facebook.com"):
        return ""
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[-2].lower() in POST_KINDS:
        return f"facebook:post:{parts[-1]}"
    if len(parts) == 3 and parts[0].lower() == "share" and parts[1].lower() in {"p", "v"}:
        return f"facebook:share:{parts[1].lower()}:{parts[2]}"
    if parsed.path.lower() in {"/story.php", "/permalink.php"}:
        post_id = parse_qs(parsed.query).get("story_fbid", [""])[0]
        if post_id:
            return f"facebook:post:{post_id}"
    return ""


def saved_post_key(row: dict[str, Any]) -> str:
    """Use the post ID, or a stable author/caption fingerprint without a URL."""

    url_key = facebook_url_key(row.get("post_url"))
    if url_key:
        return url_key
    text = str(row.get("text") or "")[:5000]
    text = " ".join(unicodedata.normalize("NFKC", text).casefold().split())
    text = re.sub(r"\s+see (?:more|less)$", "", text)
    if not text:
        return ""
    user = " ".join(unicodedata.normalize("NFKC", str(row.get("user") or "")).casefold().split())
    fingerprint = hashlib.sha256(f"{user}\x1f{text}".encode("utf-8")).hexdigest()
    return f"facebook:caption:{fingerprint}"


class FacebookPostCheckpoint:
    """Remember only posts whose CSV rows have been committed to disk."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        with self.connection:
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS saved_posts ("
                "post_key TEXT PRIMARY KEY, output_csv TEXT NOT NULL, saved_at TEXT NOT NULL)"
            )

    def saved_keys(self) -> set[str]:
        return {row[0] for row in self.connection.execute("SELECT post_key FROM saved_posts")}

    def record_saved_rows(self, rows: Iterable[dict[str, Any]], output_path: Path) -> None:
        saved_at = datetime.now(PH_TIME).isoformat(timespec="seconds")
        output_csv = str(output_path.resolve())
        with self.connection:
            self.connection.executemany(
                "INSERT OR IGNORE INTO saved_posts (post_key, output_csv, saved_at) VALUES (?, ?, ?)",
                ((key, output_csv, saved_at) for row in rows if (key := saved_post_key(row))),
            )

    def import_history(self, directory: Path) -> None:
        for path in sorted(directory.glob("*.csv")):
            with path.open("r", encoding="utf-8-sig", newline="") as source:
                reader = csv.DictReader(source)
                if "text" not in (reader.fieldnames or []):
                    logging.warning("Skipping CSV without a text column: %s", path)
                    continue
                self.record_saved_rows(
                    (row for row in reader if (row.get("source") or "Facebook").casefold() == "facebook"),
                    path,
                )

    def close(self) -> None:
        self.connection.close()


def relative_project_path(path: Path) -> str:
    return path.resolve().relative_to(BASE_DIR.resolve()).as_posix()


def image_fields(result: dict[str, Any]) -> tuple[str, str, int]:
    """CSV fields for the files fb_image_download saved."""

    return ";".join(relative_project_path(path) for path in result["paths"]), result["status"], len(result["paths"])


def collect_article_images(
    driver: webdriver.Chrome,
    article: Any,
    key: str,
    *,
    max_images: int,
) -> tuple[str, str, int]:
    """Download the images of one matched feed article and return its CSV fields.

    Photos are opened in a second tab, so the feed keeps its place.
    """

    try:
        media = post_media(driver, article)
    except WebDriverException as error:
        logging.warning("Could not scan post images: %s", error)
        return "", "image_scan_error", 0
    return image_fields(download_media(driver, media, IMAGE_DIR / f"fb_{safe_hash(key)}", max_images, new_tab=True))


def collect_post_page_images(
    driver: webdriver.Chrome,
    post_url: str,
    *,
    max_images: int,
    caption: str = "",
) -> tuple[str, str, int]:
    """Open one saved post and download its images, for rows that have none.

    The caption identifies the post among the other posts Facebook shows on the page.
    """

    return image_fields(
        download_post_images(driver, post_url, caption, IMAGE_DIR / f"fb_{safe_hash(post_url)}", max_images)
    )


def init_driver(
    *,
    headless: bool = True,
    chromedriver_path: str | None = None,
    profile_dir: Path | str | None = DEFAULT_PROFILE_DIR,
) -> webdriver.Chrome:
    """Create Chrome through an explicit ChromeDriver service.

    ``CHROMEDRIVER_PATH`` may be used when a local executable must be selected.
    If it is not supplied, Selenium Manager resolves a compatible driver.
    ``profile_dir`` persists cookies between runs so manual Facebook login is
    not lost when the scraper exits.
    """

    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )
    options.add_experimental_option("excludeSwitches", ["enable-logging"])

    if profile_dir:
        profile_path = Path(profile_dir).expanduser().resolve()
        profile_path.mkdir(parents=True, exist_ok=True)
        options.add_argument(f"--user-data-dir={profile_path}")

    driver_path = chromedriver_path or os.getenv("CHROMEDRIVER_PATH")
    service = Service(driver_path) if driver_path else Service()
    driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(45)
    return driver


def wait_for_manual_login(driver: webdriver.Chrome) -> None:
    driver.get("https://www.facebook.com/")
    print(
        "\nA Facebook Chrome window is open. Complete login manually there, "
        "then return to this terminal and press Enter to start scraping."
    )
    input()


def scroll_page(
    driver: webdriver.Chrome,
    scroll_times: int = 80,
    pause: float = 1.5,
    max_stalled_scrolls: int = 5,
    deadline: float | None = None,
    on_batch: Callable[[], None] | None = None,
    batch_every: int = 3,
    stop_when: Callable[[], bool] | None = None,
) -> None:
    """Scroll until the feed's post, scroll, stall, or run time limit is reached."""

    last_height = driver.execute_script("return document.body.scrollHeight")
    stalled_scrolls = 0
    completed_scrolls = 0
    max_stalled_scrolls = max(1, max_stalled_scrolls)
    for index in range(scroll_times):
        if stop_when is not None and stop_when():
            return
        if deadline is not None and time.monotonic() >= deadline:
            logging.info("Scrolling stopped because the overall run time limit was reached.")
            return

        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        remaining = deadline - time.monotonic() if deadline is not None else pause
        if remaining <= 0:
            logging.info("Scrolling stopped because the overall run time limit was reached.")
            return
        time.sleep(min(pause, remaining))

        if deadline is not None and time.monotonic() >= deadline:
            logging.info("Scrolling stopped because the overall run time limit was reached.")
            return

        driver.execute_script("window.scrollBy(0, -300);")
        completed_scrolls = index + 1
        if on_batch is not None and (index + 1) % batch_every == 0:
            on_batch()
            if stop_when is not None and stop_when():
                return
        new_height = driver.execute_script("return document.body.scrollHeight")
        if new_height <= last_height:
            stalled_scrolls += 1
            if stalled_scrolls >= max_stalled_scrolls:
                logging.info(
                    "Scrolling stopped after %s consecutive stalled scrolls at scroll %s.",
                    stalled_scrolls,
                    index + 1,
                )
                break
        else:
            stalled_scrolls = 0
        last_height = new_height
    else:
        logging.info("Scrolling reached the %s-scroll limit.", scroll_times)
    if on_batch is not None and completed_scrolls % batch_every and not (stop_when and stop_when()):
        on_batch()


def mentions_ph_location(text: str) -> bool:
    # Facebook hashtags often join place names with other capitalized words.
    separated = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)
    return LOCATION_PATTERN.search(separated) is not None


def extract_post_time(post: Any) -> datetime | None:
    """Read machine-readable timestamps from legacy and modern post markup."""

    try:
        elements = post.find_elements(By.XPATH, ".//abbr[@data-utime] | .//time[@datetime]")
        for element in elements:
            raw_epoch = element.get_attribute("data-utime")
            if raw_epoch:
                try:
                    return datetime.fromtimestamp(int(raw_epoch), tz=PH_TIME)
                except (ValueError, OverflowError, OSError):
                    continue
            raw_datetime = element.get_attribute("datetime")
            if raw_datetime:
                try:
                    parsed = datetime.fromisoformat(raw_datetime.replace("Z", "+00:00"))
                    if parsed.tzinfo is not None:
                        return parsed.astimezone(PH_TIME)
                except ValueError:
                    continue
    except WebDriverException:
        pass
    return None


def format_readable(timestamp: datetime) -> str:
    return timestamp.strftime("%Y-%m-%d %H:%M:%S")


def clean_comment_tail(text: str) -> str:
    """Keep the post caption and remove common visible comment-tail labels."""

    comment_patterns = [
        r"^see more comments",
        r"^view more replies",
        r"^(?:commented|replied|shared|tagged)$",
    ]
    lines = text.splitlines()
    filtered_lines = []
    for line in lines:
        if any(re.search(pattern, line.strip().lower()) for pattern in comment_patterns):
            break
        filtered_lines.append(line)
    return " ".join(filtered_lines).strip()


def expand_see_more(driver: webdriver.Chrome, post: Any) -> None:
    try:
        button = post.find_element(
            By.XPATH,
            ".//div[contains(@role,'button') and contains(normalize-space(.),'See more')]",
        )
        driver.execute_script("arguments[0].click();", button)
        time.sleep(0.5)
    except WebDriverException:
        pass


def extract_caption(driver: webdriver.Chrome, post: Any) -> str | None:
    """Extract visible post text, expanding a collapsed caption when possible."""

    try:
        expand_see_more(driver, post)
        blocks = post.find_elements(By.XPATH, ".//div[@dir='auto']")
        if not blocks:
            return None
        visible = [block.text.strip() for block in blocks[:2] if block.text.strip()]
        unique = []
        for block_text in visible:
            # Facebook often nests a shorter copy of the caption inside the
            # first auto-direction block; including both duplicates the post.
            if any(block_text in earlier for earlier in unique):
                continue
            unique = [earlier for earlier in unique if earlier not in block_text]
            unique.append(block_text)
        joined = " ".join(unique)
        joined = re.sub(r"\s+See (?:more|less)\s*$", "", joined, flags=re.IGNORECASE)
        text = clean_comment_tail(joined)
        return text if text and len(text.split()) >= 3 else None
    except WebDriverException:
        return None


def extract_username(post: Any, post_url: str = "") -> str:
    try:
        links = post.find_elements(By.XPATH, ".//h2//a | .//h3//a | .//h4//a | .//strong//a")
        for link in links:
            name = link.text.strip()
            if name:
                return name
    except WebDriverException:
        pass
    parts = [part for part in urlparse(post_url).path.split("/") if part]
    if len(parts) >= 3 and parts[1] in {"posts", "videos", "reel", "permalink"}:
        return parts[0]
    return "Unknown"


def extract_post_url(driver: webdriver.Chrome, post: Any) -> str:
    try:
        links = post.find_elements(By.XPATH, ".//a[@href]")
        for link in links:
            href = link.get_attribute("href") or ""
            if is_unique_post_url(href):
                return normalize_url(href)
    except WebDriverException:
        pass
    return ""


def process_fb_articles(
    driver: webdriver.Chrome,
    results: list[dict[str, str]],
    seen_urls: set[str],
    identifier: str,
    is_search_query: bool,
    since_page: datetime | None,
    since_query: datetime | None,
    scraped_timestamp: str,
    deadline: float | None = None,
    max_images: int = 8,
    on_progress: Callable[[int], None] | None = None,
    seen_feed_articles: set[str] | None = None,
    max_posts_to_scan: int | None = None,
) -> None:
    """Inspect distinct feed posts, saving only those that match the filters."""

    articles = driver.find_elements(By.XPATH, "//div[@role='article']")
    added = 0
    if seen_feed_articles is None:
        seen_feed_articles = set()
    scanned_before = len(seen_feed_articles)

    for index, article in enumerate(articles, start=1):
        if max_posts_to_scan is not None and len(seen_feed_articles) >= max_posts_to_scan:
            break
        if deadline is not None and time.monotonic() >= deadline:
            logging.info(
                "[Facebook] %s: extraction stopped at the overall run time limit (%s/%s articles).",
                identifier,
                index - 1,
                len(articles),
            )
            break

        post_url = extract_post_url(driver, article)
        url_key = facebook_url_key(post_url)
        if url_key and url_key in seen_feed_articles:
            continue
        if url_key and url_key in seen_urls:
            seen_feed_articles.add(url_key)
            continue
        text = extract_caption(driver, article)
        if not url_key and not text:
            continue
        user = extract_username(article, post_url) if not url_key else None
        article_key = url_key or saved_post_key({"user": user, "text": text})
        if article_key in seen_feed_articles:
            continue
        seen_feed_articles.add(article_key)
        if article_key in seen_urls:
            continue
        if not text:
            continue
        if not any(keyword.lower() in text.lower() for keyword in KEYWORDS):
            continue
        if not mentions_ph_location(text):
            continue

        post_timestamp = "Unknown"
        post_time = extract_post_time(article)
        cutoff = since_query if is_search_query else since_page
        if post_time is not None:
            if cutoff is not None and post_time < cutoff:
                continue
            post_timestamp = format_readable(post_time)
        elif cutoff is not None:
            # Unknown dates cannot be claimed to satisfy a recent-post cutoff.
            continue

        if user is None:
            user = extract_username(article, post_url)
        dedupe_key = saved_post_key({"post_url": post_url, "user": user, "text": text})
        if dedupe_key in seen_urls:
            continue

        if deadline is not None and time.monotonic() >= deadline:
            image_paths, image_status, image_count = "", "not_checked_time_limit", 0
        elif max_images > 0:
            image_paths, image_status, image_count = collect_article_images(
                driver, article, post_url or dedupe_key, max_images=max_images
            )
        else:
            image_paths, image_status, image_count = "", "not_requested", 0

        results.append(
            {
                "source": "Facebook",
                "query_page": identifier,
                "text": text[:5000],
                "post_timestamp": post_timestamp,
                "scraped_timestamp": scraped_timestamp,
                "user": user,
                "post_url": post_url,
                "image_paths": image_paths,
                "image_count": image_count,
                "image_status": image_status,
            }
        )
        seen_urls.add(dedupe_key)
        added += 1
        if on_progress is not None:
            on_progress(len(results))

    logging.info(
        "[Facebook] %s: +%s flood post(s) (%s article(s) scanned)",
        identifier,
        added,
        len(seen_feed_articles) - scanned_before,
    )


def scrape_feed(
    driver: webdriver.Chrome,
    results: list[dict[str, str]],
    seen_urls: set[str],
    *,
    url: str,
    identifier: str,
    is_search_query: bool,
    since_page: datetime | None,
    since_query: datetime | None,
    scraped_timestamp: str,
    scroll_times: int,
    scroll_pause: float,
    stalled_scrolls: int,
    deadline: float,
    output_path: Path | None,
    feed_type: str,
    max_images: int,
    max_posts_per_feed: int,
    on_progress: Callable[[int], None] | None = None,
    post_checkpoint: FacebookPostCheckpoint | None = None,
) -> None:
    """Visit, scroll, extract, and checkpoint one feed.

    Each feed is isolated so a timeout, stale Facebook element, or temporary
    Facebook error does not prevent the remaining pages and queries from being
    collected.
    """

    if max_posts_per_feed < 1:
        raise ValueError("max_posts_per_feed must be at least 1")
    seen_feed_articles: set[str] = set()

    def reached_post_limit() -> bool:
        return len(seen_feed_articles) >= max_posts_per_feed

    started_at = time.monotonic()
    remaining = max(0.0, deadline - started_at)
    if remaining <= 0:
        logging.info("Overall run time limit reached; skipping %s.", identifier)
        return

    logging.info(
        "[%s] Visiting %s (%.1f seconds remain in overall run limit)",
        feed_type,
        url,
        remaining,
    )

    try:
        driver.get(url)
    except TimeoutException:
        # Facebook often keeps rendering a usable feed after Selenium's page
        # load timeout. Continue with the page rather than losing the run.
        logging.warning(
            "[%s] Page-load timeout for %s; continuing with the current page.",
            identifier,
            url,
        )
    except WebDriverException as error:
        logging.warning(
            "[%s] Could not open %s: %s. Skipping this feed.",
            identifier,
            url,
            error,
        )
        save_results(results, output_path, checkpoint=f"skipped:{identifier}", post_checkpoint=post_checkpoint)
        return

    try:
        remaining = max(0.0, deadline - time.monotonic())
        if remaining:
            time.sleep(min(5, remaining))
        def collect_visible() -> None:
            if reached_post_limit():
                return
            before = len(results)
            process_fb_articles(
                driver,
                results,
                seen_urls,
                identifier,
                is_search_query,
                since_page,
                since_query,
                scraped_timestamp,
                deadline=deadline,
                max_images=max_images,
                on_progress=on_progress,
                seen_feed_articles=seen_feed_articles,
                max_posts_to_scan=max_posts_per_feed,
            )
            if len(results) > before:
                save_results(results, output_path, checkpoint=f"batch:{identifier}", post_checkpoint=post_checkpoint)

        collect_visible()
        if not reached_post_limit():
            scroll_page(
                driver,
                scroll_times,
                scroll_pause,
                stalled_scrolls,
                deadline=deadline,
                on_batch=collect_visible,
                stop_when=reached_post_limit,
            )
        collect_visible()
    except WebDriverException as error:
        logging.warning(
            "[%s] Browser error while collecting this feed: %s. Continuing.",
            identifier,
            error,
        )
    finally:
        save_results(results, output_path, checkpoint=f"{feed_type}:{identifier}", post_checkpoint=post_checkpoint)
        elapsed = time.monotonic() - started_at
        if time.monotonic() >= deadline:
            logging.info(
                "[%s] Overall run time limit reached after %.1f seconds; no more targets will start.",
                identifier,
                elapsed,
            )
        else:
            logging.info(
                "[%s] Feed complete after %.1f seconds; moving to the next target.",
                identifier,
                elapsed,
            )


def scrape_facebook(
    hours_page: int | None = RUN_DEFAULTS["hours_page"],
    hours_search: int | None = RUN_DEFAULTS["hours_search"],
    *,
    scroll_times: int = RUN_DEFAULTS["scroll_times"],
    scroll_pause: float = RUN_DEFAULTS["scroll_pause"],
    stalled_scrolls: int = RUN_DEFAULTS["stalled_scrolls"],
    total_timeout_seconds: float = RUN_DEFAULTS["total_time_limit"],
    headless: bool = not RUN_DEFAULTS["headed"],
    chromedriver_path: str | None = None,
    profile_dir: Path | str | None = DEFAULT_PROFILE_DIR,
    login: bool = False,
    output_path: Path | None = None,
    max_images: int = RUN_DEFAULTS["max_images"],
    max_posts_per_feed: int = RUN_DEFAULTS["max_posts_per_feed"],
    on_progress: Callable[[int], None] | None = None,
    checkpoint_path: Path | None = DEFAULT_CHECKPOINT_PATH,
) -> pd.DataFrame:
    """Scrape configured Facebook pages, hashtags, and post searches.

    With ``login=True``, the browser opens Facebook in headed mode and waits
    for the user to complete login manually before collection starts.
    """

    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    if max_posts_per_feed < 1:
        raise ValueError("max_posts_per_feed must be at least 1")
    now_ph = datetime.now(PH_TIME)
    since_page = now_ph - timedelta(hours=hours_page) if hours_page is not None else None
    since_query = now_ph - timedelta(hours=hours_search) if hours_search is not None else None
    scraped_timestamp = now_ph.strftime("%Y-%m-%d %H:%M")

    results: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    # Create the output header before browser startup so every CLI run has a
    # discoverable CSV, even if Chrome cannot start or no posts match.
    save_results(results, output_path, checkpoint="start")
    driver = None
    post_checkpoint = None
    try:
        if checkpoint_path is not None:
            post_checkpoint = FacebookPostCheckpoint(checkpoint_path)
            post_checkpoint.import_history(RAW_DIR)
            seen_urls.update(post_checkpoint.saved_keys())
        driver = init_driver(
            headless=headless,
            chromedriver_path=chromedriver_path,
            profile_dir=profile_dir,
        )
        if login:
            wait_for_manual_login(driver)

        # Start the shared scraping budget after browser setup and any manual
        # login, so login time is not counted as collection time.
        run_deadline = time.monotonic() + max(1.0, total_timeout_seconds)

        logging.info(
            "Collection plan: %s Facebook pages, %s hashtags, %s post searches; overall time limit: %.0f seconds",
            len(FACEBOOK["users"]),
            len(FACEBOOK["search_queries"]),
            len(FACEBOOK["search_queries"]),
            total_timeout_seconds,
        )

        for page in FACEBOOK["users"]:
            if time.monotonic() >= run_deadline:
                logging.info("Overall run time limit reached; stopping before the next Facebook page.")
                break
            scrape_feed(
                driver,
                results,
                seen_urls,
                url=f"https://www.facebook.com/{page.rstrip('/')}/posts",
                identifier=page,
                is_search_query=False,
                since_page=since_page,
                since_query=since_query,
                scraped_timestamp=scraped_timestamp,
                scroll_times=scroll_times,
                scroll_pause=scroll_pause,
                stalled_scrolls=stalled_scrolls,
                deadline=run_deadline,
                output_path=output_path,
                feed_type="fb_page",
                max_images=max_images,
                max_posts_per_feed=max_posts_per_feed,
                on_progress=on_progress,
                post_checkpoint=post_checkpoint,
            )

        for tag in FACEBOOK["search_queries"]:
            if time.monotonic() >= run_deadline:
                logging.info("Overall run time limit reached; stopping before the next hashtag.")
                break
            scrape_feed(
                driver,
                results,
                seen_urls,
                url=f"https://www.facebook.com/hashtag/{quote_plus(tag)}",
                identifier=f"#{tag}",
                is_search_query=True,
                since_page=since_page,
                since_query=since_query,
                scraped_timestamp=scraped_timestamp,
                scroll_times=scroll_times,
                scroll_pause=scroll_pause,
                stalled_scrolls=stalled_scrolls,
                deadline=run_deadline,
                output_path=output_path,
                feed_type="fb_hashtag",
                max_images=max_images,
                max_posts_per_feed=max_posts_per_feed,
                on_progress=on_progress,
                post_checkpoint=post_checkpoint,
            )

        for query in FACEBOOK["search_queries"]:
            if time.monotonic() >= run_deadline:
                logging.info("Overall run time limit reached; stopping before the next search query.")
                break
            scrape_feed(
                driver,
                results,
                seen_urls,
                url=f"https://www.facebook.com/search/posts/?q={quote_plus(query)}",
                identifier=query,
                is_search_query=True,
                since_page=since_page,
                since_query=since_query,
                scraped_timestamp=scraped_timestamp,
                scroll_times=scroll_times,
                scroll_pause=scroll_pause,
                stalled_scrolls=stalled_scrolls,
                deadline=run_deadline,
                output_path=output_path,
                feed_type="fb_search",
                max_images=max_images,
                max_posts_per_feed=max_posts_per_feed,
                on_progress=on_progress,
                post_checkpoint=post_checkpoint,
            )
    finally:
        try:
            if driver is not None:
                driver.quit()
        finally:
            if post_checkpoint is not None:
                post_checkpoint.close()

    dataframe = pd.DataFrame(
        results,
        columns=OUTPUT_COLUMNS,
    )
    logging.info("Facebook flood scraper collected %s post(s)", len(dataframe))
    return dataframe


def retry_images(
    *,
    headless: bool = not RUN_DEFAULTS["headed"],
    chromedriver_path: str | None = None,
    profile_dir: Path | str | None = DEFAULT_PROFILE_DIR,
    login: bool = False,
    max_images: int = RUN_DEFAULTS["max_images"],
    on_progress: Callable[[int], None] | None = None,
) -> int:
    """Reopen saved posts that have no images and add the images to their raw CSVs.

    Scraping runs already save images with each post. This replaces the former
    fb_image_scraper.py backfill for rows whose images were missed, for example
    because the post had not finished loading.
    """

    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    driver = init_driver(headless=headless, chromedriver_path=chromedriver_path, profile_dir=profile_dir)
    try:
        if login:
            wait_for_manual_login(driver)
        updated = add_missing_images(
            driver, sorted(RAW_DIR.glob("*.csv")), max_images=max_images, on_progress=on_progress
        )
    finally:
        driver.quit()
    logging.info("Added images to %s saved Facebook row(s)", updated)
    return updated


def add_missing_images(
    driver: webdriver.Chrome,
    csv_paths: Iterable[Path],
    *,
    max_images: int,
    on_progress: Callable[[int], None] | None = None,
) -> int:
    """Add images to saved rows that have none, changing only rows that gain images.

    Rows need a unique post link; page and search links cannot be assigned
    images safely. A CSV is rewritten atomically after each changed row, so
    CSVs without new images keep their exact contents and an interruption
    keeps earlier updates.
    """

    updated = 0
    attempts: dict[str, tuple[str, str, int]] = {}
    for path in csv_paths:
        with path.open("r", encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source)
            columns = list(reader.fieldnames or [])
            rows = list(reader)
        if "post_url" not in columns:
            logging.warning("Skipping CSV without a post_url column: %s", path)
            continue
        columns += [column for column in IMAGE_COLUMNS if column not in columns]

        for row in rows:
            post_url = normalize_url(row.get("post_url"))
            if (row.get("image_status") or "").strip() in SAVED_IMAGE_STATUSES or not is_unique_post_url(post_url):
                continue
            if post_url not in attempts:
                try:
                    attempts[post_url] = collect_post_page_images(
                        driver, post_url, max_images=max_images, caption=row.get("text") or ""
                    )
                except WebDriverException as error:
                    logging.warning("Could not open %s: %s", post_url, error)
                    attempts[post_url] = ("", "navigation_error", 0)
            image_paths, image_status, image_count = attempts[post_url]
            if not image_count:
                continue
            row.update(image_paths=image_paths, image_count=str(image_count), image_status=image_status)
            write_csv_rows(path, columns, rows)
            updated += 1
            if on_progress is not None:
                on_progress(updated)
    return updated


def write_csv_rows(path: Path, columns: list[str], rows: list[dict[str, str]]) -> None:
    """Replace a CSV atomically, keeping its columns and other values unchanged."""

    temporary_path = path.with_name(f".{path.name}.tmp")
    pd.DataFrame(rows, columns=columns).to_csv(temporary_path, index=False, encoding="utf-8-sig")
    temporary_path.replace(path)


def save_results(
    results: list[dict[str, str]],
    output_path: Path | None,
    *,
    checkpoint: str | None = None,
    post_checkpoint: FacebookPostCheckpoint | None = None,
) -> None:
    """Save a checkpoint atomically so interruption does not lose prior feeds."""

    if output_path is None:
        return

    dataframe = pd.DataFrame(
        results,
        columns=OUTPUT_COLUMNS,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_name(f".{output_path.name}.tmp")
    dataframe.to_csv(temporary_path, index=False, encoding="utf-8-sig")
    temporary_path.replace(output_path)
    if post_checkpoint is not None:
        post_checkpoint.record_saved_rows(results, output_path)
    if checkpoint:
        logging.info(
            "Checkpoint saved after %s: %s row(s) -> %s",
            checkpoint,
            len(dataframe),
            output_path,
        )


def choose_output_path(requested_path: Path | None) -> Path:
    """Return a unique output path for this run.

    Slashes are invalid in Windows filenames, so the date uses hyphens while
    retaining the requested month-day-year and time order.
    """

    timestamp = datetime.now(PH_TIME).strftime("%m-%d-%Y_%H%M%S")
    automatic_path = RAW_DIR / f"fb_raw_floodData_{timestamp}.csv"

    def unused(path: Path) -> Path:
        candidate = path
        counter = 2
        while candidate.exists():
            candidate = path.with_name(f"{path.stem}_{counter}{path.suffix}")
            counter += 1
        return candidate

    if requested_path is None:
        return unused(automatic_path)

    # Treat the old fixed-name convention as a request for a new run file.
    if requested_path.name.lower() in {"fb_raw_flooddata_latest.csv", "fb_raw_flood_posts.csv"}:
        return unused(requested_path.parent / f"fb_raw_floodData_{timestamp}.csv")

    # Never overwrite an existing explicitly named output either.
    if requested_path.exists():
        suffix = requested_path.suffix or ".csv"
        stem = requested_path.stem
        return unused(requested_path.with_name(f"{stem}_{timestamp}{suffix}"))

    return requested_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape flood-related Facebook posts into CSV.")
    parser.add_argument(
        "--hours-page",
        type=int,
        default=RUN_DEFAULTS["hours_page"],
        help="Only keep page posts from this many hours ago. Omit for no time cutoff.",
    )
    parser.add_argument(
        "--hours-search",
        type=int,
        default=RUN_DEFAULTS["hours_search"],
        help="Only keep search/hashtag posts from this many hours ago. Omit for no time cutoff.",
    )
    parser.add_argument(
        "--scroll-times",
        type=int,
        default=RUN_DEFAULTS["scroll_times"],
        help=f"Maximum scrolls per feed (default: {RUN_DEFAULTS['scroll_times']}).",
    )
    parser.add_argument(
        "--scroll-pause",
        type=float,
        default=RUN_DEFAULTS["scroll_pause"],
        help=f"Seconds to wait between scrolls (default: {RUN_DEFAULTS['scroll_pause']}).",
    )
    parser.add_argument(
        "--stalled-scrolls",
        type=int,
        default=RUN_DEFAULTS["stalled_scrolls"],
        help=f"Consecutive stalled scrolls before stopping (default: {RUN_DEFAULTS['stalled_scrolls']}).",
    )
    parser.add_argument(
        "--total-time-limit",
        "--feed-time-limit",
        dest="total_time_limit",
        type=float,
        default=RUN_DEFAULTS["total_time_limit"],
        help=f"Maximum seconds across all feeds (default: {RUN_DEFAULTS['total_time_limit']:.0f}).",
    )
    browser_mode = parser.add_mutually_exclusive_group()
    browser_mode.add_argument("--headed", dest="headed", action="store_true", help="Show Chrome while scraping.")
    browser_mode.add_argument("--headless", dest="headed", action="store_false", help="Run without showing Chrome.")
    parser.set_defaults(headed=RUN_DEFAULTS["headed"])
    parser.add_argument(
        "--login",
        action="store_true",
        help="Open Facebook for manual login before scraping; implies headed mode.",
    )
    parser.add_argument("--chromedriver", help="Path to chromedriver.exe.")
    parser.add_argument(
        "--max-images",
        type=int,
        default=RUN_DEFAULTS["max_images"],
        help=f"Maximum images per post (default: {RUN_DEFAULTS['max_images']}; use 0 for text only).",
    )
    parser.add_argument(
        "--max-posts-per-feed",
        type=int,
        default=RUN_DEFAULTS["max_posts_per_feed"],
        help=f"Maximum distinct posts to inspect from each page, hashtag, or search feed (default: {RUN_DEFAULTS['max_posts_per_feed']}).",
    )
    parser.add_argument(
        "--profile-dir",
        type=Path,
        default=DEFAULT_PROFILE_DIR,
        help=f"Persistent Chrome profile directory (default: {DEFAULT_PROFILE_DIR}).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional output path. Omit it to create a timestamped CSV automatically.",
    )
    parser.add_argument(
        "--checkpoint-file",
        type=Path,
        default=DEFAULT_CHECKPOINT_PATH,
        help=f"Persistent record of saved Facebook posts (default: {DEFAULT_CHECKPOINT_PATH}).",
    )
    parser.add_argument(
        "--retry-images",
        action="store_true",
        help="Instead of scraping feeds, reopen saved posts that have no images and add "
        "their images to the existing CSVs in data/raw/facebook.",
    )
    args = parser.parse_args()
    if args.max_posts_per_feed < 1:
        parser.error("--max-posts-per-feed must be at least 1")
    if args.retry_images and args.max_images < 1:
        parser.error("--retry-images needs --max-images of at least 1")
    return args


def main() -> int:
    args = parse_args()
    output_path = choose_output_path(args.output)
    logging.basicConfig(level=logging.ERROR, format=LOG_FORMAT, force=True)
    label = "Rows given images" if args.retry_images else "Posts scraped"
    displayed_count = -1

    def show_count(count: int) -> None:
        nonlocal displayed_count
        if count != displayed_count:
            print(f"\r{label}: {count}", end="", flush=True)
            displayed_count = count

    show_count(0)
    try:
        if args.retry_images:
            retry_images(
                headless=not args.headed and not args.login,
                chromedriver_path=args.chromedriver,
                profile_dir=args.profile_dir,
                login=args.login,
                max_images=args.max_images,
                on_progress=show_count,
            )
            print()
            return 0
        dataframe = scrape_facebook(
            hours_page=args.hours_page,
            hours_search=args.hours_search,
            scroll_times=args.scroll_times,
            scroll_pause=args.scroll_pause,
            stalled_scrolls=args.stalled_scrolls,
            total_timeout_seconds=args.total_time_limit,
            headless=not args.headed and not args.login,
            chromedriver_path=args.chromedriver,
            profile_dir=args.profile_dir,
            login=args.login,
            output_path=output_path,
            max_images=max(0, args.max_images),
            max_posts_per_feed=args.max_posts_per_feed,
            on_progress=show_count,
            checkpoint_path=args.checkpoint_file,
        )
        save_results(dataframe.to_dict("records"), output_path)
        show_count(len(dataframe))
        print()
        return 0
    except KeyboardInterrupt:
        print()
        return 130
    except FacebookLoginRequired as error:
        print()
        logging.error("%s", error)
        return 1
    except WebDriverException as error:
        print()
        logging.error("ChromeDriver could not start or load Facebook: %s", error)
        return 1
    except (OSError, sqlite3.Error, csv.Error) as error:
        print()
        logging.error("Could not save or read Facebook CSV/checkpoint data: %s", error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
