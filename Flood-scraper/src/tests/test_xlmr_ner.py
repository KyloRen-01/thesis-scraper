"""Offline mechanics tests; fake recognizers are not model accuracy evaluation."""
import csv
import io
import json
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "xlmr-ner"))
import ner_utils as u
import extract_datetime as dt
import extract_location as loc
import run_ner_pipeline as runner
from preprocessors.format_ner_posts import FINAL_COLUMNS, format_post

COLUMNS = ["source", "query_page", "text", "post_timestamp", "scraped_timestamp", "user", "post_url",
           "image_paths", "image_count", "image_status"]


def row(text="Flood in Davao City on October 20 2026 at 8 AM", **changes):
    result = dict(zip(COLUMNS, ["Facebook", "Example", text, "Unknown", "2026-10-20 21:00", "Example",
        "https://facebook.com/Example/posts/123", "data/raw/facebook/images/a.png;b.png", "2", "downloaded"]))
    result.update(changes)
    return result


class FakeNER:
    resolved_revision = "offline-test-only"
    def __init__(self, labels=()):
        self.labels, self.calls = labels, []
    def __call__(self, text):
        self.calls.append(text)
        return [dict(text=phrase, label=label, start=text.index(phrase), end=text.index(phrase)+len(phrase), score=.95)
                for phrase, label in self.labels if phrase in text]


def write(path, columns, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def lookup(root):
    write(root/"refregion.csv", ["regDesc", "regCode"], [{"regDesc":"REGION XI (DAVAO REGION)","regCode":"11"}])
    write(root/"refprovince.csv", ["provDesc", "provCode", "regCode"], [
        dict(provDesc="DAVAO DEL SUR",provCode="1124",regCode="11"),
        dict(provDesc="DAVAO DEL NORTE",provCode="1123",regCode="11")])
    write(root/"refcitymun.csv", ["citymunDesc","citymunCode","provCode","regCode"], [
        dict(citymunDesc="CITY OF DAVAO",citymunCode="112402",provCode="1124",regCode="11"),
        dict(citymunDesc="CITY OF TAGUM (Capital)",citymunCode="112319",provCode="1123",regCode="11"),
        dict(citymunDesc="SAN ISIDRO",citymunCode="112318",provCode="1123",regCode="11"),
        dict(citymunDesc="SAN ISIDRO",citymunCode="112411",provCode="1124",regCode="11")])


class DatetimeTests(unittest.TestCase):
    def extract(self, text, model=None, **changes):
        r=row(text,**changes);r["ner_input_text"]=u.prepare_text(text)
        return dt.extract_row(r, model if model is not None else FakeNER())

    def test_explicit_english_date_and_clock(self):
        result=self.extract("Flood on October 20 2026 at 8 AM")
        self.assertEqual(result["event_time_primary_iso"],"2026-10-20T08:00:00+08:00")
        self.assertEqual(result["event_time_source"],"explicit_text_rules")

    def test_model_evidence_separate_from_rule_helpers(self):
        result=self.extract("Flood on October 20 2026 at 8 AM",FakeNER([("October 20 2026","DAT"),("8 AM","TIM")]))
        self.assertEqual(result["event_time_source"],"model_and_rules")
        self.assertEqual(json.loads(result["time_mentions_raw"]),["October 20 2026","8 AM"])

    def test_tagalog_cebuano_clock_and_midnight(self):
        for phrase,hour in [("alas otso ng umaga","08"),("alas dose ng umaga","00"),
                            ("alas dose ng tanghali","12"),("alas otso sa gabii","20"),("alas otso sa buntag","08")]:
            with self.subTest(phrase=phrase):
                self.assertEqual(self.extract("20 Oktubre 2026 "+phrase)["event_time_primary_iso"],f"2026-10-20T{hour}:00:00+08:00")

    def test_iso_and_utc_conversion(self):
        for text in ["Flood 2026-10-20T00:00:00Z","Flood 2026-10-20T00:00:00+00:00","Flood October 20 2026 at 00:00 UTC"]:
            self.assertEqual(self.extract(text)["event_time_primary_iso"],"2026-10-20T08:00:00+08:00")

    def test_date_only_no_invented_midnight(self):
        r=self.extract("Flood on October 20 2026")
        self.assertEqual(r["event_time_primary_iso"],"2026-10-20")
        self.assertEqual(r["event_time_primary"],"October 20, 2026")
        self.assertEqual(r["event_time_precision"],"date")

    def test_missing_year_and_relative_dates_use_scrape_date_without_post_time(self):
        r=self.extract("Flood October 20 at 8 AM")
        self.assertEqual(r["event_time_primary_iso"],"2026-10-20T08:00:00+08:00")
        self.assertEqual(json.loads(r["time_parsed"])[0]["anchor"],"scraped_timestamp")
        self.assertEqual(r["datetime_review_required"],"true")
        self.assertEqual(self.extract("Flood yesterday at 8 AM")["event_time_primary_iso"],"2026-10-19T08:00:00+08:00")

    def test_relative_and_missing_year_prefer_publication_anchor(self):
        self.assertEqual(self.extract("Flood kahapon alas otso ng umaga",post_timestamp="2025-10-20 12:00")["event_time_primary_iso"],"2025-10-19T08:00:00+08:00")
        r=self.extract("Flood October 20 at 8 AM",post_timestamp="2025-10-20 12:00")
        self.assertEqual(r["event_time_primary_iso"],"2025-10-20T08:00:00+08:00")
        self.assertEqual(json.loads(r["time_parsed"])[0]["anchor"],"post_timestamp")
        self.assertEqual(r["datetime_review_required"],"true")

    def test_no_caption_date_falls_back_to_post_then_scrape_then_current_time(self):
        r=self.extract("Flood reported",post_timestamp="2025-10-20 12:00")
        self.assertEqual((r["event_time_primary_iso"],r["event_time_source"]),("2025-10-20T12:00:00+08:00","post_timestamp"))
        self.assertEqual(r["publication_time_iso"],"2025-10-20T12:00:00+08:00")
        for text in ["Flood reported","Flood at 8 AM"]:
            with self.subTest(text=text):
                r=self.extract(text)
                self.assertEqual((r["event_time_primary_iso"],r["event_time_source"]),("2026-10-20T21:00:00+08:00","scraped_timestamp"))
                self.assertEqual((r["event_time_role"],r["datetime_review_required"]),("fallback","true"))
        r=self.extract("Flood reported",scraped_timestamp="")
        self.assertEqual(r["event_time_source"],"fallback_now")
        self.assertTrue(r["event_time_primary_iso"].endswith("+08:00"))

    def test_ambiguous_calendar_dates_and_invalid_dates_are_not_parsed(self):
        for text in ["Flood 03/04/2026 8 AM","Flood February 30 2026","Flood 1.5m deep 200 people"]:
            with self.subTest(text=text):
                r=self.extract(text)
                self.assertEqual(json.loads(r["time_parsed"]),[])
                self.assertEqual(r["event_time_source"],"scraped_timestamp")
        self.assertEqual(self.extract("Flood 20/10/2026 8 AM")["event_time_primary_iso"],"2026-10-20T08:00:00+08:00")

    def test_multiple_times_take_the_first_in_text_order_and_require_review(self):
        r=self.extract("Flood October 20 2026 at 8 AM and at 10 AM")
        self.assertEqual(r["datetime_status"],"ambiguous")
        self.assertEqual(r["event_time_primary_iso"],"2026-10-20T08:00:00+08:00")
        self.assertEqual(r["datetime_review_required"],"true")
        self.assertEqual(len(json.loads(r["time_parsed"])),2)

    def test_bulletin_issuance_not_disaster_occurrence(self):
        self.assertEqual(self.extract("Issued at 08:30 PM 27 October 2025")["event_time_role"],"bulletin_issuance")

    def test_blank_text_never_calls_model(self):
        model=FakeNER();self.assertEqual(self.extract("",model)["datetime_status"],"no_text")
        self.assertEqual(model.calls,[])


class LocationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);lookup(self.root);self.references=loc.Gazetteer(self.root)
    def extract(self,text,pairs=(),user=""):
        return loc.extract_row({"ner_input_text":text,"user":user},FakeNER(pairs),self.references)

    def test_model_entity_matches_city_reference(self):
        r=self.extract("Flood in Davao City",[("Davao City","LOC")])
        self.assertEqual(r["location"],"CITY OF DAVAO (citymun)")
        self.assertEqual(r["location_source"],"model")

    def test_helper_not_claimed_as_model_detection(self):
        r=self.extract("Flood in Davao City")
        self.assertEqual(r["location_source"],"reference_rules")
        self.assertEqual(r["location_review_required"],"true")

    def test_ambiguous_name_not_first_row(self):
        r=self.extract("Flood in San Isidro",[("San Isidro","LOC")],user="Davao Rescue")
        self.assertEqual(r["location"],"")
        self.assertEqual(r["location_status"],"ambiguous")
        self.assertEqual(len(json.loads(r["location_candidates"])[0]["candidates"]),2)

    def test_name_shared_with_places_outside_davao_needs_davao_context(self):
        write(self.root/"refbrgy.csv",["brgyDesc","brgyCode","citymunCode","provCode","regCode"],[
            dict(brgyDesc="Pampanga",brgyCode="112402001",citymunCode="112402",provCode="1124",regCode="11")])
        self.references=loc.Gazetteer(self.root)
        alone=self.extract("Baha sa Lubao Pampanga",[("Pampanga","LOC")])
        self.assertEqual((alone["location"],alone["location_status"]),("","needs_davao_context"))
        held=json.loads(alone["location_candidates"])[0]
        self.assertEqual((held["status"],held["candidates"][0]["matched_name"]),("needs_davao_context","Pampanga"))
        for text,pairs,user in [("Baha sa Pampanga Davao City",[("Pampanga","LOC"),("Davao City","LOC")],""),
                                ("Baha sa Brgy. Pampanga",[("Pampanga","LOC")],""),
                                ("Baha sa Pampanga",[("Pampanga","LOC")],"DXDC 621 RMN Davao")]:
            with self.subTest(text=text,user=user):
                self.assertIn("Pampanga (barangay)",self.extract(text,pairs,user)["location"])

    def test_context_disambiguates_province(self):
        r=self.extract("Flood San Isidro Davao del Norte",[("San Isidro","LOC"),("Davao del Norte","LOC")])
        city=next(r for r in json.loads(r["location_raw"]) if r["level"]=="citymun")
        self.assertEqual(city["province_code"],"1123")

    def test_unmatched_local_landmark_retained(self):
        r=self.extract("Flood Mystery Road",[("Mystery Road","LOC")])
        self.assertEqual(r["location_status"],"unmatched_entities")
        self.assertEqual(json.loads(r["location_mentions_raw"])[0]["text"],"Mystery Road")

    def test_word_boundaries_no_partial_matches(self):
        self.assertEqual(self.extract("davaocity photography")["location_status"],"not_found")

    def test_region_alias(self):
        self.assertIn("REGION XI",self.extract("Flood in Davao Region")["location"])

    def test_davao_aliases(self):
        write(self.root/"refprovince.csv",["provDesc","provCode","regCode"],[dict(provDesc="DAVAO DE ORO",provCode="1182",regCode="11")])
        write(self.root/"refcitymun.csv",["citymunDesc","citymunCode","provCode","regCode"],[
            dict(citymunDesc="ISLAND GARDEN CITY OF SAMAL",citymunCode="112315",provCode="1123",regCode="11")])
        self.references=loc.Gazetteer(self.root)
        for text,expected in [("Baha sa Davao","REGION XI"),("Baha sa Dabaw","REGION XI"),
                              ("Flood in Compostela Valley","DAVAO DE ORO"),("Flood in Samal","ISLAND GARDEN CITY OF SAMAL")]:
            with self.subTest(text=text):
                self.assertIn(expected,self.extract(text)["location"])

    def test_missing_barangays_optional_but_provinces_required(self):
        self.assertEqual(self.references.hashes["refbrgy.csv"],"missing_optional")
        (self.root/"refprovince.csv").unlink()
        with self.assertRaisesRegex(ValueError,"Required"):
            loc.Gazetteer(self.root)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name);self.input=root/"dedup";self.output=root/"ner";self.lookup=root/"lookup"
        self.input.mkdir();lookup(self.lookup)
        self.date_model=FakeNER();self.loc_model=FakeNER([("Davao City","LOC")])
    def source(self,stamp="10-20-2026_230340",rows=None):
        path=self.input/f"fb_deduplicate_floodData_{stamp}.csv"
        write(path,COLUMNS,[row()] if rows is None else rows);return path
    def run_pipeline(self,**options):
        with redirect_stdout(io.StringIO()):
            return runner.run_pipeline(input_dir=self.input,output_dir=self.output,lookup_dir=self.lookup,
                datetime_recognizer=self.date_model,location_recognizer=self.loc_model,**options)

    def test_datetime_keeps_every_post_and_locations_drop_posts_without_a_place(self):
        original=[row(),row("No place or time",image_paths="other.png"),row("")]
        source=self.source(rows=original);before=source.read_bytes()
        self.assertEqual(len(self.run_pipeline()),3)
        expected={"datetime":original,"locations":original[:1],"combined":original[:1]}
        for stage in u.STAGES:
            path=u.target(source,stage,self.output)
            columns,rows,_=u.read_csv(path)
            self.assertEqual(len(rows),len(expected[stage]))
            if stage == "combined":
                self.assertEqual(columns, FINAL_COLUMNS)
                self.assertEqual([r["image_paths"] for r in rows], [r["image_paths"] for r in expected[stage]])
                self.assertEqual([r["url"] for r in rows], [r["post_url"] for r in expected[stage]])
                self.assertEqual([r["text_content"] for r in rows], [format_post(r)["text_content"] for r in expected[stage]])
                self.assertTrue(all(r["datetime"] and r["location"] for r in rows))
            else:
                self.assertEqual([{k:r[k] for k in COLUMNS} for r in rows],expected[stage])
                self.assertEqual(columns,COLUMNS+u.IDENTITY+(dt.COLUMNS if stage=='datetime' else loc.COLUMNS))
                if stage == 'datetime':self.assertNotIn('location',columns)
                else:self.assertNotIn('event_time_primary_iso',columns)
            details=u.read_csv(u.details_path(path))[1]
            self.assertEqual([{k:r[k] for k in COLUMNS} for r in details],expected[stage])
        locations=u.target(source,"locations",self.output)
        dropped=u.read_csv(u.dropped_path(locations))[1]
        self.assertEqual([{k:r[k] for k in COLUMNS} for r in dropped],original[1:])
        self.assertEqual(json.loads(u.previous(locations)["status_counts"])["dropped"],2)
        self.assertEqual(source.read_bytes(),before)
        self.assertEqual(len(self.date_model.calls),2);self.assertEqual(len(self.loc_model.calls),2)

    def test_order_is_all_datetime_all_locations_then_combined(self):
        first=self.source("12-31-2025_235959");second=self.source("01-01-2026_000000")
        self.assertEqual([r["output_file"] for r in self.run_pipeline()],
                         [u.target(p,s).name for s in u.STAGES for p in [first,second]])

    def test_reruns_do_not_reload_models_or_rewrite_outputs(self):
        self.source();self.run_pipeline()
        before={p:(p.read_bytes(),p.stat().st_mtime_ns) for p in self.output.rglob('*') if p.is_file()}
        self.date_model.calls.clear();self.loc_model.calls.clear()
        self.assertEqual(self.run_pipeline(),[])
        self.assertEqual(self.date_model.calls+self.loc_model.calls,[])
        self.assertEqual(before,{p:(p.read_bytes(),p.stat().st_mtime_ns) for p in self.output.rglob('*') if p.is_file()})

    def test_default_input_uses_deduplicated_posts_with_any_image_count(self):
        self.assertEqual(u.INPUT, u.ROOT / "data/deduplicated")

    def test_datetime_display_update_reuses_evidence_without_inference(self):
        source=self.source(rows=[row(),row('Flood in Davao City on October 21 2026')]);self.run_pipeline()
        location=u.target(source,'locations',self.output)
        location_before=location.read_bytes()
        for stage in ['datetime','combined']:
            path=u.target(source,stage,self.output);details_path=u.details_path(path)
            detail_columns,details,_=u.read_csv(details_path)
            for detail in details:detail['event_time_primary']=detail['event_time_primary_iso']
            write(details_path,detail_columns,details)
            if stage=='datetime':
                write(path,detail_columns,details)
                settings=dict(model=u.DATE_MODEL,revision='main',recognizer='test')
                recipe=u.stage_recipe(stage,settings,legacy_datetime=True)
            else:
                public_columns,public,_=u.read_csv(path)
                for public_row,detail in zip(public,details):public_row['datetime']=detail['event_time_primary_iso']
                write(path,public_columns,public);recipe='previous-iso-display-recipe'
            manifest=path.parent/'_metadata/manifest.csv';history=u.history(manifest)
            history[-1]['output_sha256']=u.digest(path.read_bytes())
            history[-1]['details_sha256']=u.digest(details_path.read_bytes())
            history[-1]['recipe_sha256']=recipe;write(manifest,u.MANIFEST,history)
        self.date_model.calls.clear();self.loc_model.calls.clear()
        self.assertEqual(len(self.run_pipeline()),2)
        self.assertEqual(self.date_model.calls+self.loc_model.calls,[])
        dates=u.read_csv(u.target(source,'datetime',self.output))[1]
        final=u.read_csv(u.target(source,'combined',self.output))[1]
        self.assertEqual(dates[0]['event_time_primary'],'October 20, 2026 08:00:00')
        self.assertEqual(dates[0]['event_time_primary_iso'],'2026-10-20T08:00:00+08:00')
        self.assertEqual(final[1]['datetime'],'October 21, 2026')
        self.assertEqual(location.read_bytes(),location_before)
        self.assertEqual(self.run_pipeline(),[])

    def test_restore_compact_individual_schemas_without_inference(self):
        source=self.source(rows=[row(),row('')]);self.run_pipeline()
        final=u.target(source,'combined',self.output);final_before=final.read_bytes()
        for stage in ['datetime','locations']:
            path=u.target(source,stage,self.output)
            _,details,_=u.read_csv(u.details_path(path))
            write(path,FINAL_COLUMNS,[u.format_stage(r,stage) for r in details])
            settings=dict(model=u.DATE_MODEL if stage=='datetime' else u.LOC_MODEL,revision='main',recognizer='test')
            if stage=='locations':
                settings.update(lookup_dir=str(self.lookup.resolve()),lookup_hashes=loc.Gazetteer(self.lookup).hashes,
                                aliases=loc.ALIASES,shared_names=sorted(loc.SHARED_NAMES))
            manifest=path.parent/'_metadata/manifest.csv';history=u.history(manifest)
            history[-1]['output_sha256']=u.digest(path.read_bytes())
            history[-1]['recipe_sha256']=u.stage_recipe(stage,settings,compact=True)
            write(manifest,u.MANIFEST,history)
        manifest=final.parent/'_metadata/manifest.csv';history=u.history(manifest)
        history[-1]['recipe_sha256']='previous-compact-individual-recipe'
        write(manifest,u.MANIFEST,history)
        self.date_model.calls.clear();self.loc_model.calls.clear()
        self.assertEqual(len(self.run_pipeline()),3)
        self.assertEqual(self.date_model.calls+self.loc_model.calls,[])
        self.assertEqual(final.read_bytes(),final_before)
        for stage in ['datetime','locations']:
            path=u.target(source,stage,self.output)
            self.assertEqual(u.read_csv(path)[:2],u.read_csv(u.details_path(path))[:2])
        self.assertEqual(self.run_pipeline(),[])

    def test_single_image_input_filename_and_attachment_validation(self):
        source=self.input/"fb_single_image_floodData_10-20-2026_230340.csv"
        write(source,COLUMNS,[row(image_paths="one.png",image_count="1")])
        self.assertEqual(len(self.run_pipeline()),3)
        self.assertEqual(u.previous(u.target(source,"datetime",self.output))["source_file"],source.name)
        write(source,COLUMNS,[row()])
        with self.assertRaisesRegex(ValueError,"attachment count"):
            self.run_pipeline(overwrite=True)

    def test_empty_input_directory_is_not_reported_as_success(self):
        with self.assertRaisesRegex(ValueError,"No dated"):
            self.run_pipeline()

    def test_mixed_source_stages_cannot_overwrite_same_timestamp(self):
        source=self.source()
        write(self.input/source.name.replace('deduplicate','single_image'),COLUMNS,[])
        with self.assertRaisesRegex(ValueError,"colliding"):
            self.run_pipeline()

    def test_missing_details_blocks_skip_and_combination(self):
        source=self.source();self.run_pipeline()
        u.details_path(u.target(source,"datetime",self.output)).unlink()
        with self.assertRaisesRegex(ValueError,"details"):
            self.run_pipeline()
        with self.assertRaisesRegex(ValueError,"details"):
            runner.combine_files(input_dir=self.input,output_dir=self.output)

    def test_edited_details_detected_even_when_public_csv_unchanged(self):
        source=self.source();self.run_pipeline()
        details=u.details_path(u.target(source,"datetime",self.output))
        details.write_bytes(details.read_bytes()+b'\n')
        with self.assertRaisesRegex(ValueError,"details"):
            self.run_pipeline()

    def test_public_rows_must_match_detailed_evidence(self):
        source=self.source();self.run_pipeline()
        path=u.target(source,"locations",self.output)
        columns,rows,_=u.read_csv(path)
        rows[0]['location']='Invented location';write(path,columns,rows)
        manifest=path.parent/'_metadata/manifest.csv';history=u.history(manifest)
        history[-1]['output_sha256']=u.digest(path.read_bytes());write(manifest,u.MANIFEST,history)
        with self.assertRaisesRegex(ValueError,'detailed evidence'):
            runner.combine_files(input_dir=self.input,output_dir=self.output,overwrite=True)

    def test_next_processes_one_file_through_three_stages(self):
        first=self.source("10-09-2026_211231");second=self.source()
        self.assertEqual(len(self.run_pipeline(next_only=True)),3)
        self.assertTrue(u.target(first,"combined",self.output).exists())
        self.assertFalse(u.target(second,"combined",self.output).exists())
        self.assertEqual(len(self.run_pipeline(next_only=True)),3)
        self.assertEqual(self.run_pipeline(next_only=True),[])

    def test_changed_source_requires_explicit_regeneration(self):
        self.source();self.run_pipeline();self.source(rows=[row("Baha")])
        with self.assertRaisesRegex(ValueError,"--overwrite"):self.run_pipeline()
        self.assertEqual(len(self.run_pipeline(overwrite=True)),3)

    def test_changed_lookup_requires_explicit_regeneration(self):
        self.source();self.run_pipeline()
        p=self.lookup/"refprovince.csv";p.write_bytes(p.read_bytes()+b'\n')
        with self.assertRaisesRegex(ValueError,"--overwrite"):self.run_pipeline()

    def test_header_only_file_still_produces_three_csvs(self):
        source=self.source(rows=[]);self.run_pipeline()
        for stage in u.STAGES:self.assertEqual(u.read_csv(u.target(source,stage,self.output))[1],[])
        self.assertEqual(self.date_model.calls+self.loc_model.calls,[])

    def test_ids_distinguish_repeated_post_urls_with_different_images(self):
        source=self.source(rows=[row(),row(image_paths="other.png")]);self.run_pipeline()
        records=u.read_csv(u.details_path(u.target(source,"datetime",self.output)))[1]
        self.assertNotEqual(records[0]["ner_record_id"],records[1]["ner_record_id"])
        final=u.read_csv(u.target(source,"combined",self.output))[1]
        self.assertEqual(final[1]["image_paths"],"other.png")
        self.assertEqual(len(final),2)

    def test_failed_inference_no_partial_output(self):
        source=self.source(rows=[row(),row("Second")]);self.date_model=Mock(side_effect=[[],RuntimeError('failed')])
        with self.assertRaisesRegex(RuntimeError,'failed'):self.run_pipeline()
        self.assertFalse(u.target(source,"datetime",self.output).exists())

    def test_changed_intermediate_rejected(self):
        source=self.source();self.run_pipeline();path=u.target(source,"datetime",self.output)
        path.write_bytes(path.read_bytes()+b'\n')
        with self.assertRaisesRegex(ValueError,'Stale'):runner.combine_files(input_dir=self.input,output_dir=self.output)

    def test_join_uses_ids_not_positions(self):
        source=self.source(rows=[row(),row("Flood in Tagum")]);self.run_pipeline()
        path=u.target(source,"locations",self.output);details=u.details_path(path)
        columns,rows,_=u.read_csv(details)
        write(details,columns,list(reversed(rows)))
        public_columns,public_rows,_=u.read_csv(path)
        write(path,public_columns,list(reversed(public_rows)))
        manifest=path.parent/'_metadata/manifest.csv';history=u.history(manifest)
        history[-1]['output_sha256']=u.digest(path.read_bytes());write(manifest,u.MANIFEST,history)
        history[-1]['details_sha256']=u.digest(details.read_bytes());write(manifest,u.MANIFEST,history)
        with redirect_stdout(io.StringIO()):runner.combine_files(input_dir=self.input,output_dir=self.output,overwrite=True)
        rows=u.read_csv(u.target(source,'combined',self.output))[1]
        self.assertIn('DAVAO',rows[0]['location']);self.assertIn('TAGUM',rows[1]['location'])

    def test_protected_directories_rejected(self):
        self.source()
        for output in [self.input,self.input/'nested',self.input.parent,u.ROOT/'data/raw']:
            with self.assertRaisesRegex(ValueError,'separate'):
                runner.run_pipeline(input_dir=self.input,output_dir=output,lookup_dir=self.lookup,
                    datetime_recognizer=self.date_model,location_recognizer=self.loc_model)

    def test_malformed_csv_and_reserved_headers(self):
        source=self.source()
        for content in ['text,text\na,b\n','body\nhello\n','text,source\nhello\n','text,ner_record_id\na,123\n']:
            source.write_text(content,encoding='utf-8')
            with self.assertRaises(ValueError):self.run_pipeline()

    def test_manifest_failure_detected_on_restart(self):
        self.source();original=u._atomic_csv
        def fail_manifest(path,columns,rows):
            if path.name=='manifest.csv':raise OSError('locked')
            return original(path,columns,rows)
        with patch.object(u,'_atomic_csv',side_effect=fail_manifest):
            with self.assertRaises(OSError):self.run_pipeline()
        with self.assertRaisesRegex(ValueError,'--overwrite'):self.run_pipeline()
        self.assertEqual(len(self.run_pipeline(overwrite=True)),3)


class ModelLoaderTests(unittest.TestCase):
    def test_individual_download_only_commands_do_not_process_csvs(self):
        args=u.parser('test').parse_args(['--download-only','--local-files-only'])
        for module in [dt,loc]:
            with self.subTest(module=module.__name__),patch.object(u,'Recognizer') as model,patch.object(module,'process_files') as processing:
                module.main(args)
                model.return_value.load.assert_called_once()
                processing.assert_not_called()

    def test_cached_loading_uses_local_paths_and_overlapping_windows(self):
        tokenizer=types.SimpleNamespace(is_fast=True,model_max_length=10**30)
        model=types.SimpleNamespace(config=types.SimpleNamespace(id2label={0:'O',1:'B-LOC'},max_position_embeddings=514,model_type='xlm-roberta'))
        auto_tokenizer=Mock(return_value=tokenizer);auto_model=Mock(return_value=model)
        pipeline=Mock(return_value=lambda text:[])
        snapshot=Mock(return_value=str(Path('cache/snapshots/commit123')))
        modules={'torch':types.SimpleNamespace(get_num_threads=lambda:8,set_num_threads=Mock()),
                 'huggingface_hub':types.SimpleNamespace(snapshot_download=snapshot),
                 'transformers':types.SimpleNamespace(AutoTokenizer=types.SimpleNamespace(from_pretrained=auto_tokenizer),
                    AutoModelForTokenClassification=types.SimpleNamespace(from_pretrained=auto_model),pipeline=pipeline)}
        recognizer=u.Recognizer('model',{'LOC'},offline=True)
        with patch.dict(sys.modules,modules),redirect_stdout(io.StringIO()):
            self.assertEqual(recognizer('word '*2000),[])
        self.assertTrue(snapshot.call_args.kwargs['local_files_only'])
        self.assertEqual(auto_tokenizer.call_args.args[0],snapshot.return_value)
        self.assertTrue(auto_tokenizer.call_args.kwargs['local_files_only'])
        self.assertFalse(auto_tokenizer.call_args.kwargs['fix_mistral_regex'])
        self.assertEqual(tokenizer.model_max_length,512)
        self.assertEqual(pipeline.call_args.kwargs['stride'],64)
        self.assertEqual(recognizer.resolved_revision,'commit123')


if __name__=='__main__':unittest.main()
