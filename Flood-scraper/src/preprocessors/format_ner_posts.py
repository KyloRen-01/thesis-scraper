"""Compact combined NER post schema, inspired by the senior's format_columns.py.

Separate stages keep full extraction evidence; combined posts retain the
requested seven fields. No row filtering, deduplication, or date inference occurs.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, unquote, urlsplit

from preprocessors.text_cleaner import clean_text


FORMAT_VERSION = "2"
FINAL_COLUMNS = ["source", "page_source", "text_content", "datetime", "location", "image_paths", "url"]
MISSING = {"", "unknown", "none", "null", "nan", "nat", "n/a", "<na>"}
ENGLISH_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
                  "September", "October", "November", "December")
PH_TIME = timezone(timedelta(hours=8))

# Organization names follow disaster-scraper/src/preprocessing/extract_pagesource.py.
SOURCE_NAME_MAP = {
    "abscbnnews": "ABS-CBN",
    "atenews": "Atenews",
    "bfprhq11": "BFPRHQ11",
    "civildefensedavao": "Civil Defense Davao",
    "davaocitydrmmc": "Davao City DRRMC",
    "davaodrrmo": "Davao DRRMO",
    "davaocitydisasterradio": "Davao City Disaster Radio",
    "dzmmteleradyo.mspc": "DZMM Teleradyo MSPC",
    "gmanews": "GMA News",
    "inquirerdotnet": "Inquirer.net",
    "manilabulletin": "Manila Bulletin",
    "mprsdcdo": "MPRSD CDO",
    "mindanews": "Mindanews",
    "mrpsd.com.ph": "MRPSD",
    "ndrrmc": "NDRRMC",
    "pagasa.dost.gov.ph": "PAGASA",
    "philstarnews": "Philstar News",
    "phivolcs": "PHIVOLCS",
    "piagovernment": "PIA Government",
    "rapplerdotcom": "Rappler",
    "sunstardavaonews": "SunStar Davao",
    "sunstarphilippines": "SunStar Philippines",
    "stormchasersph": "Storm Chasers PH",
    "untvnewsrescue": "UNTV News Rescue",
    "weather.davao": "Weather Davao",
}


def present(value: str | None) -> str:
    value = (value or "").strip()
    return "" if value.casefold() in MISSING else value


def display_datetime(value: str | None) -> str:
    """Display known ISO values in English/PHT; never infer a missing time/date."""
    value = present(value)
    if not value:
        return ""
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            stamp = date.fromisoformat(value)
            return f"{ENGLISH_MONTHS[stamp.month - 1]} {stamp.day:02d}, {stamp.year:04d}"
        if not re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", value):
            return value  # Already displayed/legacy text: do not guess its meaning.
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is not None:
            stamp = stamp.astimezone(PH_TIME)
        return (f"{ENGLISH_MONTHS[stamp.month - 1]} {stamp.day:02d}, {stamp.year:04d} "
                f"{stamp.hour:02d}:{stamp.minute:02d}:{stamp.second:02d}")
    except ValueError:
        return value


def page_source(url: str, author: str = "", existing: str = "") -> str:
    """Prefer URL page identity, then recorded identity; never a search keyword."""
    identity = ""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if parts.scheme in {"http", "https"} and (
            host in {"facebook.com", "x.com", "twitter.com"}
            or host.endswith(".facebook.com")
            or host in {"www.x.com", "www.twitter.com", "mobile.twitter.com"}
        ):
            segments = [unquote(value) for value in parts.path.split("/") if value]
            first = segments[0].casefold() if segments else ""
            if first == "hashtag" and len(segments) > 1:
                return "#" + segments[1].lower()
            if first in {"profile.php", "permalink.php", "story.php", "photo.php"}:
                identity = parse_qs(parts.query).get("id", [""])[0]
            elif first == "groups" and len(segments) > 1:
                identity = "group:" + segments[1]
            elif first and first not in {"watch", "reel", "reels", "photo", "photos", "posts", "share", "search", "login", "i"}:
                identity = segments[0]
    except ValueError:
        pass
    if identity:
        identity = identity.strip().strip(".@/").lower()
        return SOURCE_NAME_MAP.get(identity, identity)
    return present(existing) or present(author) or "Unknown"


def format_post(row: dict[str, str]) -> dict[str, str]:
    """Project one row without altering extracted location, URLs, or image paths."""
    url = row.get("post_url", row.get("url", ""))
    text = row.get("text", row.get("text_content", ""))
    # Reuse the project's Unicode-aware cleanup; lowercase as in the senior's
    # cleaner, but keep short alerts and unknown/blank rows instead of filtering.
    return {
        "source": row.get("source", ""),
        "page_source": page_source(url, row.get("user", ""), row.get("page_source", "")),
        "text_content": clean_text(text).lower(),
        "datetime": display_datetime(present(row.get("event_time_primary_iso")) or present(row.get("event_time_primary")) or present(row.get("datetime"))),
        "location": row.get("location", ""),
        "image_paths": row.get("image_paths", ""),
        "url": url,
    }
