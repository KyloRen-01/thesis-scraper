#!/usr/bin/env python3
"""XLM-RoBERTa location extraction and normalization using data/lookup CSVs.

The lookup tables cover Davao Region only. As in the senior project's
extract_location.py (drop_empty=True), posts without a matched location are
dropped from the stage output; their evidence is kept in
_metadata/<output>_dropped.csv. Reference files are not verified event places.
A Davao name that also names a place elsewhere (SHARED_NAMES, e.g. Barangay
Pampanga or Barangay Luzon) counts only when the post shows it is about Davao.
"""
from __future__ import annotations

import csv
import re
from collections import defaultdict
from pathlib import Path

import ner_utils as u

COLUMNS = ["location_entities", "location_mentions_raw", "location_raw", "location_candidates", "location",
           "location_source", "location_status", "location_review_required"]
TABLES = [("refregion.csv", "regDesc", "region", "regCode"),
          ("refprovince.csv", "provDesc", "province", "provCode"),
          ("refcitymun.csv", "citymunDesc", "citymun", "citymunCode"),
          ("refbrgy.csv", "brgyDesc", "barangay", "brgyCode")]
# Common names that the tables spell differently. Plain "Davao" or "Dabaw" can
# mean the city, a province, or the region, so it maps to the region, which
# contains all of them.
ALIASES = {"gensan": "general santos", "metro manila": "national capital region", "maynila": "city of manila",
           "davao region": "region xi", "ncr": "national capital region", "davao": "region xi", "dabaw": "region xi",
           "compostela valley": "davao de oro", "samal": "island garden city of samal",
           "igacos": "island garden city of samal"}
# Davao place names that also name a region, province, city or municipality elsewhere in the
# Philippines (the nationwide PSGC tables in ../disaster-scraper/data/lookup), or Luzon. A post
# naming only these is not evidence of a Davao place; see extract_row. The ALIASES above are
# deliberate Davao readings ("Samal" is the island city, not Samal, Bataan) and are left out.
SHARED_NAMES = frozenset(name.strip() for name in """
aguinaldo, alegria, aurora, bacungan, bagumbayan, bato, bayabas, baybay, binondo, binuangan, bobon, bongabong,
bonifacio, buenavista, bulacan, bunawan, camalig, caraga, carmen, cawayan, compostela, concepcion, consolacion,
dagohoy, danao, el salvador, esperanza, hagonoy, iba, imelda, jovellar, kapatagan, katipunan, kauswagan,
la libertad, la paz, la union, lantawan, libertad, luna, luzon, mabini, mabuhay, magsaysay, mahayag, mainit,
malinao, mercedes, naga, pampanga, pasig, prosperidad, quezon, quiapo, rizal, roxas, sabangan, san agustin,
san antonio, san francisco, san guillermo, san isidro, san jose, san juan, san luis, san miguel, san nicolas,
san pedro, san rafael, san remigio, san roque, san vicente, santa cruz, santa fe, santa maria, santiago,
santo niño, santo tomas, sarangani, sibulan, siocon, talisay, teresa, tigbao, tubod
""".split(",") if name.strip())
DAVAO_WORD = re.compile(r"davao|dabaw", re.IGNORECASE)
BARANGAY_BEFORE = re.compile(r"\b(?:brgy|barangay)\.?\s*$", re.IGNORECASE)


def key(text):
    return " ".join(re.findall(r"\w+", u.normalized(text)))


def padded(value, width):
    return value.zfill(width) if value.isdigit() else value


class Gazetteer:
    def __init__(self, lookup_dir=u.LOOKUP):
        self.index = defaultdict(list)
        self.hashes, helpers = {}, set()
        for filename, name_column, level, code_column in TABLES:
            path = Path(lookup_dir) / filename
            if not path.exists():
                if level != "barangay":
                    raise ValueError(f"Required location reference not found: {path}")
                self.hashes[filename] = "missing_optional"
                continue
            self.hashes[filename] = u.digest(path.read_bytes())
            with path.open(encoding="utf-8-sig", newline="") as stream:
                reader = csv.DictReader(stream)
                if not reader.fieldnames or not {name_column, code_column}.issubset(reader.fieldnames):
                    raise ValueError(f"Invalid geographic reference schema: {path}")
                for row in reader:
                    record = dict(matched_name=row[name_column], level=level,
                        code=padded(row.get("psgcCode", row[code_column]), 9),
                        region_code=padded(row.get("regCode", ""), 2),
                        province_code=padded(row.get("provCode", ""), 4),
                        citymun_code=padded(row.get("citymunCode", ""), 6))
                    core = key(re.sub(r"\([^)]*\)", "", row[name_column]))
                    aliases = {key(row[name_column]), core}
                    if core.startswith("city of "):
                        aliases.update({core[8:], core[8:] + " city"})
                    if level == "region":
                        parens = re.search(r"\(([^)]+)\)", row[name_column])
                        if parens:
                            aliases.add(key(parens[1]))
                        romans = {"i": "1", "ii": "2", "iii": "3", "iv a": "4a", "iv b": "4b", "v": "5",
                                  "vi": "6", "vii": "7", "viii": "8", "ix": "9", "x": "10", "xi": "11", "xii": "12", "xiii": "13"}
                        if core.startswith("region ") and core[7:] in romans:
                            aliases.add("region " + romans[core[7:]])
                    for alias in aliases - {""}:
                        if not any(r["code"] == record["code"] and r["level"] == level for r in self.index[alias]):
                            self.index[alias].append(record)
                        if level != "barangay" and len(alias) > 3:
                            helpers.add(alias)
        for alias, official in ALIASES.items():
            if official in self.index:
                self.index[alias] = self.index[official][:]
                if len(alias) > 3:
                    helpers.add(alias)
        self.helper = re.compile(r"(?<!\w)(?:" + "|".join(re.escape(n).replace(r"\ ", r"[\s-]+")
                               for n in sorted(helpers, key=lambda n: (-len(n), n))) + r")(?!\w)", re.I)

    def candidates(self, text):
        name = key(text)
        barangay = re.match(r"^(?:brgy|barangay)\s+(.+)$", name)
        if barangay:
            return [r for r in self.index.get(barangay[1], []) if r["level"] == "barangay"]
        records = self.index.get(name, [])
        # Municipal names can also name many barangays; do not let those shadow
        # an unqualified higher-level mention. Same-level duplicates stay ambiguous.
        return [r for r in records if r["level"] != "barangay"] or records

    def mentions(self, text, entities):
        result = [{"text": e["text"], "start": e["start"], "end": e["end"], "source": "model"} for e in entities]
        for match in self.helper.finditer(text):
            if not any(match.start() < e["end"] and e["start"] < match.end() for e in result):
                result.append(dict(text=match[0], start=match.start(), end=match.end(), source="reference_rules"))
        return sorted(result, key=lambda m: m["start"])


def shared_name(mention, text):
    """A Davao name that also names a place elsewhere, unless written as "Brgy./Barangay <name>"."""
    return key(mention["text"]) in SHARED_NAMES and not BARANGAY_BEFORE.search(text[:mention["start"]])


def extract_row(row, model, references):
    text = row["ner_input_text"]
    entities = [e for e in (model(text) if text.strip() else []) if e["label"] in {"LOC", "GPE"}]
    mentions = references.mentions(text, entities)
    preliminary = [(m, references.candidates(m["text"])) for m in mentions]
    # A shared name counts only when the post shows it is about Davao: it names another Davao
    # place (the word Davao included), or its author's name says Davao. Otherwise it is held.
    context = bool(DAVAO_WORD.search(row.get("user") or "")) or any(
        len(matches) == 1 and not shared_name(mention, text) for mention, matches in preliminary)
    held = {index for index, (mention, matches) in enumerate(preliminary)
            if matches and not context and shared_name(mention, text)}
    unique = [matches[0] for index, (_, matches) in enumerate(preliminary) if len(matches) == 1 and index not in held]
    provinces = {r["province_code"] for r in unique if r["province_code"]}
    cities = {r["citymun_code"] for r in unique if r["citymun_code"]}
    resolved, candidates, seen = [], [], set()
    ambiguous = unmatched = 0
    for index, (mention, matches) in enumerate(preliminary):
        if index in held:
            candidates.append({**mention, "status": "needs_davao_context", "candidates": matches})
            continue
        choices = matches
        if len(choices) > 1 and len(cities) == 1 and all(r["level"] == "barangay" for r in choices):
            choices = [r for r in choices if r["citymun_code"] in cities]
        elif len(choices) > 1 and len(provinces) == 1:
            choices = [r for r in choices if r["province_code"] in provinces]
        candidates.append({**mention, "status": "matched" if len(choices) == 1 else "ambiguous" if choices else "unmatched",
                           "candidates": choices or matches})
        if len(choices) != 1:
            ambiguous += bool(choices)
            unmatched += not bool(choices)
            continue
        record = choices[0]
        identity = (record["level"], record["code"])
        if identity not in seen:
            seen.add(identity)
            resolved.append({**record, "entity": mention["text"], "source": mention["source"],
                             "start": mention["start"], "end": mention["end"]})
    status = ("no_text" if not text.strip() else "partial" if resolved and (ambiguous or unmatched) else "matched" if resolved
              else "needs_davao_context" if held else "ambiguous" if ambiguous
              else "unmatched_entities" if mentions else "not_found")
    source = "+".join(sorted({m["source"] for m in mentions})) or "none"
    return dict(zip(COLUMNS, [u.dumps(entities), u.dumps(mentions), u.dumps(resolved), u.dumps(candidates),
        "; ".join(f"{r['matched_name']} ({r['level']})" for r in resolved), source, status,
        str(status != "matched" or source == "reference_rules").lower()]))


def process_files(*, input_dir=u.INPUT, output_dir=u.OUTPUT, lookup_dir=u.LOOKUP, revision="main", device="cpu",
                  offline=False, overwrite=False, files=None, recognizer=None):
    references = Gazetteer(lookup_dir)
    model = recognizer if recognizer is not None else u.Recognizer(u.LOC_MODEL, {"LOC", "GPE"}, revision=revision, device=device, offline=offline)
    settings = dict(model=u.LOC_MODEL, revision=revision, lookup_dir=str(Path(lookup_dir).resolve()),
                    lookup_hashes=references.hashes, aliases=ALIASES, shared_names=sorted(SHARED_NAMES),
                    recognizer="test" if recognizer is not None else "xlmr")
    return u.process("locations", COLUMNS, lambda row: extract_row(row, model, references), model, settings,
                     input_dir=input_dir, output_dir=output_dir, overwrite=overwrite, files=files,
                     keep=lambda row: bool(row["location"].strip()))


def main(args):
    if args.download_only:
        u.Recognizer(u.LOC_MODEL, {"LOC"}, revision=args.location_revision,
                     device=args.device, offline=args.local_files_only).load()
        return
    for source in u.discover(args.input_dir) if args.next else [None]:
        result = process_files(input_dir=args.input_dir, output_dir=args.output_dir, lookup_dir=args.lookup_dir,
            revision=args.location_revision, device=args.device, offline=args.local_files_only, overwrite=args.overwrite,
            files=[source] if source else None)
        if result:
            break


if __name__ == "__main__":
    raise SystemExit(u.cli(main, __doc__))
