"""Consolidate historical Facebook post/image CSVs without discarding originals.

Run without flags to inspect the plan; pass --apply to perform it. The regular
Facebook scraper already writes combined CSVs and does not call this script.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import shutil
from datetime import datetime
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[2]
FACEBOOK = PROJECT / "data" / "raw" / "facebook"
ENRICHED = FACEBOOK / "with_images"
IMAGE_COLUMNS = ("image_paths", "image_count", "image_status")
DATED_NAME = re.compile(r"^fb_raw_floodData_\d{2}-\d{2}-\d{4}_\d{6}\.csv$", re.I)
EXTRA_NAMES = ("Gempesaw_FloodData.csv", "fb_raw_floodData_latest.csv")
OLD_IMAGE_PREFIX = "data/raw/facebook/gempesaw_images/"


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None:
            raise ValueError(f"CSV has no header: {path}")
        return list(reader.fieldnames), list(reader)


def write_csv(path: Path, columns: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def merge_rows(raw_path: Path | None, enriched_path: Path) -> tuple[list[str], list[dict[str, str]]]:
    enriched_columns, enriched_rows = read_csv(enriched_path)
    if not all(column in enriched_columns for column in IMAGE_COLUMNS):
        raise ValueError(f"Missing image columns: {enriched_path}")
    if raw_path is None:
        return enriched_columns, enriched_rows

    raw_columns, raw_rows = read_csv(raw_path)
    if len(raw_rows) != len(enriched_rows):
        raise ValueError(f"Row count changed between {raw_path.name} and {enriched_path.name}")
    identity_columns = [column for column in ("source", "query_page", "text", "post_url") if column in raw_columns]
    for number, (raw_row, image_row) in enumerate(zip(raw_rows, enriched_rows), start=1):
        if any(raw_row[column] != image_row.get(column, "") for column in identity_columns):
            raise ValueError(f"Post mismatch in {raw_path.name} at row {number}")
        for column in IMAGE_COLUMNS:
            if not raw_row.get(column):
                raw_row[column] = image_row.get(column, "")
    columns = raw_columns + [column for column in IMAGE_COLUMNS if column not in raw_columns]
    return columns, raw_rows


def image_paths(rows: list[dict[str, str]]) -> list[str]:
    return [path for row in rows for path in row.get("image_paths", "").split(";") if path]


def resolve_image_path(value: str) -> Path:
    path = (PROJECT / value).resolve()
    if not path.is_relative_to(PROJECT.resolve()):
        raise ValueError(f"Image path leaves the project: {value}")
    return path


def validate_move(source: Path, destination: Path) -> None:
    project = PROJECT.resolve()
    for path in (source, destination):
        resolved = path.resolve()
        if not resolved.is_relative_to(project) or resolved == project:
            raise ValueError(f"Move target is outside the project: {path}")
    if not source.exists() or destination.exists():
        raise ValueError(f"Unsafe move from {source} to {destination}")


def replace_image_prefix(rows: list[dict[str, str]], new_prefix: str) -> None:
    for row in rows:
        if "image_paths" in row:
            row["image_paths"] = row["image_paths"].replace(OLD_IMAGE_PREFIX, new_prefix)


def plan() -> list[tuple[Path | None, Path, Path, list[str], list[dict[str, str]]]]:
    if not ENRICHED.is_dir():
        raise ValueError(f"Enriched CSV directory is missing: {ENRICHED}")
    pairs = []
    for enriched in sorted(ENRICHED.glob("*_with_images.csv")):
        target = FACEBOOK / f"{enriched.stem.removesuffix('_with_images')}.csv"
        raw = target if target.exists() else None
        columns, rows = merge_rows(raw, enriched)
        for value in image_paths(rows):
            if not resolve_image_path(value).is_file():
                raise ValueError(f"Missing image referenced by {enriched.name}: {value}")
        pairs.append((raw, enriched, target, columns, rows))
    if not pairs:
        raise ValueError("No enriched CSVs found")
    expected = {target.name for _raw, _enriched, target, _columns, _rows in pairs}
    expected.update(EXTRA_NAMES)
    unexpected = [path.name for path in FACEBOOK.glob("*.csv") if path.name not in expected]
    if unexpected:
        raise ValueError(f"Unmatched root CSVs need review before migration: {unexpected}")
    return pairs


def rebase_archived_csv(path: Path, new_prefix: str) -> None:
    columns, rows = read_csv(path)
    if "image_paths" not in columns:
        return
    replace_image_prefix(rows, new_prefix)
    write_csv(path, columns, rows)


def rebase_manifest(path: Path, new_images: Path) -> None:
    if not path.is_file():
        return
    columns, rows = read_csv(path)
    if "Path" not in columns:
        return
    for row in rows:
        row["Path"] = row["Path"].replace(
            str(FACEBOOK / "gempesaw_images") + os.sep,
            str(new_images) + os.sep,
        )
    write_csv(path, columns, rows)


def consolidate(pairs: list[tuple[Path | None, Path, Path, list[str], list[dict[str, str]]]]) -> Path:
    archive = PROJECT / "data" / "archive" / "facebook" / datetime.now().strftime("%Y%m%d_%H%M%S")
    if archive.exists():
        raise FileExistsError(f"Archive destination already exists: {archive}")
    archive.mkdir(parents=True)
    archive_images = archive / "gempesaw_images"
    new_prefix = archive_images.relative_to(PROJECT).as_posix() + "/"

    # Back up all original root CSVs before replacing a single one.
    originals = archive / "original_root_csv"
    originals.mkdir()
    for source in FACEBOOK.glob("*.csv"):
        shutil.copy2(source, originals / source.name)
    shutil.copytree(ENRICHED, archive / "original_enriched_csv")
    manifest = FACEBOOK / "_evaluation_sheets" / "image_manifest.csv"
    if manifest.is_file():
        shutil.copy2(manifest, archive / "original_image_manifest.csv")

    staged = archive / "staged_csv"
    for _raw, _enriched, target, columns, rows in pairs:
        replace_image_prefix(rows, new_prefix)
        write_csv(staged / target.name, columns, rows)

    for source_name, archive_name in (
        ("gempesaw_images", "gempesaw_images"),
        ("with_images", "source_with_images"),
        ("_evaluation_sheets", "evaluation_sheets"),
    ):
        source = FACEBOOK / source_name
        if source.exists():
            destination = archive / archive_name
            validate_move(source, destination)
            shutil.move(str(source), str(destination))

    for staged_csv in staged.glob("*.csv"):
        os.replace(staged_csv, FACEBOOK / staged_csv.name)
    staged.rmdir()

    extras = archive / "other_merged_csv"
    extras.mkdir()
    for name in EXTRA_NAMES:
        source = FACEBOOK / name
        if source.exists():
            destination = extras / name
            validate_move(source, destination)
            shutil.move(str(source), str(destination))

    # Keep the archived evaluation data usable after its images move.
    for archived_csv in (archive / "source_with_images").glob("*.csv"):
        rebase_archived_csv(archived_csv, new_prefix)
    rebase_manifest(archive / "evaluation_sheets" / "image_manifest.csv", archive_images)

    for root_csv in FACEBOOK.glob("*.csv"):
        if not DATED_NAME.match(root_csv.name):
            raise ValueError(f"Unexpected CSV remains in raw/facebook: {root_csv.name}")
        _columns, rows = read_csv(root_csv)
        for value in image_paths(rows):
            if not resolve_image_path(value).is_file():
                raise ValueError(f"Broken image reference in {root_csv.name}: {value}")

    return archive


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Perform the validated consolidation.")
    args = parser.parse_args()
    pairs = plan()
    print(f"Matched enriched CSVs: {len(pairs)}")
    print(f"Root CSVs to update: {sum(raw is not None for raw, *_rest in pairs)}")
    print(f"Dated CSVs to restore from enriched copies: {sum(raw is None for raw, *_rest in pairs)}")
    print(f"Image references verified: {sum(len(image_paths(rows)) for *_rest, rows in pairs)}")
    if not args.apply:
        print("Dry run only. Pass --apply to consolidate and archive the originals.")
        return 0
    archive = consolidate(pairs)
    print(f"Consolidation complete. Originals and historical extras: {archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
