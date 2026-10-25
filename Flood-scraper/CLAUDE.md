# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Thesis pipeline that collects public flood-related Facebook posts about the Philippines (Davao focus), keeps their images, and extracts event date/time and location with XLM-RoBERTa NER. It extends the senior project in `../disaster-scraper/` (modules say "inspired by disaster-scraper/…"); treat that folder as read-only reference. The project is Facebook-only: X/Twitter support was deliberately removed on 2026-10-23, so don't re-add it unless asked. `src/README.md` is the detailed user manual; keep it in sync when behavior or CLI options change.

## Commands

Run everything from `Flood-scraper/` with the project venv; the global Python lacks the dependencies. Windows/PowerShell; `.\.venv\Scripts\python.exe` works without activating.

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r src\requirements.txt
.\.venv\Scripts\python.exe src\xlmr-ner\run_ner_pipeline.py --download-only   # cache both NER models in .hf_cache
```

Tests use `unittest` (pytest is not installed). There is no linter, formatter, or build step.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s src\tests              # all tests (~176, a few seconds)
.\.venv\Scripts\python.exe -m unittest src.tests.test_text_deduplication  # one module
.\.venv\Scripts\python.exe -m unittest src.tests.test_fb_integration.FacebookImageRetryTests.test_retry_changes_only_rows_that_gain_images
```

Tests are offline: Selenium is mocked and NER uses `FakeNER`, so no browser, login, or model download is needed.

Pipeline, in order. Each stage processes every pending dated file, oldest scrape first:

```powershell
python src\scraper\fb_flood_scraper.py [--login]        # headed Chrome; --login waits for manual login, then Enter
python src\scraper\fb_flood_scraper.py --retry-images   # optional: add missed images to saved raw rows
python src\preprocessors\text_cleaner.py
python src\preprocessors\text_deduplication.py
python src\xlmr-ner\run_ner_pipeline.py --local-files-only   # reads data/deduplicated
python src\scripts\download_ner_images.py [--retry|--apply]   # one-off: swap screenshots in the NER CSVs for downloads
python src\image_classification\image_classifier.py --max-files 5   # Claude Opus 5.5 + flood_image_rubric.md; ANTHROPIC_API_KEY in Flood-scraper/.env
```

`src\preprocessors\single_image_filter.py` is optional and no longer feeds NER. Since 2026-10-23, NER evaluates every deduplicated post, including multi-image and no-image posts.

Shared stage flags: `--next` (oldest pending file only), `--overwrite` (regenerate outputs; deduplication refuses `--next` with `--overwrite`), and `--input-dir/--output-dir/--lookup-dir` for experiments. Scraper defaults live in `RUN_DEFAULTS` at the top of `fb_flood_scraper.py`.

## Architecture

### Data flow and naming

| Stage | Code | Output | Filename prefix |
|---|---|---|---|
| Scrape text + images | `src/scraper/fb_flood_scraper.py` | `data/raw/facebook/` (+ `downloaded_images/`) | `fb_raw_floodData_` |
| Clean `text` only | `src/preprocessors/text_cleaner.py` | `data/cleaned/` | `fb_cleaned_floodData_` |
| Remove duplicates | `src/preprocessors/text_deduplication.py` | `data/deduplicated/` | `fb_deduplicate_floodData_` |
| NER: datetime, locations, combined (reads `data/deduplicated`) | `src/xlmr-ner/` | `data/ner/{ner_datetime,ner_locations,ner_datetime_locations}/` | `fb_ner_<stage>_floodData_` |
| Flood image severity (reads combined NER) | `src/image_classification/image_classifier.py` | `data/image_classified/` | `fb_image_classified_floodData_` |
| Optional, not in the NER path: keep exactly one saved image | `src/preprocessors/single_image_filter.py` | `data/single_image/` | `fb_single_image_floodData_` |

Every stage file keeps the `MM-DD-YYYY_HHMMSS[_N]` suffix of its raw file. That timestamp is the scrape time; processing times live only in manifests and are never backdated. Stages find inputs by regex on these names, so renamed files are silently ignored.

Raw, cleaned, and deduplicated CSVs share 10 columns: `source, query_page, text, post_timestamp, scraped_timestamp, user, post_url, image_paths, image_count, image_status`. CSVs are UTF-8 with BOM (`utf-8-sig`). `image_paths` are project-relative (`data/raw/facebook/downloaded_images/...`; runs before 2026-10-25 point at screenshots in `data/raw/facebook/images/`) and `;`-separated; later stages never copy or move images. The combined NER CSV uses the 7-column `FINAL_COLUMNS` (`source,page_source,text_content,datetime,location,image_paths,url`) from `src/preprocessors/format_ner_posts.py`; full evidence stays in the per-stage CSVs and `_metadata/*_details.csv`.

### Audit/checkpoint model (all post-scrape stages)

- Inputs are never modified. Outputs are written atomically with `_atomic_csv` from `text_cleaner.py`, which every stage reuses.
- Each stage keeps a `_metadata/` manifest with SHA-256s of inputs and outputs plus a `VERSION` or recipe hash. A rerun skips a file only if everything matches; any mismatch raises an error instead of overwriting, and the fix is an explicit `--overwrite`. Bump the module's `VERSION` (or `FORMAT_VERSION`) when output semantics change.
- Upstream changes cascade. For example, `--retry-images` edits raw CSVs, so cleaning, deduplication, and NER outputs all need `--overwrite` afterwards.
- Deduplication replays every earlier file to rebuild cross-file state, and its context hash chains all inputs, so changing or inserting an older file needs a full `--overwrite`, not `--next`.
- The optional single-image filter re-checks that image files exist, so moving or deleting images invalidates its outputs.
- `src/scripts/download_ner_images.py --apply` edits the combined NER CSVs and their details in place, keeping a backup in `data/archive/`, and appends NER manifest records so NER still treats them as current. NER's `--overwrite` would rebuild them from `data/deduplicated`, which still lists the screenshots.
- The location stage's recipe includes the absolute `data/lookup` path and lookup hashes, so moving or renaming the project folder makes NER outputs look stale; `--overwrite` then re-runs inference.
- `reusable_compact_output` in `ner_utils.py` and `src/scripts/format_final_ner_csvs.py` rebuild presentation-only changes to the combined CSV from audited evidence, without re-running the models.

Data-handling rules the code enforces; keep them when editing:
- Deduplication (conservative, with a per-row `*_decisions.csv`) and the optional single-image filter remove rows.
- Since 2026-10-23, NER follows the senior project's configuration at the user's request:
  - every post gets a datetime: the first caption datetime in text order, else `post_timestamp`, else `scraped_timestamp`, else now. `event_time_source` and `datetime_review_required` mark fallbacks and inferred values.
  - the location stage drops posts without a matched place, like the senior's `drop_empty=True`, saving them in `_metadata/*_dropped.csv`. The datetime stage keeps every post; the combined stage keeps only the location stage's posts.
- Outside NER, unknown or ambiguous values stay blank rather than guessed.
- Naive times are Philippine time, `timezone(timedelta(hours=8))`, defined in each module.

### Scraper

- **Browser:** Selenium drives a headed Chrome with a persistent profile in `.facebook_chrome_profile/`, which holds a live Facebook session and is gitignored.
- **Feeds:** from `src/scraper/fb_scraping_config.py`: page usernames (`facebook.com/<page>/posts`), plus each search term as both a hashtag feed and a post search.
- **Filters:** a post is saved only if its caption contains a `KEYWORDS` substring and a place name from the `data/lookup` tables (Davao Region only since 2026-10-23) or `PH_LOCATIONS`.
- **Capture:** text and images come from the same pass. Since 2026-10-25, images are downloaded files, not screenshots: the user rejected screenshots, which sometimes showed caption text or another post's picture. `fb_image_download.py` opens each photo in Facebook's photo viewer (in a second tab during feed runs), walks multi-photo `set=pcb.*` posts with "Next photo", and saves the full-size file from the image server as `downloaded_images/fb_<sha1(post_url or post key)[:16]>_NN.jpg`. The image URL must keep its signed query string (`oh`, `oe`), which `normalize_url` would strip.
- **Finding a post:** an opened post shows in a `[role=dialog]` in front of the previously viewed page, so `find_post_media` matches the caption (text-cleaner tokenization) and prefers the dialog. Comment articles (`aria-label` "Comment by…"/"Reply…") are excluded. Without a post link, `search_post_images` uses Facebook search and accepts only the same author with ≥80% of the caption's key words, opening "See more" first.
- **Defaults:** 50 inspected posts per feed, 8 images per post, and a 2-hour budget shared by all feeds.
- **Saving:** the CSV is checkpointed during scrolling. `data/checkpoints/facebook_posts.sqlite3` remembers saved posts across runs, keyed `facebook:post:<id>` or, without a URL, an author+caption hash. Keys are recorded only after the CSV write succeeds.
- **Terminal output:** `main()` silences logging below ERROR and prints only a `\rPosts scraped: N` counter (`Rows given images: N` for `--retry-images`). Tests assert this exact output.

### NER

`run_ner_pipeline.py` runs two extractors, then joins their results by `ner_record_id`, a hash of source file, checksum, and row number:
- `extract_datetime.py` uses the model `programmersilvanus/ner-xlmr`, plus rule-based date and clock parsing for English, Filipino, and Cebuano.
- `extract_location.py` uses the model `Davlan/xlm-roberta-base-ner-hrl`, then matches places against `data/lookup` (`refregion`, `refprovince`, `refcitymun`, `refbrgy`). Since 2026-10-23 those tables hold only Davao Region rows filtered from the senior's tables; the nationwide originals remain in `../disaster-scraper/data/lookup`. `ALIASES` adds Davao names the tables lack (plain "Davao"/"Dabaw" go to the region, "Compostela Valley" to Davao de Oro, "Samal"/"IGaCoS" to Samal) and is part of the location recipe. `SHARED_NAMES` (85 Davao names that also name a place elsewhere, e.g. Barangay Pampanga, Barangay Luzon, Hagonoy; derived from the nationwide tables) count only with Davao context: another unique Davao place, "Brgy." before the name, or "Davao" in the author's name. Otherwise the status is `needs_davao_context`. Added 2026-10-25 at the user's request, after about 95 Pampanga/Bulacan/Luzon posts had been kept as Davao posts; it is part of the recipe too.

Models load from `.hf_cache` on CPU. `--local-files-only` keeps them offline.

### Image classification

Rebuilt 2026-10-24 to the user's spec: Claude Opus 5.5 only (the Gemini and Sonnet versions and `label_images.py` are gone). Three files configure it:
- `image_classifier_config.py`: paths, `MODEL`, `EFFORT`, `WORKERS`, `PRICES`/`MAX_USD`, `KEEP_IF_ANY_IMAGE_TRUE`, the post severity rule (`POST_SEVERITY_ORDER`, `POST_SEVERITY_COUNTS_IF_TRUE`, `POST_SEVERITY_IF_NO_IMAGE_COUNTS`), the rubric's anchor table (`ANCHOR_LEVELS`) and cue lists, the structured answer format (`ANSWER_SCHEMA`), and `PROMPT_VERSION`.
- `flood_image_rubric.md` (written by the user as rubric 2.0; 2.1 on 2026-10-25 documents `post_visual_severity` in section 4a, at the user's request): the rules Opus reads, sent as a cached system prompt of about 3.9K tokens. Keep sections 2, 3, 4 and 6 numbered as they are: the answer format's descriptions cite them. Caching is what keeps a full run affordable.
- `flood_image_schema.json` (written by the user): the allowed values of the seven final parameters, in record order. `post_records()` writes every record's fields in this order (the user put `post_visual_severity` right after `visual_severity`). `check_record()` refuses anything else and enforces the rubric's consistency rules. Keep the schema a flat list of allowed values: on 2026-10-25 the user undid a two-section version that held the post rule and asked for the rule in the config instead. The schema is not part of `PROMPT_VERSION`, so editing it keeps the answer cache; it and the post settings are hashed into `OUTPUT_VERSION`, so saved outputs then need `--overwrite`.

`image_classifier.py` sends each post's uncached images in one request ("Image N:" labels, no caption). Opus reports `is_valid_flood_evidence`, `flood_presence`, `visual_description`, and observations: anchors as `object:level` with `on_road`, impact cues, road visible/blocked, and `depth_anchor_evidence`. The script sets `visual_severity` and `infrastructure_accessibility` with the rubric's first-match rules (`visual_severity()`, `infrastructure_accessibility()`).
- **Post level:** the user added `post_visual_severity` to the schema on 2026-10-25. `post_visual_severity()` follows the config's post settings: among images with `is_valid_flood_evidence` true, the highest `visual_severity` in `POST_SEVERITY_ORDER` (no_flood < not_assessable < minor < moderate < major) goes on every record of the post, and each image keeps its own level. A post with no counted image gets `not_assessable`. `check_post_settings()` refuses settings the schema doesn't allow when the config is imported. It is script-side and Opus never reports it. Rubric section 4a and the prompt instructions describe it; keep them in sync with the config.
- **Output:** the NER columns with `image_paths` replaced by `image_classification`, a JSON list of seven-parameter records; `_metadata/*_excluded.csv`, `*_evidence.jsonl` (raw answers, `decision_basis`), and `manifest.csv` (hashes, per-file `cost_usd`).
- **Keep rule:** a post stays if any image has `is_valid_flood_evidence` or `flood_presence` true, so a real photo of a dry street keeps its post. This is the user's choice. On 2026-10-25 the user restated it as "drop a post when every picture has is_valid_flood_evidence false and flood_presence false" (invalid pictures have flood_presence null, which counts as false); that was already the rule, so the code did not change, and `KeepRuleTests` now pins it.
- **Cache:** answers per image SHA-256, model, and `PROMPT_VERSION` in `data/checkpoints/image_classification.sqlite3`; reruns resume. Images that can't be judged (missing file, refusal, invalid answer) get a `not_assessable` record and are retried by `--overwrite`.
- **Budget:** the user has limited Anthropic credits ($14.69 on 2026-10-24) and is cost-sensitive. Runs stop at `--max-usd` (default $5) or when credits run out, and print the cost. The user asked to classify the five oldest files first (`--max-files 5`) and check the spend before running the rest.

### Imports

There are no `__init__.py` files.
- Tests import modules as `src.<folder>.<module>`, so run them from `Flood-scraper/`.
- Scripts fall back to sibling imports when run directly (`try: from .x import … except ImportError: from x import …`).
- `src/xlmr-ner/` has a hyphen, so it can't be a package. Its modules use bare imports (`import ner_utils as u`), and `ner_utils.py` puts `src/` on `sys.path` so `preprocessors.*` resolves.
- As a result, one file can load under two module names (`src.preprocessors.text_cleaner` and `preprocessors.text_cleaner`). Patch the module object that the code under test actually uses.

## Known issues (as of 2026-10-23)

- Every Facebook `post_timestamp` is `Unknown`. `extract_post_time` looks for `abbr[data-utime]` and `time[datetime]`, which current Facebook pages lack. As a result, `--hours-page/--hours-search` drop every post, and time-based deduplication and NER date anchoring are weakened.
- Keyword matching is substring-based, so `baha` also matches `bahay`.
- `"ABS News Davao "` in `FACEBOOK["users"]` is not a valid page username.
- With Davao-only tables, places outside Davao that share a name with a Davao place resolve to the Davao one. For example, Pampanga, Bulacan and Luzon become Davao City barangays, and Hagonoy, Sarangani and Caraga become Davao municipalities. In the 2026-10-23 run, 93 of 671 kept posts were kept only by such names (e.g. Pampanga flood posts).
- The deduplicated outputs were built with the old nationwide lookup tables, so the next deduplication run needs `--overwrite`.
- No human ground-truth labels exist yet, so NER accuracy can't be measured; the README notes that independent human evaluation is required.
- `src/tests/test_image_classifier.py` and `src/tests/test_label_images.py` still target the deleted 2026-10-23 classifier and labeling page, so they fail until rewritten or removed. The current classifier is covered offline by `src/tests/test_post_visual_severity.py` (post level, schema field order, post settings, keep rule).

## Repository notes

- The git root is the parent `thesis/` folder, whose remote is the public GitHub repo `KyloRen-01/thesis`. `.facebook_chrome_profile/` was once committed there by mistake; keep it out of commits.
- `data/` holds about 1 GB of CSVs and images, much of it untracked; don't stage it wholesale.
- `data/archive/` holds retired data and code (old Gempesaw data, the former `fb_image_scraper.py`). The pipeline doesn't use it.
