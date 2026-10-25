"""Download the actual image files of Facebook posts instead of screenshotting them.

Photos are opened in Facebook's photo viewer, which serves the full-size, uncropped file; a
multi-photo post (``set=pcb.*``) is walked with "Next photo", so photos hidden behind a "+N" tile
are included. Link previews and other pictures shown in the post are saved as shown. Only images
inside the post itself are taken: comment photos, ads, and the feed Facebook keeps behind an opened
post are skipped. A video-only post has no image. Files come straight from Facebook's image server
in their real format (usually JPEG).
"""

from __future__ import annotations

import re
import time
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import requests
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}
SAVED_STATUSES = {"downloaded", "partially_downloaded"}
CAPTION_MATCH = 0.4  # Share of the caption's key words an opened post must show.
SEARCH_MATCH = 0.8   # Stricter for search results, which can be other posts with similar text.

# Posts (not comments) on the page: element, text, and whether it is in the dialog of an opened post.
POST_TEXTS = r"""
return [...document.querySelectorAll('[role=article]')]
  .filter(a => !/^(comment|reply)/i.test(a.getAttribute('aria-label') || ''))
  .map(a => [a, (a.innerText || '').slice(0, 4000), !!a.closest('[role=dialog]')]);
"""
# Photo links with their tile images, and other pictures (link previews), inside one post.
POST_MEDIA = r"""
const root = arguments[0];
const inPost = el => { const a = el.closest('[role=article]'); return !(a && /^(comment|reply)/i.test(a.getAttribute('aria-label') || '')); };
const isPhotoLink = a => /\/photo(\/|\.php)?\?|\/photos\//.test(a.href);
const photos = [], tiles = [], images = [];
for (const a of root.querySelectorAll('a[href]')) {
  const img = a.querySelector('img');
  if (!img || !isPhotoLink(a) || !inPost(a) || photos.includes(a.href)) continue;
  photos.push(a.href);
  tiles.push(img.currentSrc || img.src);
}
for (const img of root.querySelectorAll('img')) {
  const link = img.closest('a');
  if (!inPost(img) || img.naturalWidth < 200 || img.naturalHeight < 150 || (link && isPhotoLink(link))) continue;
  const src = img.currentSrc || img.src;
  if (src && !images.includes(src)) images.push(src);
}
return {photos, tiles, images};
"""
# The photo viewer's own image; pictures inside posts and comments on the page are not it.
VIEWER_IMAGE = r"""
const imgs = [...document.querySelectorAll('img[data-visualcompletion="media-vc-image"]')]
  .filter(i => i.complete && i.naturalWidth > 0 && !i.closest('[role=article]'))
  .sort((a, b) => b.naturalWidth * b.naturalHeight - a.naturalWidth * a.naturalHeight);
return imgs.length ? imgs[0].currentSrc || imgs[0].src : null;
"""


class FacebookLoginRequired(RuntimeError):
    """Facebook showed a login or checkpoint page instead of a saved post."""


def check_login(driver: Any, url: str) -> None:
    if any(marker in str(driver.current_url).lower() for marker in ("/login", "/checkpoint", "/recover")):
        raise FacebookLoginRequired(
            f"Facebook showed a login or checkpoint page for {url}. "
            "Run the same command again with --login, sign in, then press Enter."
        )


def open_page(driver: Any, url: str) -> None:
    try:
        driver.get(url)
    except TimeoutException:
        pass  # Facebook often keeps rendering a usable page after the load timeout.
    check_login(driver, url)


def split_hashtag(match: re.Match) -> str:
    # The text cleaner's rule (preprocessors/text_cleaner.py): #BahaSaDavao -> Baha Sa Davao.
    word = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", match.group(1).replace("_", " "))
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", word)


def tokens(text: str) -> list[str]:
    """Plain lowercase words, so a cleaned caption can be compared with the post on the page."""
    text = unicodedata.normalize("NFKD", text or "")  # Also turns styled letters (𝗛𝗲𝗮𝘃𝘆) into plain ones.
    text = re.sub(r"#(\w+)", split_hashtag, "".join(char for char in text if not unicodedata.combining(char)))
    return re.findall(r"[a-z0-9]+", text.casefold())


def key_words(text: str, count: int = 20) -> list[str]:
    words: list[str] = []
    for word in tokens(text):
        if len(word) >= 4 and not word.isdigit() and word not in words:
            words.append(word)
            if len(words) == count:
                break
    return words


def caption_score(words: list[str], text: str) -> float:
    present = set(tokens(text))
    return sum(word in present for word in words) / len(words) if words else 0.0


def post_media(driver: Any, root: Any) -> dict[str, list[str]]:
    """Photo links, their tile images, and other pictures inside one post element."""
    media = driver.execute_script(POST_MEDIA, root) or {}
    return {key: list(media.get(key) or []) for key in ("photos", "tiles", "images")}


def expand_see_more(driver: Any, post: Any) -> None:
    try:
        button = post.find_element(By.XPATH, ".//div[contains(@role,'button') and contains(normalize-space(.),'See more')]")
        driver.execute_script("arguments[0].click();", button)
    except WebDriverException:
        pass


def find_post_media(driver: Any, caption: str, *, author: str = "", search: bool = False,
                    timeout: float = 15.0, settle: float = 5.0) -> dict[str, list[str]] | None:
    """Find the post showing this caption on the page and return its media, or None.

    An opened post sits in a dialog in front of the previous page's posts, so a match in the dialog
    wins. With ``author``, the post must also show all of the author's name. In ``search`` results,
    which can be other posts with similar text, the caption must match more closely, and each
    candidate's "See more" is opened, since results show only the start of a caption. A found post
    gets ``settle`` seconds for its images to load before it counts as having none.
    """
    words, names = key_words(caption), key_words(author, 8)
    threshold = SEARCH_MATCH if search else CAPTION_MATCH
    deadline = time.monotonic() + timeout
    found_at, media = None, None
    expanded: set = set()
    while time.monotonic() < deadline:
        try:
            matches = []
            for element, text, in_dialog in driver.execute_script(POST_TEXTS) or []:
                if names and caption_score(names, text) < 1.0:
                    continue
                score = caption_score(words, text) if words else float(in_dialog)
                if score >= threshold:
                    matches.append((in_dialog, score, element))
                elif search and element.id not in expanded:
                    expanded.add(element.id)
                    expand_see_more(driver, element)  # Compared again on the next look.
            if matches:
                media = post_media(driver, max(matches, key=lambda match: match[:2])[2])
                found_at = found_at or time.monotonic()
                if media["photos"] or media["images"] or time.monotonic() - found_at >= settle:
                    return media
        except WebDriverException:
            pass  # The page re-rendered between the two scripts; look again.
        time.sleep(1)
    return media


def photo_id(url: str) -> tuple[str, str]:
    query = parse_qs(urlparse(url).query)
    return query.get("fbid", [""])[0], query.get("set", [""])[0]


def viewer_image(driver: Any, previous: str | None = None, timeout: float = 8.0) -> str | None:
    """Wait for the photo viewer's full-size image (a new one after "Next photo")."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        source = driver.execute_script(VIEWER_IMAGE)
        if source and source != previous:
            return source
        time.sleep(0.3)
    return None


def next_photo(driver: Any, fbid: str, timeout: float = 8.0) -> bool:
    buttons = driver.find_elements(By.CSS_SELECTOR, '[aria-label="Next photo"]')
    if buttons:
        driver.execute_script("arguments[0].click();", buttons[0])
    else:
        driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ARROW_RIGHT)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if photo_id(driver.current_url)[0] not in ("", fbid):
            return True
        time.sleep(0.3)
    return False


def full_size_urls(driver: Any, photo_links: list[str], max_images: int) -> list[str]:
    """Open the post's photos in Facebook's viewer and return their full-size image URLs.

    A multi-photo post is walked with "Next photo" from its first photo until the set repeats,
    so photos hidden behind a "+N" tile are included. A single photo links to the page's album,
    where "Next photo" would leave the post, so it is opened on its own, as is any tile the walk
    missed.
    """
    urls: list[str] = []
    seen: set[str] = set()
    set_links = [link for link in photo_links if photo_id(link)[1].startswith("pcb.")]
    if set_links:
        open_page(driver, set_links[0])
        set_id, previous = photo_id(set_links[0])[1], None
        while len(urls) < max_images:
            fbid, current_set = photo_id(driver.current_url)
            if fbid in seen or (current_set and current_set != set_id):
                break
            seen.add(fbid)
            source = viewer_image(driver, previous)
            if source:  # A video in the set has no viewer image; skip it.
                urls.append(source)
                previous = source
            if not fbid or not next_photo(driver, fbid):
                break
    for link in photo_links:
        if len(urls) >= max_images:
            break
        if photo_id(link)[0] and photo_id(link)[0] in seen:
            continue
        open_page(driver, link)
        source = viewer_image(driver)
        if source and source not in urls:
            urls.append(source)
    return urls


def viewer_urls(driver: Any, photo_links: list[str], max_images: int, new_tab: bool) -> list[str]:
    if not new_tab:
        return full_size_urls(driver, photo_links, max_images)
    home = driver.current_window_handle
    driver.switch_to.new_window("tab")
    try:
        return full_size_urls(driver, photo_links, max_images)
    finally:
        try:
            driver.close()
        finally:
            driver.switch_to.window(home)


def download(url: str, stem: Path, attempts: int = 3) -> Path:
    """Save one image from Facebook's image server, named ``stem`` plus its real extension.

    The URL is used as given: its signed query string (``oh``, ``oe``) is what lets the file load.
    """
    problem: object = None
    for attempt in range(attempts):
        try:
            response = requests.get(url, timeout=30, headers={"User-Agent": USER_AGENT})
            kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
            if response.status_code == 200 and kind in EXTENSIONS and len(response.content) >= 256:
                path = stem.with_name(stem.name + EXTENSIONS[kind])
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_name(f".{path.name}.tmp")
                temporary.write_bytes(response.content)
                temporary.replace(path)
                return path
            problem = f"HTTP {response.status_code}, {kind or 'no content type'}, {len(response.content)} bytes"
        except requests.RequestException as error:
            problem = error
        time.sleep(1 + attempt)
    raise ValueError(f"Could not download {url[:80]}: {problem}")


def download_media(driver: Any, media: dict[str, list[str]], stem: Path, max_images: int,
                   *, new_tab: bool = False) -> dict[str, Any]:
    """Download a post's images as ``<stem>_01.jpg``, ``<stem>_02.jpg``, ...

    With ``new_tab``, photos are opened in a second tab so the caller's page (a feed) stays put.
    Returns the saved paths, the image URLs they came from, and a status.
    """
    urls: list[str] = []
    if media["photos"]:
        try:
            urls = viewer_urls(driver, media["photos"], max_images, new_tab)
        except WebDriverException:
            urls = []
        if not urls:  # The viewer failed; the post's own tiles are still real image files.
            urls = media["tiles"]
    if not urls:
        urls = media["images"]
    urls = list(dict.fromkeys(url for url in urls if url))[:max_images]
    if not urls:
        return {"paths": [], "sources": [], "status": "no_image_found"}
    paths, sources = [], []
    for number, url in enumerate(urls, 1):
        try:
            paths.append(download(url, stem.with_name(f"{stem.name}_{number:02d}")))
            sources.append(url)
        except ValueError:
            continue
    status = "downloaded" if len(paths) == len(urls) else "partially_downloaded" if paths else "download_error"
    return {"paths": paths, "sources": sources, "status": status}


def download_post_images(driver: Any, post_url: str, caption: str, stem: Path, max_images: int) -> dict[str, Any]:
    """Open a saved post, find it on the page by its caption, and download its images."""
    open_page(driver, post_url)
    media = find_post_media(driver, caption)
    if media is None:
        return {"paths": [], "sources": [], "status": "post_not_found"}
    return download_media(driver, media, stem, max_images)


def search_url(query: str) -> str:
    return "https://www.facebook.com/search/posts/?q=" + quote(query)


def search_queries(caption: str) -> list[str]:
    """The caption's opening words, then its first distinctive words, for two different result lists."""
    return list(dict.fromkeys(query for query in (" ".join(tokens(caption)[:10]), " ".join(key_words(caption, 8)))
                              if query))


def search_post_images(driver: Any, caption: str, author: str, stem: Path, max_images: int) -> dict[str, Any]:
    """Find a post that has no saved link by searching Facebook for its caption, then download its images.

    Only a result whose text closely matches the caption is accepted, and, when ``author`` is known,
    only one from that author.
    """
    for query in search_queries(caption):
        open_page(driver, search_url(query))
        media = find_post_media(driver, caption, author=author, search=True, timeout=12.0)
        if media is not None:
            return download_media(driver, media, stem, max_images)
    return {"paths": [], "sources": [], "status": "not_found_in_search"}
