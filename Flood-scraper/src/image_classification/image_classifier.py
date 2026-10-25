#!/usr/bin/env python3
"""Label every image of each post with Claude and the flood image rubric, then keep flood posts.

Run: python src/image_classification/image_classifier.py [--max-files 5]
Reads data/ner/ner_datetime_locations, oldest scrape first, and writes data/image_classified.
Each post's images go to Claude in one request, without the caption, with the rubric as a cached
system prompt. Claude reports what each image shows; the rubric's fixed rules turn that into
visual_severity and infrastructure_accessibility. Every record of a post also gets
post_visual_severity, the highest visual_severity among the post's images (POST_SEVERITY_ORDER).
Every record is checked against flood_image_schema.json, and follows its field order, before it is
written. Settings live in image_classifier_config.py.
Input CSVs and images are never modified.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import re
import sqlite3
import sys
import tempfile
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

try:
    from . import image_classifier_config as cfg
    from ..preprocessors.text_cleaner import PH_TIME, _atomic_csv
except ImportError:  # Direct script invocation.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import image_classifier_config as cfg
    from preprocessors.text_cleaner import PH_TIME, _atomic_csv


NAME = re.compile(r"fb_ner_datetime_locations_floodData_(\d{2}-\d{2}-\d{4}_\d{6})(_\d+)?\.csv\Z")
INPUT_COLUMNS = ["source", "page_source", "text_content", "datetime", "location", "image_paths", "url"]
MANIFEST_COLUMNS = ["source_file", "output_file", "started_at", "completed_at", "input_posts", "kept_posts",
                    "excluded_posts", "exclusion_counts", "image_references", "unclassified_images", "requests",
                    "cost_usd", "model", "model_versions", "prompt_version", "version", "input_sha256",
                    "images_sha256", "output_sha256", "excluded_sha256", "evidence_sha256"]


class QuotaExhausted(RuntimeError):
    """Credits, the rate limit, or the spending limit ran out; cached answers are kept, so a later run continues."""


class ModelUnavailable(RuntimeError):
    """Claude can't be used: no or rejected key, unknown model, or an outage."""


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def now() -> str:
    return datetime.now(PH_TIME).isoformat(timespec="microseconds")


def deepest_anchor(anchors: list[dict]) -> str | None:
    ranked = [(cfg.LEVEL_RANK[cfg.ANCHOR_LEVELS[a["anchor"]]], a["anchor"]) for a in anchors]
    return max(ranked)[1] if ranked else None


def visual_severity(answer: dict) -> tuple[str, str]:
    """Rubric section 4: the first rule that fits wins."""
    if not answer["is_valid_flood_evidence"]:
        return "not_assessable", "rule 1: not valid evidence"
    if not answer["flood_presence"]:
        return "no_flood", "rule 2: no floodwater"
    if answer["impact_cues"]:
        return "major", "rule 3: major impact cue (" + ", ".join(answer["impact_cues"]) + ")"
    anchor = deepest_anchor(answer["anchors"])
    if anchor:
        return cfg.ANCHOR_LEVELS[anchor], f"rule 4: deepest anchor {anchor}"
    return "not_assessable", "rule 5: floodwater but no depth anchor"


def infrastructure_accessibility(answer: dict) -> tuple[str, str]:
    """Rubric section 5: the first rule that fits wins."""
    if not answer["is_valid_flood_evidence"] or not answer["road_visible"]:
        return "no_road_visible", "rule 1: not valid evidence or no road visible"
    if answer["road_blocked"]:
        return "not_passable_all_vehicles", "rule 2: road blocked"
    if not answer["flood_presence"]:
        return "passable_all_vehicles", "rule 3: no floodwater"
    anchor = deepest_anchor([a for a in answer["anchors"] if a["on_road"]])
    if anchor:
        return cfg.ACCESSIBILITY_BY_LEVEL[cfg.ANCHOR_LEVELS[anchor]], f"rule 4: deepest on-road anchor {anchor}"
    stopping = [cue for cue in answer["impact_cues"] if cue in cfg.ROAD_STOPPING_CUES]
    if stopping:
        return "not_passable_all_vehicles", "rule 5: " + ", ".join(stopping)
    return "not_assessable", "rule 6: flooded road with nothing to judge depth"


def counts_for_post(record: dict) -> bool:
    """Whether an image's visual_severity counts toward its post's level (POST_SEVERITY_COUNTS_IF_TRUE)."""
    return record[cfg.POST_SEVERITY_COUNTS_IF_TRUE] is True and record["visual_severity"] in cfg.POST_RANK


def post_visual_severity(records: list[dict]) -> tuple[str, str]:
    """A post's overall severity: the highest visual_severity among the post's counted images, ranked
    by POST_SEVERITY_ORDER in image_classifier_config.py (no_flood < not_assessable < minor <
    moderate < major, counting only valid flood evidence). Each image keeps its own visual_severity.
    A post without a counted image gets POST_SEVERITY_IF_NO_IMAGE_COUNTS."""
    counted = [(number, record["visual_severity"]) for number, record in enumerate(records, 1)
               if counts_for_post(record)]
    if not counted:
        return cfg.POST_SEVERITY_IF_NO_IMAGE_COUNTS, "no image counts toward the post level"
    top = max((level for _, level in counted), key=cfg.POST_RANK.get)
    numbers = [str(number) for number, level in counted if level == top]
    return top, f"highest image severity: {top} (image{'s' if len(numbers) > 1 else ''} {', '.join(numbers)})"


def check_record(record: dict) -> None:
    """Refuse any record that breaks flood_image_schema.json or the rubric's consistency rules."""
    def allowed(value, choices):
        return any(value is choice or (type(value) is type(choice) and value == choice) for choice in choices)
    if list(record) != list(cfg.ALLOWED_VALUES):
        raise RuntimeError(f"Record fields differ from flood_image_schema.json: {list(record)}")
    for key, rule in cfg.ALLOWED_VALUES.items():
        if not (allowed(record[key], rule) if isinstance(rule, list) else isinstance(record[key], str)):
            raise RuntimeError(f"{key}={record[key]!r} is not allowed by flood_image_schema.json")
    valid, presence, post = record["is_valid_flood_evidence"], record["flood_presence"], record["post_visual_severity"]
    consistent = (
        (valid or (presence is None and record["visual_severity"] == "not_assessable"
                   and record["infrastructure_accessibility"] == "no_road_visible"))
        and (not valid or presence is not None)
        and (presence is not False or (record["visual_severity"] == "no_flood" and record["infrastructure_accessibility"]
                                       in {"passable_all_vehicles", "not_passable_all_vehicles", "no_road_visible"}))
        and (presence is not True or record["visual_severity"] != "no_flood")
        # A counted image never ranks above its post (POST_SEVERITY_ORDER).
        and (not counts_for_post(record) or cfg.POST_RANK.get(post, -1) >= cfg.POST_RANK[record["visual_severity"]])
        and len(record["visual_description"].split()) <= cfg.DESCRIPTION_MAX_WORDS)
    if not consistent:
        raise RuntimeError(f"Record breaks the rubric's consistency rules: {record}")


def final_record(path: str, answer: dict) -> tuple[dict, dict, bool]:
    """The per-image rubric parameters for one classified image, the rule behind each label, and
    whether the description had to be cut to the word limit. post_records() adds
    post_visual_severity, puts the fields in the schema's order, and checks the record once the
    whole post is known."""
    severity, severity_basis = visual_severity(answer)
    access, access_basis = infrastructure_accessibility(answer)
    words = answer["visual_description"].split()
    record = {"image_path": path, "is_valid_flood_evidence": answer["is_valid_flood_evidence"],
              "flood_presence": answer["flood_presence"] if answer["is_valid_flood_evidence"] else None,
              "visual_severity": severity, "infrastructure_accessibility": access,
              "visual_description": " ".join(words[:cfg.DESCRIPTION_MAX_WORDS])}
    return record, {"visual_severity": severity_basis, "infrastructure_accessibility": access_basis}, \
        len(words) > cfg.DESCRIPTION_MAX_WORDS


def unclassified_record(path: str) -> dict:
    """An image that could not be judged (missing file, refusal, or no valid answer)."""
    record = {"image_path": path, "is_valid_flood_evidence": False, "flood_presence": None,
              "visual_severity": "not_assessable", "infrastructure_accessibility": "no_road_visible",
              "visual_description": ""}
    return record


def post_records(records: list[dict]) -> tuple[list[dict], str]:
    """The image records of one post, each given the post's post_visual_severity, with its fields in
    flood_image_schema.json order and checked, plus the rule behind the post's level. The images'
    own visual_severity values are not changed."""
    severity, basis = post_visual_severity(records)
    ordered = []
    for record in records:
        values = {**record, "post_visual_severity": severity}
        # Schema fields in schema order; a field the schema lacks stays at the end for check_record to refuse.
        record = {key: values.pop(key) for key in cfg.ALLOWED_VALUES if key in values} | values
        check_record(record)
        ordered.append(record)
    return ordered, basis


def read_answer(text: str, count: int) -> list[dict] | None:
    """One answer per image, in image order, or None unless every image is answered once."""
    try:
        items = json.loads(text or "").get("images")
    except (ValueError, AttributeError):
        return None
    required = set(cfg.IMAGE_ANSWER["required"])
    if not isinstance(items, list) or len(items) != count or not all(
            isinstance(item, dict) and required <= set(item) for item in items):
        return None
    by_number = {item["image_number"]: item for item in items}
    if set(by_number) != set(range(1, count + 1)):
        return None
    answers = [{key: value for key, value in by_number[n].items() if key != "image_number"} for n in range(1, count + 1)]
    # The rubric allows a null flood_presence only for images that are not valid evidence.
    if any(a["is_valid_flood_evidence"] and a["flood_presence"] is None for a in answers):
        return None
    return answers


class Claude:
    """Asks Claude what one post's images show, following the rubric; it never sets the levels."""

    def __init__(self, model: str = cfg.MODEL, max_usd: float = cfg.MAX_USD, client=None):
        if max_usd and model not in cfg.PRICES:
            raise ValueError(f"No price is known for {model!r}; add it to PRICES in image_classifier_config.py "
                             "or pass --max-usd 0.")
        self.model, self.max_usd, self._client = model, max_usd, client
        self.requests, self.tokens, self.lock, self.last_answer = 0, Counter(), threading.Lock(), 0.0

    def spent_usd(self) -> float:
        """This run's cost at Anthropic's list prices, from the token counts it reports."""
        price = cfg.PRICES.get(self.model, {})
        return sum(self.tokens[kind] * price[kind] for kind in price) / 1_000_000

    def client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(api_key=cfg.anthropic_api_key(), max_retries=cfg.RETRIES,
                                               timeout=cfg.TIMEOUT_SECONDS)
        return self._client

    def observe(self, images: list[tuple[bytes, str]]) -> tuple[list[dict], str]:
        """Return one answer per (bytes, MIME type) image and the model that answered.

        A refused or invalid answer gives every image {'unclassified_reason': ...}; those aren't
        cached, so --overwrite asks again. Credit, key, and API failures raise instead.
        """
        import anthropic
        content = []
        for number, (data, mime_type) in enumerate(images, 1):
            content += [{"type": "text", "text": f"Image {number}:"},
                        {"type": "image", "source": {"type": "base64", "media_type": mime_type,
                                                     "data": base64.standard_b64encode(data).decode()}}]
        content.append({"type": "text", "text": cfg.USER_PROMPT.format(count=len(images))})
        # A request the safety classifiers decline is re-run on Anthropic's recommended fallback
        # model where one exists; model_version records whichever model answered.
        fallback = (dict(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
                    if self.model in cfg.FALLBACK_MODELS else {})
        for _ in range(2):  # One retry for an answer that doesn't fit the format.
            if self.max_usd and self.spent_usd() >= self.max_usd:
                raise QuotaExhausted(f"Spending limit reached: about ${self.spent_usd():.2f} this run "
                                     f"(--max-usd {self.max_usd:g}). Cached answers are kept; run again to continue.")
            try:
                response = self.client().beta.messages.create(
                    model=self.model, max_tokens=cfg.MAX_TOKENS,
                    system=[{"type": "text", "text": cfg.SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
                    messages=[{"role": "user", "content": content}],
                    output_config={"effort": cfg.EFFORT, "format": {"type": "json_schema", "schema": cfg.ANSWER_SCHEMA}},
                    **fallback)
            except anthropic.BadRequestError as error:
                message = str(error.message)
                if "credit balance" in message.lower():
                    raise QuotaExhausted("Anthropic credits are used up. Cached answers are kept; add funds in the "
                                         "Claude Console and run again to continue.") from error
                if "image" in message.lower():
                    return [{"unclassified_reason": "model_rejected_image"}] * len(images), ""
                raise ModelUnavailable(f"Claude request failed: {message}") from error
            except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as error:
                raise ModelUnavailable(f"Anthropic rejected ANTHROPIC_API_KEY ({error.status_code}).") from error
            except anthropic.NotFoundError as error:
                raise ModelUnavailable(f"Claude model {self.model!r} was not found; choose another with --model.") from error
            except anthropic.RateLimitError as error:
                raise QuotaExhausted("Anthropic's rate limit is still exceeded after retrying. "
                                     "Cached answers are kept; run again later to continue.") from error
            except (anthropic.APIStatusError, anthropic.APIConnectionError) as error:
                raise ModelUnavailable(f"Claude is unavailable: {error}") from error
            usage = response.usage
            with self.lock:  # Only answered requests cost money.
                self.requests += 1
                self.last_answer = time.monotonic()
                self.tokens.update(input=usage.input_tokens or 0, cache_write=usage.cache_creation_input_tokens or 0,
                                   cache_read=usage.cache_read_input_tokens or 0, output=usage.output_tokens or 0)
            if response.stop_reason == "refusal":
                category = getattr(response.stop_details, "category", None) or "unknown"
                return [{"unclassified_reason": f"model_refused_{category}"}] * len(images), ""
            answers = read_answer("".join(b.text for b in response.content if b.type == "text"), len(images))
            if answers is not None:
                return answers, response.model
        return [{"unclassified_reason": "model_gave_no_valid_answer"}] * len(images), ""


class ObservationCache:
    """Claude's answers keyed by image SHA-256, model, and prompt version, so reruns and
    images shared between posts are never sent twice."""

    def __init__(self, path: Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS observations (image_sha256 TEXT NOT NULL, model TEXT NOT NULL, "
                        "prompt_version TEXT NOT NULL, observation TEXT NOT NULL, model_version TEXT NOT NULL, "
                        "classified_at TEXT NOT NULL, PRIMARY KEY (image_sha256, model, prompt_version))")

    def get(self, sha256: str, model: str) -> tuple[dict, str] | None:
        row = self.db.execute("SELECT observation, model_version FROM observations WHERE image_sha256 = ? AND model = ? "
                              "AND prompt_version = ?", (sha256, model, cfg.PROMPT_VERSION)).fetchone()
        return (json.loads(row[0]), row[1]) if row else None

    def put(self, sha256: str, model: str, answer: dict, model_version: str) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO observations VALUES (?, ?, ?, ?, ?, ?)", (sha256, model,
                            cfg.PROMPT_VERSION, json.dumps(answer, sort_keys=True), model_version, now()))

    def close(self) -> None:
        self.db.close()


def discover(directory: Path) -> list[Path]:
    if not directory.is_dir():
        raise ValueError(f"Combined NER directory not found: {directory}")
    def order(path):
        match = NAME.fullmatch(path.name)
        return datetime.strptime(match[1], "%m-%d-%Y_%H%M%S"), int((match[2] or "_0")[1:])
    files = sorted((p for p in directory.glob("*.csv") if NAME.fullmatch(p.name)), key=order)
    if not files:
        raise ValueError(f"No dated combined NER CSVs found: {directory}")
    return files


def read_source(path: Path):
    raw = path.read_bytes()
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""), strict=True)
    columns = reader.fieldnames or []
    if len(columns) != len(set(columns)) or not set(INPUT_COLUMNS) <= set(columns):
        raise ValueError(f"Expected the combined NER columns {INPUT_COLUMNS}: {path}")
    rows = list(reader)
    if any(None in row or any(value is None for value in row.values()) for row in rows):
        raise ValueError(f"Malformed CSV record: {path}")
    return rows, digest(raw)


def read_manifest(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, strict=True)
        if reader.fieldnames != MANIFEST_COLUMNS:
            raise ValueError(f"Invalid image classification manifest: {path}")
        return list(reader)


def mime_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    return "image/webp" if data[:4] == b"RIFF" and data[8:12] == b"WEBP" else ""


def saved_image(path: str, project: Path) -> dict:
    """SHA-256, MIME type, and file of a saved image, or the problem that keeps it from Claude."""
    image = {"sha256": "", "mime_type": "", "file": None, "problem": ""}
    try:
        file = (project / path.replace("\\", "/")).resolve()
        if not file.is_relative_to(project):
            return {**image, "problem": "unsafe_image_path"}
        data = file.read_bytes()
    except FileNotFoundError:
        return {**image, "problem": "missing_image_file"}
    except (OSError, ValueError):
        return {**image, "problem": "unreadable_image_file"}
    if not mime_type(data):
        return {**image, "problem": "unsupported_image_type"}
    return {**image, "sha256": digest(data), "mime_type": mime_type(data), "file": file}


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def observe_posts(posts: list[list[str]], images: dict, cache: ObservationCache, claude: Claude, label: str) -> dict:
    """Return {image SHA-256: (answer, model version)}, asking Claude once per post about that
    post's images that are neither cached nor already asked about."""
    results, jobs, seen = {}, [], set()
    for paths in posts:
        job = {}
        for path in paths:
            image = images[path]
            if not image["sha256"] or image["sha256"] in seen:
                continue
            seen.add(image["sha256"])
            cached = cache.get(image["sha256"], claude.model)
            if cached:
                results[image["sha256"]] = cached
            else:
                job[image["sha256"]] = image
        if job:
            jobs.append(job)
    if not jobs:
        return results
    print(f"{claude.model}: {label} | posts to send: {len(jobs)} | images: {sum(map(len, jobs))}", flush=True)
    claude.client()  # Fail on a missing key before any request.

    def ask(job):
        payload = []
        for sha256, image in job.items():
            data = image["file"].read_bytes()
            if digest(data) != sha256:
                raise RuntimeError(f"Image changed during classification: {image['file']}")
            payload.append((data, image["mime_type"]))
        return claude.observe(payload)

    def store(job, outcome):
        answers, version = outcome
        for sha256, answer in zip(job, answers):
            if "unclassified_reason" not in answer:
                cache.put(sha256, claude.model, answer, version)
            results[sha256] = (answer, version)

    # Parallel requests can't read a prompt cache that is still being written, so when the cache
    # may be cold the first post goes alone and the rest then read the cached rubric.
    first = []
    if time.monotonic() - claude.last_answer > 240:
        store(jobs[0], ask(jobs[0]))
        first, jobs = jobs[:1], jobs[1:]
    stop, total = None, len(first) + len(jobs)
    with ThreadPoolExecutor(cfg.WORKERS) as pool:
        futures = {pool.submit(ask, job): job for job in jobs}
        try:
            for count, future in enumerate(as_completed(futures), 1):
                if future.cancelled():
                    continue
                try:
                    outcome = future.result()
                except Exception as error:
                    # Send nothing new, but keep answers to requests already under way: each one cost money.
                    stop = stop or error
                    for other in futures:
                        other.cancel()
                    continue
                store(futures[future], outcome)
                if count % 10 == 0 or count == len(jobs):
                    print(f"{claude.model}: {label} | posts answered: {len(first) + count}/{total}", flush=True)
        except BaseException:
            for future in futures:
                future.cancel()
            raise
    if stop is not None:
        raise stop
    return results


def classify_file(source: Path, output_dir: Path, history: list[dict], cache: ObservationCache, claude: Claude,
                  project: Path, overwrite: bool) -> dict | None:
    started = now()
    rows, checksum = read_source(source)
    posts = [[path.strip() for path in row["image_paths"].split(";") if path.strip()] for row in rows]
    images = {path: saved_image(path, project) for paths in posts for path in paths}
    # Changed, missing, or restored image files invalidate a saved result like a changed CSV.
    evidence_key = digest(json.dumps(sorted([path, image["sha256"] or image["problem"]]
                                            for path, image in images.items())).encode())
    name = source.name.replace("fb_ner_datetime_locations_floodData_", "fb_image_classified_floodData_", 1)
    target = output_dir / name
    excluded_path = output_dir / "_metadata" / (target.stem + "_excluded.csv")
    evidence_path = output_dir / "_metadata" / (target.stem + "_evidence.jsonl")
    outputs = [target, excluded_path, evidence_path]
    previous = next((r for r in reversed(history) if r["output_file"] == name), None)
    if (previous or any(path.exists() for path in outputs)) and not overwrite:
        if not (previous and all(path.is_file() for path in outputs)
                and [previous[key] for key in ("input_sha256", "images_sha256", "model", "prompt_version", "version")]
                == [checksum, evidence_key, claude.model, cfg.PROMPT_VERSION, cfg.OUTPUT_VERSION]
                and [previous[key] for key in ("output_sha256", "excluded_sha256", "evidence_sha256")]
                == [digest(path.read_bytes()) for path in outputs]):
            raise ValueError(f"Changed, missing, or unaudited classified output: {target}. Use --overwrite to regenerate.")
        print(f"Already classified: {name}")
        return None
    requests, spent = claude.requests, claude.spent_usd()
    results = observe_posts(posts, images, cache, claude, source.name)
    kept, excluded, evidence, versions, unclassified = [], [], [], set(), 0
    for row_number, (row, paths) in enumerate(zip(rows, posts), 2):
        records, notes, failed = [], [], False
        for number, path in enumerate(paths, 1):
            image = images[path]
            answer, version = results.get(image["sha256"], (None, "")) if image["sha256"] else (None, "")
            status = image["problem"] or ("not_answered" if answer is None
                                          else answer.get("unclassified_reason", "classified"))
            if status == "classified":
                record, basis, truncated = final_record(path, answer)
                versions.add(version)
            else:
                record, basis, truncated, failed = unclassified_record(path), {}, False, True
                unclassified += 1
            records.append(record)
            notes.append({"source_row": row_number, "url": row["url"], "image_number": number, "image_path": path,
                          "image_sha256": image["sha256"], "status": status, "model_version": version,
                          "answer": answer if status == "classified" else None, "decision_basis": basis,
                          "description_truncated": truncated, "record": record})
        records, post_basis = post_records(records)
        for note, record in zip(notes, records):
            note["record"] = record
            note["decision_basis"]["post_visual_severity"] = post_basis
        keep = cfg.keep_post(records)
        output = {column: json.dumps(records, ensure_ascii=False) if column == cfg.IMAGE_COLUMN else row[column]
                  for column in cfg.OUTPUT_COLUMNS}
        if keep:
            kept.append(output)
        else:
            reason = "no_images" if not paths else "images_not_classified" if failed else "no_flood_evidence"
            excluded.append({**output, "exclusion_reason": reason})
        evidence.extend({**note, "post_kept": keep} for note in notes)
    if digest(source.read_bytes()) != checksum:
        raise RuntimeError(f"Input changed while classifying {source.name}; output was not committed.")
    atomic_text(evidence_path, "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in evidence))
    _atomic_csv(excluded_path, cfg.OUTPUT_COLUMNS + ["exclusion_reason"], excluded)
    _atomic_csv(target, cfg.OUTPUT_COLUMNS, kept)
    cost = claude.spent_usd() - spent
    print(f"Classified: {name} | kept: {len(kept)} | excluded: {len(excluded)} | images: {sum(map(len, posts))} | "
          f"requests: {claude.requests - requests} | cost: ${cost:.2f}", flush=True)
    return dict(zip(MANIFEST_COLUMNS, [
        source.name, name, started, now(), len(rows), len(kept), len(excluded),
        json.dumps(dict(Counter(row["exclusion_reason"] for row in excluded)), sort_keys=True),
        sum(map(len, posts)), unclassified, claude.requests - requests, f"{cost:.4f}", claude.model,
        json.dumps(sorted(versions)), cfg.PROMPT_VERSION, cfg.OUTPUT_VERSION, checksum, evidence_key,
        *(digest(path.read_bytes()) for path in outputs)]))


def classify_files(input_dir=cfg.INPUT_DIR, output_dir=cfg.OUTPUT_DIR, *, cache_path=cfg.CACHE_PATH, claude=None,
                   overwrite=False, max_files=None, project_dir=cfg.BASE_DIR):
    project = Path(project_dir).resolve()
    input_dir, output_dir = Path(input_dir).resolve(), Path(output_dir).resolve()
    for protected in [input_dir, project / "src", *(project / "data" / name for name in
                      ["raw", "cleaned", "deduplicated", "ner", "lookup"])]:
        if output_dir == protected or output_dir.is_relative_to(protected) or protected.is_relative_to(output_dir):
            raise ValueError("Classified output must be separate from source code, source data, NER, and lookup directories.")
    sources = discover(input_dir)
    claude = claude or Claude()
    manifest = output_dir / "_metadata/manifest.csv"
    history = read_manifest(manifest)
    completed = []
    cache = ObservationCache(cache_path)
    try:
        for source in sources:
            if max_files is not None and len(completed) >= max_files:
                break
            record = classify_file(source, output_dir, history, cache, claude, project, overwrite)
            if record is None:
                continue
            history.append(record)
            _atomic_csv(manifest, MANIFEST_COLUMNS, history)
            completed.append(record)
    finally:
        cache.close()
    return completed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=cfg.INPUT_DIR)
    parser.add_argument("--output-dir", type=Path, default=cfg.OUTPUT_DIR)
    parser.add_argument("--model", default=cfg.MODEL, help=f"Claude model (default {cfg.MODEL}).")
    parser.add_argument("--max-usd", type=float, default=cfg.MAX_USD,
                        help=f"Stop once this run's estimated cost reaches this many US dollars "
                             f"(default {cfg.MAX_USD:g}; 0 = no limit).")
    parser.add_argument("--max-files", type=int, help="Classify at most this many pending files, oldest first.")
    parser.add_argument("--next", action="store_true", help="Classify only the oldest pending file.")
    parser.add_argument("--overwrite", action="store_true",
                        help="Regenerate outputs from cached answers; images Claude couldn't classify are asked again.")
    args = parser.parse_args(argv)
    csv.field_size_limit(10_000_000)
    claude = None
    try:
        claude = Claude(args.model, args.max_usd)
        results = classify_files(args.input_dir, args.output_dir, claude=claude, overwrite=args.overwrite,
                                 max_files=1 if args.next else args.max_files)
        print(f"Files classified this run: {len(results)}")
        return 0
    except KeyboardInterrupt:
        print("Classification stopped. Finished files and cached answers are kept.", file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError, csv.Error, UnicodeError, sqlite3.Error) as error:
        print(f"Classification stopped: {error}", file=sys.stderr)
        return 1
    finally:
        if claude is not None and claude.requests:
            print(f"Requests answered: {claude.requests} | tokens: {dict(claude.tokens)} | "
                  f"cost at Anthropic's list prices: ${claude.spent_usd():.2f}")


if __name__ == "__main__":
    raise SystemExit(main())
