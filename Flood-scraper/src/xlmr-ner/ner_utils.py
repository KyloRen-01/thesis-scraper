"""Shared inference, chronological CSV processing, and provenance helpers."""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sys
import unicodedata
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from preprocessors.text_cleaner import _atomic_csv
from preprocessors.format_ner_posts import FINAL_COLUMNS, FORMAT_VERSION, format_post, display_datetime

INPUT = ROOT / "data/deduplicated"
OUTPUT = ROOT / "data/ner"
LOOKUP = ROOT / "data/lookup"
CACHE = ROOT / ".hf_cache"
PH = timezone(timedelta(hours=8))
VERSION = "3"
DATE_MODEL = "programmersilvanus/ner-xlmr"
LOC_MODEL = "Davlan/xlm-roberta-base-ner-hrl"
NAME = re.compile(r"fb_(?:single_image|deduplicate)_floodData_(\d{2}-\d{2}-\d{4}_\d{6})(_\d+)?\.csv\Z")
IDENTITY = ["ner_record_id", "ner_source_file", "ner_source_row", "ner_input_text"]
MISSING = {"", "unknown", "none", "null", "nan", "nat", "n/a", "<na>"}
MANIFEST = ["source_file", "output_file", "input_sha256", "output_sha256", "recipe_sha256",
            "started_at", "completed_at", "rows", "status_counts", "model_revision", "version",
            "details_file", "details_sha256"]
STAGES = {"datetime": "ner_datetime", "locations": "ner_locations", "combined": "ner_datetime_locations"}


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def normalized(text):
    return " ".join(unicodedata.normalize("NFKC", str(text)).casefold().split())


def prepare_text(text):
    def hashtag(match):
        word = match[1].replace("_", " ")
        word = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", word)
        return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", word)
    return " ".join(re.sub(r"#(\w+)", hashtag, unicodedata.normalize("NFKC", text)).split())


def discover(directory=INPUT):
    if not Path(directory).is_dir():
        raise ValueError(f"Input directory not found: {directory}")
    files = sorted((p for p in Path(directory).glob("*.csv") if NAME.fullmatch(p.name)), key=lambda p:
                  (datetime.strptime(NAME.fullmatch(p.name)[1], "%m-%d-%Y_%H%M%S"),
                   int((NAME.fullmatch(p.name)[2] or "_0")[1:])))
    if not files:
        raise ValueError(f"No dated single-image or deduplicated Facebook CSVs found: {directory}")
    stamps = [(NAME.fullmatch(p.name)[1], NAME.fullmatch(p.name)[2] or "") for p in files]
    if len(stamps) != len(set(stamps)):
        raise ValueError("Input has colliding scrape timestamps; select just one processing stage.")
    return files


def target(source, stage, output=OUTPUT):
    match = NAME.fullmatch(source.name)
    if not match:
        raise ValueError(f"Expected a dated single-image or deduplicated Facebook CSV: {source.name}")
    prefix = "fb_ner_" + ("datetime_locations" if stage == "combined" else stage) + "_floodData_"
    return Path(output) / STAGES[stage] / (prefix + match[1] + (match[2] or "") + ".csv")


def read_csv(path):
    raw = path.read_bytes()
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""), strict=True)
    columns = reader.fieldnames
    if not columns or not {"text", "text_content"}.intersection(columns) or len(columns) != len(set(columns)):
        raise ValueError(f"Missing text or repeated column names: {path}")
    rows = list(reader)
    if any(None in row or any(v is None for v in row.values()) for row in rows):
        raise ValueError(f"Malformed CSV record in {path}")
    return columns, rows, digest(raw)


def inputs(source):
    columns, rows, checksum = read_csv(source)
    if "text" not in columns:
        raise ValueError(f"NER source inputs require the original text column: {source}")
    if set(columns) & set(IDENTITY):
        raise ValueError(f"Input already contains reserved NER columns: {source}")
    if source.name.startswith("fb_single_image_") and any(
        row.get("image_count") != "1" or len([p for p in row.get("image_paths", "").split(";") if p.strip()]) != 1
        for row in rows
    ):
        raise ValueError(f"Single-image input contains an invalid attachment count: {source}")
    for number, row in enumerate(rows, 2):
        row.update(dict(zip(IDENTITY, [digest(f"{source.name}\0{checksum}\0{number}".encode())[:24],
                         source.name, str(number), prepare_text(row["text"])])))
    return columns + IDENTITY, rows, checksum


def validate_paths(input_dir, output_dir, lookup_dir=LOOKUP):
    output_dir = Path(output_dir).resolve()
    for protected in [Path(input_dir), ROOT / "data/raw", ROOT / "data/cleaned", ROOT / "src", Path(lookup_dir)]:
        protected = protected.resolve()
        if output_dir == protected or output_dir.is_relative_to(protected) or protected.is_relative_to(output_dir):
            raise ValueError("NER output must be separate from inputs, raw data, lookup tables, and source code.")


def history(path):
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, strict=True)
        if reader.fieldnames != MANIFEST:
            raise ValueError(f"Invalid NER manifest: {path}")
        return list(reader)


def previous(path):
    return next((r for r in reversed(history(path.parent / "_metadata/manifest.csv")) if r["output_file"] == path.name), None)


def details_path(path):
    return path.parent / "_metadata" / (path.stem + "_details.csv")


def dropped_path(path):
    return path.parent / "_metadata" / (path.stem + "_dropped.csv")


def checked_details(path, record):
    details = details_path(path)
    if (not record or record.get("details_file") != details.name or not details.is_file()
            or record.get("details_sha256") != digest(details.read_bytes())):
        raise ValueError(f"Stale, missing, or unaudited NER details: {details}. Use --overwrite.")
    return details


def format_stage(row, stage):
    result = format_post(row)
    if stage == "datetime":
        result["location"] = ""
    elif stage == "locations":
        result["datetime"] = ""
    elif stage != "combined":
        raise ValueError(f"Unknown NER stage: {stage}")
    return result


def stage_recipe(stage, settings, *, compact=False, legacy_datetime=False):
    schema = ({"format_version": FORMAT_VERSION, "public_columns": FINAL_COLUMNS} if compact
              else {"public_schema": "original_columns_and_full_ner_v2" if stage == "datetime" and not legacy_datetime
                    else "original_columns_and_full_ner_v1"})
    return digest(dumps({"stage": stage, "version": VERSION, **schema, **settings}).encode())


def displayed_temporal_row(row):
    result = dict(row)
    if "event_time_primary" in result:
        value = result.get("event_time_primary_iso") or result.get("event_time_primary")
        result["event_time_primary"] = display_datetime(value)
    return result


def reusable_compact_output(path, source_rows, columns, extra, checksum, stage, settings, *, subset=False):
    """Reuse audited extraction evidence for schema/display-only changes.

    With ``subset``, the stage keeps only some posts, so the evidence must match
    those source rows in order rather than all of them.
    """
    record = previous(path)
    legacy_recipes = {stage_recipe(stage, settings, compact=True)}
    if stage == "datetime":
        legacy_recipes.add(stage_recipe(stage, settings, legacy_datetime=True))
    if not (path.is_file() and record and record["input_sha256"] == checksum
            and record["version"] == VERSION and record["recipe_sha256"] in legacy_recipes
            and record["output_sha256"] == digest(path.read_bytes())):
        return None
    public_columns, public, _ = read_csv(path)
    detail_columns, details, _ = read_csv(checked_details(path, record))
    matches_public = ((public_columns == detail_columns and public == details)
                      or (public_columns == FINAL_COLUMNS and public == [format_stage(row, stage) for row in details]))
    kept = {row["ner_record_id"] for row in details}
    expected = [row for row in source_rows if row["ner_record_id"] in kept] if subset else source_rows
    if (not matches_public or detail_columns != columns + extra
            or [{key: row[key] for key in columns} for row in details] != expected):
        raise ValueError(f"Output does not match its source/evidence: {path}")
    return [displayed_temporal_row(row) for row in details], record["model_revision"]


def skip_output(path, checksum, recipe, overwrite):
    record = previous(path)
    if overwrite:
        return False
    if path.exists() or record:
        if (path.exists() and record and record["input_sha256"] == checksum and record["recipe_sha256"] == recipe
                and record["version"] == VERSION and record["output_sha256"] == digest(path.read_bytes())):
            checked_details(path, record)
            return True
        raise ValueError(f"Stale, changed, missing, or unaudited output: {path}. Use --overwrite to regenerate.")
    return False


def commit(source, path, columns, rows, checksum, recipe, started, status, revision, *, status_counts=None,
           detail_columns=None, detail_rows=None):
    if digest(source.read_bytes()) != checksum:
        raise RuntimeError(f"Input changed while processing {source.name}; output was not committed.")
    if detail_columns is None or detail_rows is None or len(detail_rows) != len(rows):
        raise ValueError("Every public NER output requires aligned detailed evidence rows.")
    details = details_path(path)
    _atomic_csv(details, detail_columns, detail_rows)
    _atomic_csv(path, columns, rows)
    manifest = path.parent / "_metadata/manifest.csv"
    records = history(manifest)
    record = dict(zip(MANIFEST, [source.name, path.name, checksum, digest(path.read_bytes()), recipe, started,
        datetime.now(PH).isoformat(timespec="microseconds"), len(rows),
        dumps(dict(Counter(row.get(status, "complete") for row in rows)) if status_counts is None else status_counts),
        revision, VERSION, details.name, digest(details.read_bytes())]))
    records.append(record)
    _atomic_csv(manifest, MANIFEST, records)
    print(f"Saved {path.name} | posts: {len(rows)}", flush=True)
    return record


class Recognizer:
    """Fast-tokenizer overlapping windows cover captions beyond 512 tokens."""
    def __init__(self, model, labels, *, revision="main", device="cpu", offline=False):
        self.name, self.labels, self.revision = model, set(labels), revision
        self.device, self.offline = device, offline
        self.pipe = None
        self.resolved_revision = "not_loaded"

    def load(self):
        if self.pipe is not None:
            return self
        try:
            import torch
            from huggingface_hub import snapshot_download
            from transformers import AutoTokenizer, AutoModelForTokenClassification, pipeline
        except ImportError as error:
            raise RuntimeError("Install project dependencies with: python -m pip install -r src/requirements.txt") from error
        torch.set_num_threads(min(4, torch.get_num_threads()))
        print(f"Loading {self.name} on {self.device}...", flush=True)
        # Loading by repo ID can trigger a tokenizer metadata request even with
        # local_files_only in Transformers 4.57.6. Resolve the snapshot first,
        # then give both loaders a filesystem path, never a remote identifier.
        folder = snapshot_download(self.name, revision=self.revision, local_files_only=self.offline,
            cache_dir=str(CACHE), allow_patterns=["config.json", "tokenizer_config.json", "tokenizer.json",
            "special_tokens_map.json", "sentencepiece.bpe.model", "model.safetensors",
            "model.safetensors.index.json", "model-*.safetensors"])
        options = dict(local_files_only=True, trust_remote_code=False)
        # These are XLM-R/SentencePiece models, not Mistral. 4.57.6 otherwise
        # emits a false Mistral-regex warning for old configs without a version.
        tokenizer = AutoTokenizer.from_pretrained(folder, use_fast=True, fix_mistral_regex=False, **options)
        model = AutoModelForTokenClassification.from_pretrained(folder, use_safetensors=True, **options)
        if model.config.model_type != "xlm-roberta":
            raise ValueError(f"Expected an XLM-RoBERTa model, got {model.config.model_type}")
        labels = {str(v).removeprefix("B-").removeprefix("I-") for v in model.config.id2label.values()}
        if not labels & self.labels:
            raise ValueError(f"Incompatible labels for {self.name}: {labels}")
        if not tokenizer.is_fast:
            raise ValueError("A fast tokenizer is required to process full captions.")
        tokenizer.model_max_length = min(int(tokenizer.model_max_length), int(model.config.max_position_embeddings) - 2, 512)
        self.pipe = pipeline("token-classification", model=model, tokenizer=tokenizer, aggregation_strategy="simple",
                             stride=min(64, tokenizer.model_max_length // 4), device=self.device)
        self.resolved_revision = Path(folder).name
        return self

    def __call__(self, text):
        if not text.strip():
            return []
        self.load()
        return [{"text": text[int(e["start"]):int(e["end"])], "label": str(e["entity_group"]),
                 "start": int(e["start"]), "end": int(e["end"]), "score": round(float(e["score"]), 6)}
                for e in self.pipe(text) if e["entity_group"] in self.labels]


def process(stage, extra, function, model, settings, *, input_dir=INPUT, output_dir=OUTPUT, overwrite=False, files=None,
            keep=None):
    """Run one stage over every source. ``keep`` optionally selects the posts the
    stage outputs; the others are saved with their evidence in _metadata/*_dropped.csv."""
    validate_paths(input_dir, output_dir, settings.get("lookup_dir", LOOKUP))
    recipe = stage_recipe(stage, settings)
    jobs = []
    for source in discover(input_dir) if files is None else files:
        columns, rows, checksum = inputs(source)
        if set(columns) & set(extra):
            raise ValueError(f"Input contains reserved {stage} columns: {source}")
        path = target(source, stage, output_dir)
        reuse = (reusable_compact_output(path, rows, columns, extra, checksum, stage, settings, subset=keep is not None)
                 if not overwrite else None)
        skip = False if reuse is not None else skip_output(path, checksum, recipe, overwrite)
        jobs.append((source, columns, rows, checksum, path, skip, reuse))
    completed = []
    for source, columns, rows, checksum, path, skip, reuse in jobs:
        if skip:
            print(f"Already processed: {path.name}")
            continue
        started = datetime.now(PH).isoformat(timespec="microseconds")
        output = []
        if reuse is not None:
            output = reuse[0]
            print(f"Updating {stage} presentation from verified evidence: {source.name}", flush=True)
        else:
            for number, row in enumerate(rows, 1):
                additions = function(row)
                if set(additions) != set(extra):
                    raise RuntimeError(f"Unexpected output fields for {stage}")
                output.append({**row, **additions})
                if number == 1 or number % 25 == 0 or number == len(rows):
                    print(f"{stage}: {source.name} | posts: {number}/{len(rows)}", flush=True)
            if keep is not None:
                decisions = [keep(row) for row in output]
                _atomic_csv(dropped_path(path), columns + extra,
                            [row for row, kept in zip(output, decisions) if not kept])
                output = [row for row, kept in zip(output, decisions) if kept]
        status = "datetime_status" if stage == "datetime" else "location_status"
        counts = dict(Counter(row[status] for row in output))
        if keep is not None:
            counts["dropped"] = len(rows) - len(output)
        completed.append(commit(source, path, columns + extra, output,
                                checksum, recipe, started, status,
                                reuse[1] if reuse is not None else getattr(model, "resolved_revision", "injected_test_recognizer"),
                                status_counts=counts, detail_columns=columns + extra, detail_rows=output))
    return completed


def parser(description):
    p = argparse.ArgumentParser(description=description)
    p.add_argument("--input-dir", type=Path, default=INPUT)
    p.add_argument("--output-dir", type=Path, default=OUTPUT)
    p.add_argument("--lookup-dir", type=Path, default=LOOKUP)
    p.add_argument("--device", default="cpu")
    p.add_argument("--local-files-only", action="store_true")
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--next", action="store_true", help="Process the oldest pending source only.")
    p.add_argument("--datetime-revision", default="main")
    p.add_argument("--location-revision", default="main")
    p.add_argument("--download-only", action="store_true", help="Download/load both models without processing CSVs.")
    return p


def cli(main, description):
    try:
        main(parser(description).parse_args())
        return 0
    except KeyboardInterrupt:
        print("NER interrupted; completed stage files are retained.", file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError, csv.Error, UnicodeError) as error:
        print(f"NER stopped: {error}", file=sys.stderr)
        return 1
