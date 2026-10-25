#!/usr/bin/env python3
"""Extract XLM-RoBERTa date/time evidence with the senior project's priority order.

Like disaster-scraper/src/ner/extract_datetime.py, every post gets a datetime:
the first caption datetime in text order, else post_timestamp, else
scraped_timestamp, else the current time. event_time_source records which one
was used, and datetime_review_required marks every fallback or inferred value.
Run directly for datetime-only CSVs, or run run_ner_pipeline.py for all stages.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

import ner_utils as u
from preprocessors.format_ner_posts import display_datetime

COLUMNS = ["datetime_entities", "time_mentions_raw", "datetime_rule_mentions", "time_parsed",
           "event_time_primary_iso", "event_time_primary", "event_time_source", "event_time_precision",
           "event_time_role", "publication_time_iso", "datetime_status", "datetime_review_required"]
MONTHS = {word: number for number, words in enumerate([
    "january jan enero", "february feb pebrero", "march mar marso", "april apr abril", "may mayo", "june jun hunyo",
    "july jul hulyo", "august aug agosto", "september sep sept setyembre septyembre", "october oct oktubre",
    "november nov nobyembre", "december dec disyembre"], 1) for word in words.split()}
MONTH = "(?:" + "|".join(sorted(MONTHS, key=len, reverse=True)) + ")"
DATES = [
    re.compile(r"\b(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})(?!\d)"),
    re.compile(r"\b(?P<numeric>\d{1,2}[-/]\d{1,2}[-/]\d{4})\b"),
    re.compile(rf"\b(?P<day>\d{{1,2}})\s*[- /]?\s*(?P<month>{MONTH})\.?\b(?:[, /-]*\s*(?P<year>\d{{4}})\b)?", re.I),
    re.compile(rf"\b(?P<month>{MONTH})\.?\s+(?P<day>\d{{1,2}})\b(?:[, /-]*\s*(?P<year>\d{{4}})\b)?", re.I),
]
RELATIVE = re.compile(r"\b(today|yesterday|kahapon|ngayong araw|karong adlawa|kagahapon|gahapon)\b", re.I)
CLOCK = re.compile(r"(?:(?<=T)|(?<![\w:]))(?P<clock>\d{1,2}:\d{2}(?::\d{2})?\s*(?:[ap]\s*\.?\s*m\.?)?|\d{1,2}\s*[ap]\s*\.?\s*m\.?)"
                   r"(?=\s|$|[.,;!?)+-]|Z\b)(?:\s*(?P<zone>Z|[+-]\d{2}:\d{2}|UTC|GMT|PHT|PHST)\b)?", re.I)
HOURS = {"una": 1, "uno": 1, "dos": 2, "tres": 3, "kwatro": 4, "kuwatro": 4, "singko": 5,
         "sais": 6, "siyete": 7, "syete": 7, "otso": 8, "nwebe": 9, "nuwebe": 9,
         "diyes": 10, "dyes": 10, "onse": 11, "dose": 12}
LOCAL_CLOCK = re.compile(r"\b(?:alas[- ]*)?(?P<hour>\d{1,2}|" + "|".join(HOURS) +
    r")(?::(?P<minute>\d{2}))?\s+(?:ng|sa)\s+(?P<period>umaga|buntag|hapon|gabi|gabii|tanghali|udto|hatinggabi)\b", re.I)


def stored_time(value):
    """Parse a saved post or scrape time; naive values are Philippine time."""
    value = value or ""
    if u.normalized(value) in u.MISSING or not re.search(r"[T ]\d{1,2}:\d{2}", value):
        return None
    try:
        stamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        return stamp.replace(tzinfo=u.PH) if stamp.tzinfo is None else stamp.astimezone(u.PH)
    except ValueError:
        return None


def publication(row):
    return stored_time(row.get("post_timestamp"))


def reference_time(row):
    """The senior's fallback order after the caption: post time, scrape time, then now."""
    for source in ("post_timestamp", "scraped_timestamp"):
        value = stored_time(row.get(source))
        if value is not None:
            return value, source
    return datetime.now(u.PH).replace(microsecond=0), "fallback_now"


def dates(text, anchor):
    results, occupied = [], []
    for pattern in DATES:
        for match in pattern.finditer(text):
            if any(match.start() < end and start < match.end() for start, end in occupied):
                continue
            occupied.append(match.span())
            item = dict(text=match[0], start=match.start(), end=match.end(), date="", inferred=False)
            parts = match.groupdict()
            try:
                if parts.get("numeric"):
                    a, b, year = map(int, re.split(r"[-/]", parts["numeric"]))
                    if a <= 12 and b <= 12 and a != b:
                        raise ValueError("ambiguous_numeric_date")
                    month, day = (b, a) if a > 12 else (a, b)
                else:
                    month = MONTHS.get(parts["month"].casefold()) or int(parts["month"])
                    day = int(parts["day"])
                    if not parts.get("year") and anchor is None:
                        raise ValueError("missing_year_no_publication_anchor")
                    year = int(parts["year"]) if parts.get("year") else anchor.year
                    item["inferred"] = not bool(parts.get("year"))
                item["date"] = date(year, month, day).isoformat()
            except (ValueError, TypeError) as error:
                item["issue"] = str(error)
            results.append(item)
    for match in RELATIVE.finditer(text):
        item = dict(text=match[0], start=match.start(), end=match.end(), date="", inferred=True)
        if anchor is None:
            item["issue"] = "relative_date_no_publication_anchor"
        else:
            yesterday = match[0].casefold() in {"yesterday", "kahapon", "kagahapon", "gahapon"}
            item["date"] = (anchor.date() - timedelta(days=int(yesterday))).isoformat()
        results.append(item)
    return sorted(results, key=lambda x: x["start"])


def clocks(text):
    results = []
    for match in CLOCK.finditer(text):
        value = re.sub(r"\s+|\.", "", match["clock"]).upper()
        parsed = None
        for fmt in ("%I:%M:%S%p", "%I:%M%p", "%I%p", "%H:%M:%S", "%H:%M"):
            try:
                parsed = datetime.strptime(value, fmt).time().isoformat()
                break
            except ValueError:
                pass
        if parsed is None:
            continue
        zone = (match["zone"] or "").upper()
        offset = "+00:00" if zone in {"UTC", "GMT", "Z"} else zone if zone.startswith(("+", "-")) else "+08:00"
        try:
            datetime.fromisoformat("2000-01-01T" + parsed + offset)
        except ValueError:
            continue
        results.append(dict(text=match[0], start=match.start(), end=match.end(), time=parsed,
                            offset=offset, timezone_assumed=not bool(zone)))
    for match in LOCAL_CLOCK.finditer(text):
        hour = int(match["hour"]) if match["hour"].isdigit() else HOURS[match["hour"].casefold()]
        minute = int(match["minute"] or 0)
        if not 1 <= hour <= 12 or minute >= 60:
            continue
        hour = hour % 12 + (12 if match["period"].casefold() in {"hapon", "gabi", "gabii", "tanghali", "udto"} else 0)
        results.append(dict(text=match[0], start=match.start(), end=match.end(), time=f"{hour:02d}:{minute:02d}:00",
                            offset="+08:00", timezone_assumed=True))
    return sorted(results, key=lambda x: x["start"])


def gap(a, b):
    return max(0, a["start"] - b["end"], b["start"] - a["end"])


def extract_row(row, model):
    text = row["ner_input_text"]
    entities = model(text) if text.strip() else []
    entities = [e for e in entities if e["label"] in {"DAT", "TIM", "DATE", "TIME"}]
    published = publication(row)
    # Missing years and relative days come from the same reference time, as the
    # senior's dateutil parsing fills them from the date the pipeline runs.
    anchor, anchor_source = reference_time(row)
    day_mentions, time_mentions = dates(text, anchor), clocks(text)
    candidates = []
    for day in day_mentions:
        if not day["date"]:
            continue
        nearby = [clock for clock in time_mentions if gap(day, clock) <= 60
                  and gap(day, clock) == min(gap(other, clock) for other in day_mentions)]
        for clock in nearby or [None]:
            evidence = [day] + ([clock] if clock else [])
            value = day["date"]
            if clock:
                value = datetime.fromisoformat(value + "T" + clock["time"] + clock["offset"]).astimezone(u.PH).isoformat()
            assisted = any(e["start"] < part["end"] and part["start"] < e["end"] for e in entities for part in evidence)
            before = text[max(0, min(p["start"] for p in evidence) - 40):min(p["start"] for p in evidence)]
            candidate = dict(value=value, precision="datetime" if clock else "date", inferred=day["inferred"],
                source="model_and_rules" if assisted else "explicit_text_rules", evidence=[p["text"] for p in evidence],
                role="bulletin_issuance" if re.search(r"\b(issued|issuance|inilabas)\b", before, re.I) else "unspecified_time_mention",
                timezone_assumed=clock["timezone_assumed"] if clock else False)
            if day["inferred"]:
                candidate["anchor"] = anchor_source
            if not any(c["value"] == value for c in candidates):
                candidates.append(candidate)
    complete_dates = {c["value"][:10] for c in candidates if c["precision"] == "datetime"}
    candidates = [c for c in candidates if c["precision"] != "date" or c["value"] not in complete_dates]
    # Senior priority: the first caption datetime in text order, then the fallbacks.
    selected = candidates[0] if candidates else None
    status = ("no_text" if not text.strip() else "ambiguous" if len(candidates) > 1 else "extracted" if selected
              else "partial" if entities or day_mentions or time_mentions else "not_found")
    if selected is None:
        selected = dict(value=anchor.isoformat(), precision="datetime", inferred=True, source=anchor_source, role="fallback")
    review = len(candidates) != 1 or selected["inferred"] or any(d.get("issue") for d in day_mentions)
    return dict(zip(COLUMNS, [u.dumps(entities), u.dumps([e["text"] for e in entities]),
        u.dumps({"dates": day_mentions, "clocks": time_mentions}), u.dumps(candidates),
        selected["value"], display_datetime(selected["value"]), selected["source"], selected["precision"],
        selected["role"], published.isoformat() if published else "", status, str(bool(review)).lower()]))


def process_files(*, input_dir=u.INPUT, output_dir=u.OUTPUT, revision="main", device="cpu", offline=False,
                  overwrite=False, files=None, recognizer=None):
    model = recognizer if recognizer is not None else u.Recognizer(u.DATE_MODEL, {"DAT", "TIM", "DATE", "TIME"},
                                                                  revision=revision, device=device, offline=offline)
    settings = dict(model=u.DATE_MODEL, revision=revision, recognizer="test" if recognizer is not None else "xlmr")
    return u.process("datetime", COLUMNS, lambda row: extract_row(row, model), model, settings,
                     input_dir=input_dir, output_dir=output_dir, overwrite=overwrite, files=files)


def main(args):
    if args.download_only:
        u.Recognizer(u.DATE_MODEL, {"DAT", "TIM"}, revision=args.datetime_revision,
                     device=args.device, offline=args.local_files_only).load()
        return
    files = u.discover(args.input_dir)
    for source in files if args.next else [None]:
        result = process_files(input_dir=args.input_dir, output_dir=args.output_dir, revision=args.datetime_revision,
            device=args.device, offline=args.local_files_only, overwrite=args.overwrite, files=[source] if source else None)
        if result:
            break


if __name__ == "__main__":
    raise SystemExit(u.cli(main, __doc__))
