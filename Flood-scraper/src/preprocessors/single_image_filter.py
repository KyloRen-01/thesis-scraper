#!/usr/bin/env python3
"""Keep deduplicated posts with exactly one available saved image.

Run: python src/preprocessors/single_image_filter.py
This filters attachment records, NOT flood content, authenticity, or collages.
No NER is performed. Input CSVs and image files are never modified.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

try:
    from .text_cleaner import BASE_DIR, PH_TIME, _atomic_csv
    from .text_deduplication import post_identity
except ImportError:  # Direct script invocation.
    from text_cleaner import BASE_DIR, PH_TIME, _atomic_csv
    from text_deduplication import post_identity


INPUT_DIR = BASE_DIR / "data/deduplicated"
OUTPUT_DIR = BASE_DIR / "data/single_image"
VERSION = "1"
AUDIT_COLUMNS = ["image_filter_action", "image_filter_reason", "image_filter_source_row"]
MANIFEST_COLUMNS = [
    "source_file", "output_file", "started_at", "completed_at", "input_rows", "kept_rows", "excluded_rows",
    "reason_counts", "input_sha256", "output_sha256", "decisions_sha256", "evidence_sha256", "version",
]


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def discover(directory: Path) -> list[Path]:
    # Reuse the existing deduplicated-name parser; do not include metadata CSVs.
    import re
    pattern = re.compile(r"fb_deduplicate_floodData_(\d{2}-\d{2}-\d{4}_\d{6})(_\d+)?\.csv\Z")
    if not directory.is_dir():
        raise ValueError(f"Deduplicated directory not found: {directory}")
    return sorted((p for p in directory.glob("*.csv") if pattern.fullmatch(p.name)), key=lambda p:
        (datetime.strptime(pattern.fullmatch(p.name)[1], "%m-%d-%Y_%H%M%S"),
         int((pattern.fullmatch(p.name)[2] or "_0")[1:])))


def decision(row: dict[str, str], project: Path) -> str:
    """Validate one saved path and matching declared count, not image semantics."""
    try:
        count = int(row["image_count"])
        if count < 0:
            raise ValueError
    except ValueError:
        return "invalid_image_count"
    paths = [value.strip() for value in row["image_paths"].split(";") if value.strip()]
    if count != len(paths):
        return "image_count_path_mismatch"
    if count == 0:
        return "no_saved_image"
    if count != 1:
        return "multiple_saved_images"
    try:
        path = (project / paths[0].replace("\\", "/")).resolve()
        if not path.is_relative_to(project):
            return "unsafe_image_path"
        if not path.is_file():
            return "missing_image_file"
    except (OSError, ValueError):
        return "unsafe_image_path"
    return "exactly_one_saved_image"


def read_source(path: Path):
    raw = path.read_bytes()
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""), strict=True)
    columns = reader.fieldnames
    if not columns or len(columns) != len(set(columns)) or not {"image_paths", "image_count"}.issubset(columns):
        raise ValueError(f"Missing image columns or duplicate CSV headers: {path}")
    if set(columns).intersection(AUDIT_COLUMNS):
        raise ValueError(f"Input contains reserved filter columns: {path}")
    rows = list(reader)
    if any(None in row or any(value is None for value in row.values()) for row in rows):
        raise ValueError(f"Malformed CSV record: {path}")
    return columns, rows, digest(raw)


def read_manifest(path: Path):
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, strict=True)
        if reader.fieldnames != MANIFEST_COLUMNS:
            raise ValueError(f"Invalid single-image manifest: {path}")
        rows = list(reader)
    if any(None in row or any(value is None for value in row.values()) for row in rows):
        raise ValueError(f"Malformed single-image manifest: {path}")
    return rows


def filter_files(input_dir=INPUT_DIR, output_dir=OUTPUT_DIR, *, project_dir=BASE_DIR,
                 overwrite=False, next_only=False):
    project = Path(project_dir).resolve()
    input_dir, output_dir = Path(input_dir).resolve(), Path(output_dir).resolve()
    for protected in [input_dir, project / "src", *(project / "data" / name for name in
                      ["raw", "cleaned", "deduplicated", "ner", "lookup"])]:
        if output_dir == protected or output_dir.is_relative_to(protected) or protected.is_relative_to(output_dir):
            raise ValueError("Filtered output must be separate from source code, source data, NER, and lookup directories.")
    sources = discover(input_dir)
    if not sources:
        raise ValueError("No dated deduplicated Facebook CSVs found.")
    manifest = output_dir / "_metadata/single_image_manifest.csv"
    history = read_manifest(manifest)
    completed = []
    for source in sources:
        started = datetime.now(PH_TIME).isoformat(timespec="microseconds")
        columns, rows, checksum = read_source(source)
        reasons = [decision(row, project) for row in rows]
        # Recompute decisions on rerun so missing/restored image files invalidate
        # a prior result even when the source CSV itself is unchanged.
        evidence = digest(json.dumps(reasons, separators=(",", ":")).encode())
        name = source.name.replace("fb_deduplicate_floodData_", "fb_single_image_floodData_", 1)
        target = output_dir / name
        audit = output_dir / "_metadata" / name.replace(".csv", "_decisions.csv")
        previous = next((r for r in reversed(history) if r["output_file"] == name), None)
        if (target.exists() or audit.exists() or previous) and not overwrite:
            if not (previous and target.is_file() and audit.is_file()
                    and previous["input_sha256"] == checksum and previous["evidence_sha256"] == evidence
                    and previous["version"] == VERSION and previous["output_sha256"] == digest(target.read_bytes())
                    and previous["decisions_sha256"] == digest(audit.read_bytes())):
                raise ValueError(f"Changed, missing, or unaudited filtered output: {target}. Use --overwrite to regenerate.")
            print(f"Already filtered: {name}")
            continue
        kept, decisions = [], []
        for number, (row, reason) in enumerate(zip(rows, reasons), 2):
            keep = reason == "exactly_one_saved_image"
            if keep:
                kept.append(row)
            decisions.append({**row, **dict(zip(AUDIT_COLUMNS, ["keep" if keep else "exclude", reason, str(number)]))})
        if digest(source.read_bytes()) != checksum or [decision(row, project) for row in rows] != reasons:
            raise RuntimeError(f"Source data or image availability changed while filtering {source.name}; retry when stable.")
        _atomic_csv(target, columns, kept)
        _atomic_csv(audit, columns + AUDIT_COLUMNS, decisions)
        record = dict(zip(MANIFEST_COLUMNS, [source.name, name, started,
            datetime.now(PH_TIME).isoformat(timespec="microseconds"), len(rows), len(kept), len(rows) - len(kept),
            json.dumps(dict(Counter(reasons)), sort_keys=True), checksum, digest(target.read_bytes()),
            digest(audit.read_bytes()), evidence, VERSION]))
        history.append(record)
        _atomic_csv(manifest, MANIFEST_COLUMNS, history)
        completed.append(record)
        print(f"Filtered: {name} | kept: {len(kept)} | excluded: {len(rows) - len(kept)}")
        if next_only:
            break
    return completed


def summarize(output_dir=OUTPUT_DIR):
    """Describe the retained records; missing URLs are not claimed unique posts."""
    output_dir = Path(output_dir)
    rows = []
    for path in sorted(output_dir.glob("fb_single_image_floodData_*.csv")):
        rows.extend(read_source(path)[1])
    return {"kept_records": len(rows), "distinct_image_paths": len({r["image_paths"] for r in rows}),
            "distinct_identifiable_post_ids": len({post_identity(r.get("post_url", "")) for r in rows
                                                  if post_identity(r.get("post_url", ""))}),
            "records_without_post_url": sum(not r.get("post_url", "").strip() for r in rows)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=INPUT_DIR)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--next", action="store_true", help="Process only the oldest pending file.")
    parser.add_argument("--overwrite", action="store_true", help="Regenerate stale outputs; sources remain unchanged.")
    args = parser.parse_args(argv)
    csv.field_size_limit(10_000_000)
    try:
        results = filter_files(args.input_dir, args.output_dir, overwrite=args.overwrite, next_only=args.next)
        print(f"Files filtered this run: {len(results)}")
        print(json.dumps(summarize(args.output_dir), sort_keys=True))
        print("Saved-image count filter only: flood content, collages, and authenticity still need review. No NER was run.")
    except KeyboardInterrupt:
        print("Filtering stopped. Completed output files are retained.", file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError, csv.Error, UnicodeError) as error:
        print(f"Filtering stopped: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
