"""Back up and rebuild only final combined NER CSVs using the compact schema.

Preview: python src/scripts/format_final_ner_csvs.py
Apply:   python src/scripts/format_final_ner_csvs.py --apply
Datetime/location inference is NOT rerun. Existing verified intermediates are used.
Maintenance helper for the current checkpoint format; older archived checkpoint
versions must not be copied over the active NER output tree.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT / "src/xlmr-ner"))
import ner_utils as u
import run_ner_pipeline as runner
from preprocessors.format_ner_posts import FINAL_COLUMNS


def migrate(*, input_dir=u.INPUT, output_dir=u.OUTPUT, archive_dir=PROJECT / "data/archive", apply=False):
    input_dir, output_dir, archive_dir = (Path(p).resolve() for p in (input_dir, output_dir, archive_dir))
    u.validate_paths(input_dir, output_dir)
    final_dir = output_dir / u.STAGES["combined"]
    if archive_dir == output_dir or archive_dir.is_relative_to(output_dir) or output_dir.is_relative_to(archive_dir):
        raise ValueError("Keep the backup archive separate from the NER output directory.")
    sources = u.discover(input_dir)
    expected = {u.target(source, "combined", output_dir) for source in sources}
    if set(final_dir.glob("fb_ner_datetime_locations_floodData_*.csv")) != expected:
        raise ValueError("Final CSV filenames must match the selected NER inputs before migration.")
    snapshots = {}
    changed = []
    for source in sources:
        _, original, checksum = u.inputs(source)
        counts = {}
        for stage in ("datetime", "locations", "combined"):
            path = u.target(source, stage, output_dir)
            columns, rows, _ = runner.verified(source, stage, output_dir, checksum)
            counts[stage] = len(rows)
            snapshots[path] = u.digest(path.read_bytes())
            if stage == "combined" and columns != FINAL_COLUMNS:
                changed.append(path)
        # Datetime keeps every post; locations drops posts without a place, and combined follows it.
        if counts["datetime"] != len(original) or not counts["combined"] == counts["locations"] <= len(original):
            raise ValueError(f"Row count mismatch: {source.name}")
    print(f"Final CSVs checked: {len(sources)} | requiring schema update: {len(changed)}")
    if not apply or not changed:
        return {"files": len(sources), "changed": len(changed), "backup": None}
    stamp = datetime.now(u.PH).strftime("%Y%m%d_%H%M%S_%f")
    backup = archive_dir / f"ner_datetime_locations_before_schema_{stamp}"
    if backup.exists():
        raise ValueError(f"Backup already exists: {backup}")
    # Snapshot originals, including the old audit history, before any overwrite.
    shutil.copytree(final_dir, backup)
    for path, checksum in snapshots.items():
        if u.digest(path.read_bytes()) != checksum:
            raise RuntimeError(f"NER file changed during backup: {path.name}")
        if path.parent == final_dir and u.digest((backup / path.name).read_bytes()) != checksum:
            raise RuntimeError(f"Backup checksum mismatch: {path.name}")
    manifest = final_dir / "_metadata/manifest.csv"
    if manifest.read_bytes() != (backup / "_metadata/manifest.csv").read_bytes():
        raise RuntimeError("Backup audit checksum mismatch")
    results = runner.combine_files(input_dir=input_dir, output_dir=output_dir, overwrite=True)
    print(f"Backup: {backup}")
    return {"files": len(sources), "changed": len(results), "backup": str(backup)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Back up the existing final CSVs, then update them.")
    args = parser.parse_args(argv)
    try:
        migrate(apply=args.apply)
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Formatting stopped: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
