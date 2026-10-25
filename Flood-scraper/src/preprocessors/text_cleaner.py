#!/usr/bin/env python3
"""Clean dated Facebook CSVs oldest first, without changing raw data or images.

Run: python src/preprocessors/text_cleaner.py
Use --next to clean just the oldest pending file in this invocation.
Filenames retain SCRAPE times; the manifest records actual CLEANING times.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import re
import sys
import tempfile
import unicodedata as ud
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[2]
RAW_DIR = BASE_DIR / "data" / "raw" / "facebook"
CLEANED_DIR = BASE_DIR / "data" / "cleaned"
PH_TIME = timezone(timedelta(hours=8))
CLEANER_VERSION = "1"
RAW_NAME = re.compile(r"fb_raw_floodData_(\d{2}-\d{2}-\d{4}_\d{6})(_\d+)?\.csv\Z")
MANIFEST_COLUMNS = [
    "source_file", "source_scrape_timestamp", "output_file", "cleaning_started_at",
    "cleaning_completed_at", "rows", "texts_changed", "empty_cleaned_texts",
    "source_sha256", "output_sha256", "cleaner_version",
]


class _VisibleHTML(HTMLParser):
    """Keep text boundaries, but not comments or script/style contents."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if not self.hidden:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1
        if not self.hidden:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def clean_text(value: str | None) -> str:
    """Normalize noise without a length cutoff, stopword deletion, or relabeling.

    Inspired by disaster-scraper/src/preprocessing/clean_text.py. Preserve
    Unicode place names, case, hashtag words, negation, short alerts, numeric
    punctuation, and apostrophes/hyphens inside names rather than ASCII-folding.
    """
    if value is None:
        return ""
    text = ud.normalize("NFKC", str(value))
    if text.strip().casefold() in {"", "nan", "none", "null", "<na>"}:
        return ""
    parser = _VisibleHTML()
    parser.feed(html.unescape(text))
    parser.close()
    text = html.unescape("".join(parser.parts))
    text = text.translate(str.maketrans({"\u2018": "'", "\u2019": "'", "\u2013": "-", "\u2014": "-", "\u2044": "/"}))
    text = re.sub(r"\b(?:https?://|www\.)[^\s<>]+", " ", text, flags=re.I)
    text = re.sub(r"\b(?:see more|see less|see translation|view translation)\b", " ", text, flags=re.I)
    text = re.sub(r"(?im)^\s*shared a post\b", " ", text)

    def hashtag(match):
        word = match.group(1).replace("_", " ")
        word = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", word)
        return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", word)

    text = re.sub(r"#(\w+)", hashtag, text)
    # Turn meaningful symbols into words rather than silently changing facts.
    for symbol, replacement in ((">=", " at least "), ("<=", " at most "),
                                ("\u2265", " at least "), ("\u2264", " at most "),
                                ("%", " percent "), ("\u20b1", " PHP "), ("&", " and ")):
        text = text.replace(symbol, replacement)
    text = re.sub(r"([<>])\s*(?=\d)", lambda m: " less than " if m[1] == "<" else " greater than ", text)
    text = re.sub(r"(?<=\d)\s*([-:/])\s*(?=\d)", r"\1", text)
    result = []
    for index, char in enumerate(text):
        category = ud.category(char)
        before = text[index - 1] if index else ""
        after = text[index + 1] if index + 1 < len(text) else ""
        if category == "Cf" or char in {"\ufe0e", "\ufe0f"}:
            continue
        if char.isalnum() or category in {"Mn", "Mc"} or char.isspace():
            result.append(char)
        elif char in ".,:/-" and before.isdigit() and after.isdigit():
            result.append(char)
        elif char in "'-/" and before.isalpha() and after.isalpha():
            result.append(char)
        elif char in "+-" and after.isdigit() and not before.isalnum():
            result.append(char)
        elif char == "." and after.isdigit() and not before.isalnum():
            result.append("0.")
        else:
            result.append(" ")
    return " ".join("".join(result).split())


def scrape_time(path: Path) -> datetime:
    match = RAW_NAME.fullmatch(path.name)
    if not match:
        raise ValueError(f"Not a dated Facebook raw CSV: {path.name}")
    return datetime.strptime(match[1], "%m-%d-%Y_%H%M%S").replace(tzinfo=PH_TIME)


def discover_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        raise ValueError(f"Raw directory does not exist: {directory}")
    paths = [p for p in directory.glob("*.csv") if RAW_NAME.fullmatch(p.name)]
    return sorted(paths, key=lambda p: (scrape_time(p), int((RAW_NAME.fullmatch(p.name)[2] or "_0")[1:])))


def output_name(source: Path) -> str:
    scrape_time(source)  # Validate before using the basename.
    return source.name.replace("fb_raw_floodData_", "fb_cleaned_floodData_", 1)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_csv(path: Path, columns: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8-sig", newline="", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _read_manifest(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != MANIFEST_COLUMNS:
            raise ValueError(f"Unexpected cleaning manifest columns: {path}")
        return list(reader)


def clean_files(input_dir: Path = RAW_DIR, output_dir: Path = CLEANED_DIR, *,
                next_only: bool = False, overwrite: bool = False) -> list[dict]:
    """Commit one file before starting the next. Raw files are never written."""
    input_dir, output_dir = Path(input_dir).resolve(), Path(output_dir).resolve()
    if input_dir == output_dir or output_dir.is_relative_to(input_dir) or input_dir.is_relative_to(output_dir):
        raise ValueError("Keep the raw and cleaned directories separate and non-nested.")
    files = discover_files(input_dir)
    manifest_path = output_dir / "_metadata" / "cleaning_manifest.csv"
    history = _read_manifest(manifest_path)
    completed = []
    for source in files:
        target = output_dir / output_name(source)
        raw_bytes = source.read_bytes()
        source_hash = _sha256(raw_bytes)
        if target.exists() and not overwrite:
            previous = next((r for r in reversed(history) if r["output_file"] == target.name), None)
            if (previous and previous["source_sha256"] == source_hash
                    and previous["output_sha256"] == _sha256(target.read_bytes())
                    and previous["cleaner_version"] == CLEANER_VERSION):
                print(f"Already cleaned: {source.name}")
                continue
            raise ValueError(f"Existing output is changed, stale, or has no audit record: {target}. Use --overwrite only to regenerate it.")
        started = datetime.now(PH_TIME).isoformat(timespec="microseconds")
        reader = csv.DictReader(io.StringIO(raw_bytes.decode("utf-8-sig"), newline=""), strict=True)
        columns = reader.fieldnames
        if not columns or "text" not in columns or len(set(columns)) != len(columns):
            raise ValueError(f"Missing text column or duplicate CSV column names in {source.name}")
        rows, changed, empty = [], 0, 0
        for number, row in enumerate(reader, start=2):
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"Malformed CSV row {number} in {source.name}; no output was written.")
            original = row["text"]
            row["text"] = clean_text(original)
            changed += row["text"] != original
            empty += not row["text"]
            rows.append(row)
        # Do not save a partial snapshot of a scraper that is still updating.
        if _sha256(source.read_bytes()) != source_hash:
            raise RuntimeError(f"Raw CSV changed while reading: {source.name}. Wait for scraping to finish.")
        _atomic_csv(target, columns, rows)
        record = dict(zip(MANIFEST_COLUMNS, [
            source.name, scrape_time(source).isoformat(), target.name, started,
            datetime.now(PH_TIME).isoformat(timespec="microseconds"), len(rows), changed, empty,
            source_hash, _sha256(target.read_bytes()), CLEANER_VERSION,
        ]))
        history.append(record)
        _atomic_csv(manifest_path, MANIFEST_COLUMNS, history)
        completed.append(record)
        print(f"Cleaned: {target.name} | posts: {len(rows)} | empty text: {empty}")
        if next_only:
            break
    return completed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=RAW_DIR)
    parser.add_argument("--output-dir", type=Path, default=CLEANED_DIR)
    parser.add_argument("--next", action="store_true", help="Clean only the oldest pending CSV; rerun for the next file.")
    parser.add_argument("--overwrite", action="store_true", help="Regenerate existing cleaned CSVs (raw data stays unchanged).")
    args = parser.parse_args()
    try:
        results = clean_files(args.input_dir, args.output_dir, next_only=args.next, overwrite=args.overwrite)
        print(f"Files cleaned this run: {len(results)}")
        return 0
    except KeyboardInterrupt:
        print("Cleaning stopped. Previously committed CSVs are retained.", file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError, csv.Error, UnicodeError) as error:
        print(f"Cleaning stopped: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
