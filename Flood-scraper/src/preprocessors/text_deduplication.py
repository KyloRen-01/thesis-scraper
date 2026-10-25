#!/usr/bin/env python3
"""Deduplicate cleaned Facebook CSVs chronologically; never modify inputs/photos.

Run from Flood-scraper: python src/preprocessors/text_deduplication.py
Unknown event/publication times are not replaced with collection times.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sys
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

try:
    from .text_cleaner import BASE_DIR, PH_TIME, _atomic_csv
except ImportError:  # Direct script invocation.
    from text_cleaner import BASE_DIR, PH_TIME, _atomic_csv


INPUT_DIR = BASE_DIR / "data/cleaned"
OUTPUT_DIR = BASE_DIR / "data/deduplicated"
LOOKUP_DIR = BASE_DIR / "data/lookup"
VERSION = "1"
PREFIX_LEN, PREFIX_THRESHOLD, FULL_THRESHOLD = 300, 97, 90
NAME = re.compile(r"fb_cleaned_floodData_(\d{2}-\d{2}-\d{4}_\d{6})(_\d+)?\.csv\Z")
MISSING = {"", "unknown", "nan", "none", "null", "nat", "<na>", "n/a"}
AUDIT_COLUMNS = ["dedup_action", "dedup_reason", "dedup_source_row", "dedup_match_file",
                 "dedup_match_row", "dedup_prefix_score", "dedup_full_score",
                 "dedup_locations", "dedup_time"]
MANIFEST_COLUMNS = ["source_file", "output_file", "source_scrape_timestamp", "started_at", "completed_at",
                    "input_rows", "kept_rows", "removed_rows", "input_sha256", "output_sha256",
                    "decisions_sha256", "context_sha256", "version"]


def normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def present(value: str) -> bool:
    return normalize(value) not in MISSING


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def similarity(left: str, right: str) -> float:
    """Normalized Indel similarity, as in the senior script's fuzz.ratio.

    Bitset longest common subsequence avoids adding a RapidFuzz dependency.
    Scores measure character similarity, not probability or truth.
    """
    if left == right:
        return 100.0
    if not left or not right:
        return 0.0
    if len(left) > len(right):
        left, right = right, left
    masks = {}
    for index, char in enumerate(left):
        masks[char] = masks.get(char, 0) | (1 << index)
    state = 0
    for char in right:
        combined = state | masks.get(char, 0)
        state = combined & ~(combined - ((state << 1) | 1))
    return 200.0 * state.bit_count() / (len(left) + len(right))


def post_identity(url: str) -> str:
    """Recognize post permalinks, not profile/search URLs or tracking tokens."""
    try:
        parsed = urlsplit(url.strip())
        if parsed.hostname not in {"facebook.com", "www.facebook.com", "m.facebook.com", "mbasic.facebook.com"}:
            return ""
        query = parse_qs(parsed.query)
        match = re.search(r"/(?:posts|reel|videos|permalink)/([A-Za-z0-9]+)(?:/|$)", parsed.path)
        token = match[1] if match else (query.get("story_fbid") or query.get("v") or query.get("fbid") or [""])[0]
        return token if re.fullmatch(r"(?:\d+|pfbid[A-Za-z0-9]+)", token) else ""
    except ValueError:
        return ""


class LocationMatcher:
    """Exact place mentions, not an NER model or proof of an event location."""

    def __init__(self, lookup_dir: Path = LOOKUP_DIR):
        self.aliases = {name: name for name in ["davao", "davao city", "tagum", "panabo", "samal",
                                              "mati", "digos", "cebu", "manila", "quezon city",
                                              "cagayan de oro", "general santos", "metro manila"]}
        self.aliases.update({"gensan": "general santos", "city of davao": "davao city"})
        self.checksums = []
        for filename, column in [("refcitymun.csv", "citymunDesc"), ("refprovince.csv", "provDesc")]:
            path = Path(lookup_dir) / filename
            if not path.exists():
                self.checksums.append((filename, "missing"))
                continue
            data = path.read_bytes()
            self.checksums.append((filename, sha256(data)))
            reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig")))
            if not reader.fieldnames or column not in reader.fieldnames:
                raise ValueError(f"Missing {column} in {path}")
            for row in reader:
                name = normalize(re.sub(r"\s*\([^)]*\)", "", row[column]))
                if name:
                    self.aliases[name] = name
                    if name.startswith("city of "):
                        alias = name[8:] + " city"
                        self.aliases[name] = self.aliases[alias] = alias
        self.pattern = re.compile(r"(?<!\w)(?:" + "|".join(re.escape(n) for n in sorted(self.aliases, key=lambda n: (-len(n), n))) + r")(?!\w)")

    def extract(self, row: dict[str, str], text: str) -> tuple[str, ...]:
        structured = [normalize(row.get(key, "")) for key in ("location", "locations", "location_text")
                      if present(row.get(key, ""))]
        # Retain BOTH structured values and caption mentions, so conflicts cannot be hidden.
        return tuple(sorted(set(["field:" + item for item in structured]
                                + [self.aliases[m[0]] for m in self.pattern.finditer(text)])))


DATE = re.compile(r"\b(?:\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}[-/]\d{4}|"
                  r"(?:january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2},?\s+\d{4})\b")
CLOCK = re.compile(r"(?<![\w:])(?:\d{1,2}:\d{2}(?::\d{2})?\s*(?:am|pm)?|\d{1,2}\s*(?:am|pm))\b")


def time_evidence(row: dict[str, str], text: str) -> tuple[str, ...]:
    # Prefer explicit structured timestamps; never use scraped_timestamp.
    for key in ("event_timestamp", "event_time", "post_timestamp"):
        value = row.get(key, "")
        if not present(value):
            continue
        try:
            stamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            if not re.search(r"[T ]\d{1,2}:\d{2}", value):
                continue  # A date alone is insufficient to distinguish updates.
            stamp = stamp.replace(tzinfo=PH_TIME) if stamp.tzinfo is None else stamp.astimezone(PH_TIME)
            return (key, stamp.isoformat())
        except ValueError:
            continue
    dates, clocks = set(), set()
    for match in DATE.finditer(text):
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%m-%d-%Y", "%B %d %Y"):
            try:
                dates.add(datetime.strptime(match[0].replace(",", ""), fmt).date().isoformat())
                break
            except ValueError:
                pass
    for match in CLOCK.finditer(text):
        clock = re.sub(r"\s+", "", match[0])
        for fmt in ("%I:%M:%S%p", "%I:%M%p", "%I%p", "%H:%M:%S", "%H:%M"):
            try:
                clocks.add(datetime.strptime(clock, fmt).time().isoformat())
                break
            except ValueError:
                pass
    return ("caption", *sorted(dates), *sorted(clocks)) if dates and clocks else ()


def content_signature(text: str) -> tuple:
    """Reject changed facts even if similarity is very high.

    Only a small whitelist of closing courtesy phrases can be ignored. This
    intentionally retains typos/paraphrases and unrecognized locations for review.
    """
    tail = re.compile(r"(?:\b(?:stay safe|keep safe|thank you|thanks|please share|ingat po|ingat|amping|everyone))[.!\s]*$")
    while True:
        shorter = tail.sub("", text).strip()
        if shorter == text:
            break
        text = shorter
    return tuple(re.findall(r"\w+(?:[.'’:/-]\w+)*", text))


@dataclass
class Post:
    row: dict[str, str]
    file: str
    number: int
    text: str
    identity: str
    locations: tuple[str, ...]
    time: tuple[str, ...]
    images: frozenset[str]
    signature: tuple


class Deduplicator:
    def __init__(self, matcher: LocationMatcher):
        self.matcher = matcher
        self.exact = defaultdict(list)
        self.context = defaultdict(list)

    def inspect(self, row: dict[str, str], filename: str, number: int) -> tuple[Post, Post | None, str, float, float]:
        text = normalize(row["text"])
        item = Post(row, filename, number, text, post_identity(row.get("post_url", "")),
                    self.matcher.extract(row, text), time_evidence(row, text),
                    frozenset(p.strip() for p in row.get("image_paths", "").split(";") if p.strip()),
                    content_signature(text))
        source = normalize(row.get("source", "Facebook"))
        candidates = list(self.exact[(source, text)]) if text else []
        if text and item.locations and item.time:
            candidates += [p for p in self.context[(source, item.locations, item.time)] if p.text != text]
        new_images = False
        for previous in candidates:
            same_context = bool(item.locations and item.time and item.locations == previous.locations and item.time == previous.time)
            same_post = bool(item.identity and item.identity == previous.identity)
            conflict = ((item.locations and previous.locations and item.locations != previous.locations)
                        or (item.time and previous.time and item.time != previous.time))
            exact_capture = text == previous.text and same_post and not conflict
            if not exact_capture and not same_context:
                continue
            prefix = similarity(text[:PREFIX_LEN], previous.text[:PREFIX_LEN])
            full = similarity(text, previous.text)
            if prefix < PREFIX_THRESHOLD or full < FULL_THRESHOLD or item.signature != previous.signature:
                continue
            if not item.images.issubset(previous.images):
                new_images = True
                continue
            return item, previous, "same_post_exact_text" if exact_capture else "same_location_time_and_content", prefix, full
        if text:
            self.exact[(source, text)].append(item)
            if item.locations and item.time:
                self.context[(source, item.locations, item.time)].append(item)
        reason = ("empty_text" if not text else "new_image_references" if new_images else
                  "insufficient_location_or_time" if not item.locations or not item.time else "distinct_update")
        return item, None, reason, 0.0, 0.0


def file_time(path: Path) -> datetime:
    match = NAME.fullmatch(path.name)
    if not match:
        raise ValueError(f"Not a dated cleaned Facebook CSV: {path.name}")
    return datetime.strptime(match[1], "%m-%d-%Y_%H%M%S").replace(tzinfo=PH_TIME)


def output_name(path: Path) -> str:
    file_time(path)
    return path.name.replace("fb_cleaned_floodData_", "fb_deduplicate_floodData_", 1)


def discover_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        raise ValueError(f"Cleaned directory does not exist: {directory}")
    return sorted((p for p in directory.glob("*.csv") if NAME.fullmatch(p.name)),
                  key=lambda p: (file_time(p), int((NAME.fullmatch(p.name)[2] or "_0")[1:])))


def read_manifest(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, strict=True)
        if reader.fieldnames != MANIFEST_COLUMNS:
            raise ValueError(f"Unexpected deduplication manifest: {path}")
        return list(reader)


def deduplicate_files(input_dir: Path = INPUT_DIR, output_dir: Path = OUTPUT_DIR, *,
                      lookup_dir: Path = LOOKUP_DIR, next_only: bool = False, overwrite: bool = False) -> list[dict]:
    if next_only and overwrite:
        raise ValueError("Use --overwrite without --next to rebuild every dependent output.")
    input_dir, output_dir = Path(input_dir).resolve(), Path(output_dir).resolve()
    protected = [input_dir, (BASE_DIR / "data/raw").resolve(), Path(lookup_dir).resolve()]
    if any(output_dir == p or output_dir.is_relative_to(p) or p.is_relative_to(output_dir) for p in protected):
        raise ValueError("Keep output separate from cleaned inputs, raw data, and lookup directories.")
    files = discover_files(input_dir)
    matcher = LocationMatcher(lookup_dir)
    manifest_path = output_dir / "_metadata/deduplication_manifest.csv"
    history = read_manifest(manifest_path)
    latest = {r["source_file"]: r for r in history}
    context = sha256(json.dumps([VERSION, PREFIX_LEN, PREFIX_THRESHOLD, FULL_THRESHOLD, matcher.checksums]).encode())
    jobs = []
    # Validate every previous output before writing anything. A newly inserted
    # older source changes downstream decisions and requires an explicit rebuild.
    for source in files:
        data = source.read_bytes()
        digest = sha256(data)
        context = sha256((context + source.name + digest).encode())
        target = output_dir / output_name(source)
        decisions = output_dir / "_metadata" / (target.stem + "_decisions.csv")
        skip = False
        if not overwrite and (target.exists() or decisions.exists() or source.name in latest):
            previous = latest.get(source.name)
            skip = bool(previous and target.exists() and decisions.exists()
                        and previous["context_sha256"] == context
                        and previous["output_sha256"] == sha256(target.read_bytes())
                        and previous["decisions_sha256"] == sha256(decisions.read_bytes()))
            if not skip:
                raise ValueError(f"Changed, stale, missing, or unaudited output for {source.name}. Use --overwrite to rebuild all outputs.")
        jobs.append((source, data, digest, context, target, decisions, skip))
    engine, completed = Deduplicator(matcher), []
    for source, data, digest, context, target, decisions, skip in jobs:
        started = datetime.now(PH_TIME).isoformat(timespec="microseconds")
        reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig"), newline=""), strict=True)
        columns = reader.fieldnames
        if not columns or "text" not in columns or len(columns) != len(set(columns)) or set(columns) & set(AUDIT_COLUMNS):
            raise ValueError(f"Missing text or duplicate/reserved column names in {source.name}")
        kept, audit = [], []
        for number, row in enumerate(reader, start=2):
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"Malformed CSV row {number} in {source.name}")
            item, previous, reason, prefix, full = engine.inspect(row, source.name, number)
            if previous is None:
                kept.append(row)
            audit.append({**row, **dict(zip(AUDIT_COLUMNS, [
                "removed" if previous else "kept", reason, number, previous.file if previous else "",
                previous.number if previous else "", f"{prefix:.4f}" if previous else "",
                f"{full:.4f}" if previous else "", json.dumps(item.locations, ensure_ascii=False), json.dumps(item.time),
            ]))})
        if sha256(source.read_bytes()) != digest:
            raise RuntimeError(f"Cleaned CSV changed while reading: {source.name}")
        if skip:
            print(f"Already deduplicated: {source.name}")
            continue  # Replaying earlier inputs reconstructs the cross-file state.
        _atomic_csv(target, columns, kept)
        _atomic_csv(decisions, columns + AUDIT_COLUMNS, audit)
        record = dict(zip(MANIFEST_COLUMNS, [
            source.name, target.name, file_time(source).isoformat(), started,
            datetime.now(PH_TIME).isoformat(timespec="microseconds"), len(audit), len(kept), len(audit) - len(kept),
            digest, sha256(target.read_bytes()), sha256(decisions.read_bytes()), context, VERSION,
        ]))
        history.append(record)
        _atomic_csv(manifest_path, MANIFEST_COLUMNS, history)
        completed.append(record)
        print(f"Deduplicated: {target.name} | kept: {len(kept)} | duplicates: {len(audit) - len(kept)}")
        if next_only:
            break
    return completed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=INPUT_DIR)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--lookup-dir", type=Path, default=LOOKUP_DIR)
    parser.add_argument("--next", action="store_true", help="Write just the oldest pending file.")
    parser.add_argument("--overwrite", action="store_true", help="Rebuild outputs against all current inputs.")
    args = parser.parse_args()
    if args.next and args.overwrite:
        parser.error("Use --overwrite without --next: changing earlier decisions requires a complete rebuild.")
    try:
        records = deduplicate_files(args.input_dir, args.output_dir, lookup_dir=args.lookup_dir,
                                    next_only=args.next, overwrite=args.overwrite)
        print(f"Files deduplicated this run: {len(records)}")
        return 0
    except KeyboardInterrupt:
        print("Deduplication stopped; committed files are retained.", file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError, UnicodeError, csv.Error) as error:
        print(f"Deduplication stopped: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
