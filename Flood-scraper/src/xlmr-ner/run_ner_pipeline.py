#!/usr/bin/env python3
"""Run datetime, locations, then combined CSVs.

Datetime keeps every post. Locations and combined keep only posts with a
matched Davao location, as the senior pipeline drops posts without one.
"""
from __future__ import annotations

from datetime import datetime
from collections import Counter

import extract_datetime as dt
import extract_location as loc
import ner_utils as u
from preprocessors.format_ner_posts import FINAL_COLUMNS, FORMAT_VERSION
from preprocessors.format_ner_posts import display_datetime


def verified(source, stage, output_dir, checksum):
    path = u.target(source, stage, output_dir)
    if not path.exists():
        raise ValueError(f"Missing {stage} output: {path}")
    record = u.previous(path)
    columns, rows, output_hash = u.read_csv(path)
    if not record or record["input_sha256"] != checksum or record["output_sha256"] != output_hash or record["version"] != u.VERSION:
        raise ValueError(f"Stale or unaudited {stage} output: {path}. Use --overwrite.")
    u.checked_details(path, record)
    return columns, rows, record


def verified_details(source, stage, output_dir, checksum):
    columns, public_rows, record = verified(source, stage, output_dir, checksum)
    path = u.target(source, stage, output_dir)
    detail_columns, detail_rows, _ = u.read_csv(u.checked_details(path, record))
    if columns != detail_columns:
        raise ValueError(f"Unexpected detailed {stage} headers: {source.name}")
    if public_rows != detail_rows:
        raise ValueError(f"Public {stage} rows do not match their detailed evidence: {source.name}")
    return detail_columns, detail_rows, record


def combine_files(*, input_dir=u.INPUT, output_dir=u.OUTPUT, overwrite=False, files=None):
    u.validate_paths(input_dir, output_dir)
    completed = []
    for source in u.discover(input_dir) if files is None else files:
        columns, original, checksum = u.inputs(source)
        date_columns, temporal, date_record = verified_details(source, "datetime", output_dir, checksum)
        loc_columns, spatial, loc_record = verified_details(source, "locations", output_dir, checksum)
        if date_columns != columns + dt.COLUMNS or loc_columns != columns + loc.COLUMNS:
            raise ValueError(f"Unexpected intermediate headers for {source.name}")
        expected = {row["ner_record_id"] for row in original}
        def indexed(rows, *, every_post):
            result = {row["ner_record_id"]: row for row in rows}
            if len(result) != len(rows) or not set(result) <= expected or (every_post and set(result) != expected):
                raise ValueError(f"Missing, extra or duplicate record IDs for {source.name}")
            return result
        # The location stage already dropped posts without a matched location.
        times, places = indexed(temporal, every_post=True), indexed(spatial, every_post=False)
        output = []
        for row in original:
            if row["ner_record_id"] not in places:
                continue
            time_row, place_row = times[row["ner_record_id"]], places[row["ner_record_id"]]
            if any(time_row[k] != row[k] or place_row[k] != row[k] for k in columns):
                raise ValueError(f"An intermediate changed original post/image details: {source.name}")
            output.append({**row, **{k: time_row[k] for k in dt.COLUMNS}, **{k: place_row[k] for k in loc.COLUMNS}})
        recipe = u.digest(u.dumps({"version": u.VERSION, "datetime": date_record["output_sha256"],
                                  "location": loc_record["output_sha256"], "datetime_recipe": date_record["recipe_sha256"],
                                  "location_recipe": loc_record["recipe_sha256"],
                                  "datetime_details": date_record["details_sha256"],
                                  "location_details": loc_record["details_sha256"],
                                  "final_format_version": FORMAT_VERSION, "final_columns": FINAL_COLUMNS}).encode())
        path = u.target(source, "combined", output_dir)
        # A schema-only change to the individual stages changes their audit
        # hashes, but not the combined data. Refresh that recipe from verified
        # identical content rather than forcing another inference run.
        old = u.previous(path)
        same_content = False
        if path.exists() and old and old["recipe_sha256"] != recipe and not overwrite:
            existing_columns, existing_rows, _ = verified(source, "combined", output_dir, checksum)
            evidence_columns, evidence_rows, _ = u.read_csv(u.details_path(path))
            displayed_rows = [{**row, "datetime": display_datetime(row["datetime"])} for row in existing_rows]
            same_content = (existing_columns == FINAL_COLUMNS
                and displayed_rows == [u.format_stage(row, "combined") for row in output]
                and evidence_columns == columns + dt.COLUMNS + loc.COLUMNS
                and [u.displayed_temporal_row(row) for row in evidence_rows] == output)
        if not same_content and u.skip_output(path, checksum, recipe, overwrite):
            print(f"Already processed: {path.name}")
            continue
        for stage, record in [("datetime", date_record), ("locations", loc_record)]:
            if u.digest(u.target(source, stage, output_dir).read_bytes()) != record["output_sha256"]:
                raise RuntimeError(f"Intermediate changed during combination: {source.name}")
            u.checked_details(u.target(source, stage, output_dir), record)
        statuses = dict(Counter(row["datetime_status"] for row in output))
        completed.append(u.commit(source, path, FINAL_COLUMNS, [u.format_stage(row, "combined") for row in output], checksum, recipe,
            datetime.now(u.PH).isoformat(timespec="microseconds"), "datetime_status",
            u.dumps({"datetime": date_record["model_revision"], "locations": loc_record["model_revision"]}),
            status_counts=statuses, detail_columns=columns + dt.COLUMNS + loc.COLUMNS, detail_rows=output))
    return completed


def run_pipeline(*, input_dir=u.INPUT, output_dir=u.OUTPUT, lookup_dir=u.LOOKUP, device="cpu", offline=False,
                 overwrite=False, next_only=False, datetime_revision="main", location_revision="main",
                 datetime_recognizer=None, location_recognizer=None):
    common = dict(input_dir=input_dir, output_dir=output_dir, overwrite=overwrite)
    def process(files):
        dates = dt.process_files(**common, files=files, device=device, offline=offline, revision=datetime_revision, recognizer=datetime_recognizer)
        places = loc.process_files(**common, files=files, lookup_dir=lookup_dir, device=device, offline=offline,
                                   revision=location_revision, recognizer=location_recognizer)
        return dates + places + combine_files(**common, files=files)
    if not next_only:
        return process(None)
    for source in u.discover(input_dir):
        result = process([source])
        if result:
            return result
    return []


def main(args):
    if args.download_only:
        for name, labels, revision in [(u.DATE_MODEL, {"DAT", "TIM"}, args.datetime_revision),
                                        (u.LOC_MODEL, {"LOC"}, args.location_revision)]:
            model = u.Recognizer(name, labels, revision=revision, device=args.device, offline=args.local_files_only).load()
            print(f"Ready: {name} | revision: {model.resolved_revision}", flush=True)
            del model
        return
    result = run_pipeline(input_dir=args.input_dir, output_dir=args.output_dir, lookup_dir=args.lookup_dir,
        device=args.device, offline=args.local_files_only, overwrite=args.overwrite, next_only=args.next,
        datetime_revision=args.datetime_revision, location_revision=args.location_revision)
    print(f"NER stage files written: {len(result)}")


if __name__ == "__main__":
    raise SystemExit(u.cli(main, __doc__))
