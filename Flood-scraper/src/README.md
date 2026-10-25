# Flood Social Media Scrapers

This project collects flood-related public posts from configured Facebook
pages and search feeds. Matching posts are filtered for flood keywords and
Philippine locations, then saved as CSV files.

## Requirements

- Python 3.10 or newer
- Google Chrome
- An active internet connection
- A Facebook account, if Facebook asks you to log in

Selenium can automatically download or locate a compatible ChromeDriver. If
that does not work on your machine, install ChromeDriver manually and provide
its path when running the scraper.

## Installation on Windows

Open PowerShell and run these commands from the directory containing the
`Flood-scraper` folder:

```powershell
cd Flood-scraper
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r src\requirements.txt
```

If PowerShell does not allow activation, run this once in PowerShell as your
user:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

## First run and Facebook login

Run the scraper in visible-browser mode the first time. The `--login` option
opens Chrome and waits while you log in manually:

```powershell
python src\scraper\fb_flood_scraper.py --login
```

After logging in, return to PowerShell and press `Enter`. The scraper then
visits the configured pages and searches, and saves the results.

The login session is stored in `.facebook_chrome_profile` so later runs can
reuse it. Do not share or commit this directory because it contains browser
session data.

Close any Chrome window using this profile before starting another run.

## Run the scraper

To run using the saved Facebook session, including post images:

```powershell
python src\scraper\fb_flood_scraper.py
```

The plain command opens Chrome visibly, uses the saved profile, downloads up
to eight images per matching post, and allows up to two hours for all
configured feeds. It inspects at most 50 distinct posts from each configured
page, hashtag, or search feed, then moves on. Only flood-matching posts are
saved; this is not a 50-post limit for the whole run.
It may finish sooner. Edit `RUN_DEFAULTS` near the top of
`src/scraper/fb_flood_scraper.py` to change these settings permanently. To run
without displaying Chrome for one run:

```powershell
python src\scraper\fb_flood_scraper.py --headless
```

To change the per-feed post limit for one run, use
`--max-posts-per-feed 25` (or another positive number).

By default, the scraper has no timestamp cutoff and collects the posts that
Facebook loads while scrolling. To collect only recent posts, provide the
number of hours separately for page feeds and search feeds. For example, the
following collects posts from approximately the last 30 days:

```powershell
python src\scraper\fb_flood_scraper.py --hours-page 720 --hours-search 720
```

Facebook does not always expose a machine-readable post time. Those rows have
`Unknown` in `post_timestamp`; when an hours cutoff is supplied, they are
excluded because their age cannot be verified. Check the output before relying
on a recent-post sample.

The collection flow follows the senior Facebook scraper's page, hashtag,
search, caption-expansion, and scrolling approach. It matches places with the
tables in `data/lookup`, which now cover Davao Region only (see Location
evidence), plus the `PH_LOCATIONS` list. Flood-specific keywords and feed targets remain
in `fb_scraping_config.py`; the CSV layout is unchanged. This scraper additionally checkpoints matches during scrolling so
an interrupted run retains its collected posts.

The Facebook scraper gives the entire scraping run a two-hour time budget
by default, shared across all pages, hashtags, and search feeds. It also
tolerates five consecutive scrolls without page growth before it assumes the
feed has ended. The default maximum remains 80 scrolls per feed. To change
the total time budget or allow more scrolling, for example:

```powershell
python src\scraper\fb_flood_scraper.py --total-time-limit 300 --scroll-times 120 --stalled-scrolls 8
```

Use the help command to see all available options:

```powershell
python src\scraper\fb_flood_scraper.py --help
```

## Facebook post images

`fb_flood_scraper.py` is the only Facebook scraper: it collects post text and
images together. For each matching post it downloads the image files
themselves, not screenshots, to `data/raw/facebook/downloaded_images`
(`fb_<post hash>_01.jpg`, `_02.jpg`, ...) during the same browser run, and its
raw CSV includes `image_paths`, `image_count`, and `image_status`; there is no
second step for new runs. The download code is in
`src/scraper/fb_image_download.py`:

- Photos are opened in Facebook's photo viewer, which serves the full-size,
  uncropped file. A multi-photo post is walked with "Next photo", so photos
  hidden behind a "+N" tile are saved too. The viewer opens in a second tab, so
  the feed keeps its place.
- Link previews and other pictures in the post are saved as shown.
- Only images inside the post are saved: profile pictures, comment photos, ads,
  and other posts on the page are skipped. A video-only post has no image
  (`no_image_found`).
- Files come straight from Facebook's image server in their real format
  (usually JPEG). If the viewer fails, the post's own tiles are downloaded
  instead.

Runs before 2026-10-25 saved element screenshots (`.png`) in
`data/raw/facebook/images`. Some of those show caption text or another post's
picture instead of the post's photo; they are kept only as a record. A blank
`post_url` means no unique post link was found, but images can still be
downloaded from that article.

Use `--max-images 4` to limit images per post or `--max-images 0` to collect
text only. Image capture shares the overall run time budget with post
collection, so raise `--total-time-limit` when you need broader feed coverage.

To retry images that a run missed, for example because a post had not
finished loading, use the same scraper with `--retry-images`:

```powershell
python src\scraper\fb_flood_scraper.py --retry-images
```

This does not scrape feeds. It reopens each saved post that has no saved
images and a unique post link, finds the post by its caption among the posts
Facebook shows on the page, downloads its images, and writes them into that
row of its existing CSV in `data/raw/facebook`. Rows
with only a page or search link are skipped, because images cannot be assigned
to one post safely. A row changes only when images are found, and each change
is saved atomically, so CSVs without new images keep their exact contents. Add
`--login` if Facebook needs a new manual login; a login or checkpoint page
stops the retry with a message. This replaces the former `fb_image_scraper.py`,
which is archived under `data/archive/facebook_image_scraper`.

A raw CSV changed by `--retry-images` no longer matches the checksums recorded
by the cleaning, deduplication, and NER steps. Rebuild those outputs with each
step's `--overwrite` option afterwards.

## Output files

Each run creates a new timestamped CSV in:

```text
data/raw/facebook/fb_raw_floodData_MM-DD-YYYY_HHMMSS.csv
```

For example:

```text
data/raw/facebook/fb_raw_floodData_10-10-2026_143015.csv
```

Each CSV stores the post details and image references in `image_paths`,
`image_count`, and `image_status`. The image files themselves live in
`data/raw/facebook/downloaded_images` (screenshots from runs before 2026-10-25:
`data/raw/facebook/images`); the CSV does not embed image bytes. Older
Gempesaw data, the `latest` snapshot, and evaluation materials are kept under
`data/archive/facebook/<migration timestamp>` rather than mixed with the
dated scraper outputs.

You can request a custom output path with `--output`:

```powershell
python src\scraper\fb_flood_scraper.py `
  --output data\raw\facebook\my_flood_posts.csv
```

The scraper creates a headered CSV before opening Chrome, then saves
checkpoints when it finds new posts during scrolling and
after each page, hashtag, and search feed. If you stop it with `Ctrl+C`, the
latest checkpoint remains in the CSV.

Saved posts are also remembered across runs in
`data/checkpoints/facebook_posts.sqlite3`. On startup, the scraper imports
existing Facebook CSVs from `data/raw/facebook` and loads the saved post IDs.
It skips recognized posts before caption expansion and image capture. Mobile
URLs, tracking parameters, and reel/video URLs with the same post ID are
recognized as the same post. Posts without a unique URL use the author and
caption as a fallback; changes to either can prevent recognition.

Only rows successfully written to a CSV enter this checkpoint. A CSV saved
before an interrupted database update is picked up again on the next startup.
New run CSVs contain newly collected posts; previous results stay in their
original CSVs. Already saved posts still count toward the 50 posts inspected
per feed, so a run can save zero new posts when all inspected posts are known.
The usual command enables this checkpoint automatically; `--checkpoint-file`
can select a different checkpoint location. Use `--retry-images` (see Facebook
post images) to retry images missing from saved rows.

## Clean Facebook text

From `Flood-scraper`, run:

```powershell
python src\preprocessors\text_cleaner.py
```

This reads dated `fb_raw_floodData_*.csv` files in `data/raw/facebook`, sorts
their parsed scrape timestamps oldest first, and commits each cleaned file
before starting the next. It produces one corresponding file per source:

```text
data/cleaned/fb_cleaned_floodData_MM-DD-YYYY_HHMMSS.csv
```

The filename timestamp identifies the original scrape, **not** when cleaning
occurred. Actual timezone-aware cleaning start/end times, row counts, and raw
and output SHA-256 checksums are recorded in
`data/cleaned/_metadata/cleaning_manifest.csv`. Processing in sequence during
one run does not represent separate historical cleaning sessions. No file
timestamps are backdated. To actually process only one pending file per run:

```powershell
python src\preprocessors\text_cleaner.py --next
```

Cleaning changes only `text`, preserving the ten CSV columns, row order,
timestamps, URLs, and image references exactly. Images remain in
`data/raw/facebook/downloaded_images` (older screenshots in
`data/raw/facebook/images`). Keep those locations: image paths are relative to
the project root, not to the cleaned CSV's directory.

The cleaner references the senior project's `clean_text.py`, but does not
discard reports shorter than 90 characters or strip Unicode place names.
It removes URLs embedded in text, HTML/comments/scripts, emojis, visual
separators, decorative punctuation, and common Facebook `See more`/`See less`
labels. It normalizes stylized fonts and whitespace, retains hashtag words,
and preserves case, negation, decimal depths, numeric ranges, dates, times,
and punctuation inside names such as `M'lang`. Meaningful symbols such as
`>=` and `%` become words instead of silently losing their meaning.

No rows are deduplicated or relevance-filtered at this stage. A noise-only
caption becomes empty, but its row and image references remain; counts are
recorded in the manifest. Some raw captions repeat the same paragraph; those
are not automatically collapsed here. Cleaned text is not a confirmation
that a post describes a real flood incident.

Reruns skip outputs only when the source checksum, output checksum, and
cleaner version match the audit record. Changed/unaudited existing files are
preserved and reported, not silently overwritten. Use `--overwrite` only
when intentionally regenerating cleaned outputs. Raw CSVs are never edited.
If a run stops after saving a CSV but before updating the manifest, that
unrecorded output likewise requires explicit `--overwrite` to regenerate.

## Deduplicate cleaned Facebook posts

From `Flood-scraper`, run:

```powershell
python src\preprocessors\text_deduplication.py
```

This reads `data/cleaned/fb_cleaned_floodData_*.csv` oldest first and compares
posts both within each file and against earlier files. One corresponding CSV
is saved for every input, including a header-only CSV when all its rows are
duplicates:

```text
data/deduplicated/fb_deduplicate_floodData_MM-DD-YYYY_HHMMSS.csv
```

The output preserves the input columns and every retained row verbatim. The
earliest qualifying record is kept, not automatically the shortest caption.
Raw/cleaned inputs and photos are never edited, moved, renamed, or deleted.
Image paths remain relative to the project root. A matching row with an image
reference not covered by an earlier retained row is kept separately; image
paths are not merged or rewritten.

Comparison is intentionally conservative:

- Captions are temporarily Unicode/case/whitespace normalized for comparison.
- Repeated captures with the same Facebook post ID and identical normalized
  text can be removed even when publication time is unknown, provided known
  location/time evidence does not conflict.
- Otherwise, nonempty location and explicit time evidence must agree. Locations
  come from optional location fields and exact city/province mentions using
  `data/lookup/refcitymun.csv` and `refprovince.csv` (with a small fallback list).
  These are place mentions, not model-extracted or verified event locations.
- Time evidence uses an ISO event/publication timestamp, or a caption containing
  both a full date (including year) and a clock time. Naive structured times are
  interpreted as Philippine time. Ambiguous slash dates use month/day/year.
  `Unknown`, a date alone, and relative dates such as `today` do not establish
  matching event times. `scraped_timestamp` is never used as event time.
- Following the senior deduplicator, the first 300 characters must score at
  least 97% similarity and the full caption at least 90%. The standard-library
  implementation uses normalized Indel similarity, the same measure as
  `rapidfuzz.fuzz.ratio`; no new dependency is required.
- Similarity alone never removes a changed fact. Body tokens must also agree
  in order, ignoring only a small list of closing courtesy phrases such as
  `stay safe` or `thank you`. Changed numbers, negation, place names, water levels,
  road conditions, and added factual sentences are retained. Typos/paraphrases
  can therefore remain rather than risk merging distinct updates.
- Blank captions are retained, and missing location/time is not considered
  evidence that two unrelated posts describe the same event.

Your current `Unknown` publication times limit near-duplicate removal. The
script does not invent missing dates or perform flood-relevance classification.

Every source row, including removed rows and their original image references,
is preserved in a per-file `*_decisions.csv` under `data/deduplicated/_metadata`.
It records the decision, reason, comparison evidence/scores, and the earlier
source file and logical CSV record number it matched (header is record 1).
`deduplication_manifest.csv` records real processing times, counts, and hashes.
Filename timestamps remain the original scrape times; nothing is backdated.

Reruns replay earlier files to reconstruct cross-file comparisons and skip
verified outputs without rewriting them. To process just one pending file:

```powershell
python src\preprocessors\text_deduplication.py --next
```

Changed inputs, inserted older files, changed lookup tables, and edited or
unaudited outputs require an explicit full rebuild. The lookup tables were
narrowed to Davao Region on 2026-10-23, after the current deduplicated outputs
were built, so the next deduplication run needs this too:

```powershell
python src\preprocessors\text_deduplication.py --overwrite
```

Do not combine `--overwrite` and `--next`: earlier changes can affect every
later file. `--input-dir`, `--output-dir`, and `--lookup-dir` are available for
separate experiments. A failure after a CSV commit but before the audit commit
also requires `--overwrite`; incomplete output is never accepted as verified.

## Optional single-image filter

NER does not use this filter: it reads every deduplicated post, including posts
with several images or none. Use the filter only if you want a separate
single-image subset; NER reads its output only when given it with `--input-dir`.

From `Flood-scraper`:

```powershell
python src\preprocessors\single_image_filter.py
```

This processes dated `data/deduplicated/fb_deduplicate_floodData_*.csv` files in
scrape-timestamp order. It writes one corresponding
`data/single_image/fb_single_image_floodData_<timestamp>.csv` per source, including
header-only files when no rows qualify. Output columns, retained row order, post
text, metadata, URLs, and image paths remain unchanged. Source CSVs and photos
are never edited, moved, or deleted by this filter.

Eligibility requires `image_count=1`, exactly one nonempty saved path, and an
existing file inside the project. Invalid counts, count/path mismatches, missing
files, and outside-project paths are excluded. `data/single_image/_metadata`
contains full per-row keep/exclude decisions and a manifest of counts, reasons,
hashes, and actual processing times. Reruns skip verified results; changed source
data, changed image availability, or edited outputs require `--overwrite`.
`--next` processes only the oldest pending CSV. No new dependencies are needed.

**Scope:** this is an attachment-count filter, not flood-image recognition. One
saved file may still be a weather advisory, collage, unrelated scene, or an
incomplete capture of a multi-photo post. Visual flood-event screening is the
next review step. No flood-content labels, human reference labels, image model
calls, NER inference, or new embeddings are produced here.

## XLM-RoBERTa datetime and location extraction

The modules in `src/xlmr-ner` use the same pretrained model names as the senior
project: `programmersilvanus/ner-xlmr` for date/time and
`Davlan/xlm-roberta-base-ner-hrl` for locations. Separate datetime/location stages
retain their full original and extraction columns. Only the combined stage uses
the compact seven-column schema below. Following the senior project's
configuration, every post gets a datetime, and posts without a matched Davao
location are dropped from the location and combined outputs. Image references
are kept unchanged, with audited evidence copies in per-file metadata CSVs.

All scraper, preprocessing, and NER dependencies are consolidated in
`src/requirements.txt`. Install them in the project-local `.venv` to keep your
global Python environment unchanged. For a fresh setup:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r src\requirements.txt
.\.venv\Scripts\python.exe src\xlmr-ner\run_ner_pipeline.py --download-only
```

The requirements use the senior's NER libraries with versions compatible with
this project's Python 3.13, not an exact reconstruction of their unpinned
environment. Both model weights/tokenizers are cached in `.hf_cache` (large
downloads, ignored by Git). Models run locally on CPU by default. No posts or
images are sent to a hosted inference API. `--device cuda:0` is optional for a
compatible GPU/PyTorch installation.

The single requirements file includes the exact NER package versions verified
for this Windows/Python 3.13 CPU setup, alongside the existing scraper dependency
ranges. Fully offline loading resolves the cached model snapshot to a filesystem
path before loading its tokenizer and weights.

From `Flood-scraper`, process your deduplicated CSVs with:

```powershell
.\.venv\Scripts\python.exe src\xlmr-ner\run_ner_pipeline.py --local-files-only
```

After activating this environment, the equivalent short command is:

```powershell
python src\xlmr-ner\run_ner_pipeline.py
```

If your `.venv` was created with the earlier NER-only requirements, run the
installation command above once to add the scraper dependencies too.

### CSV stages

All dated `data/deduplicated/fb_deduplicate_floodData_*.csv` inputs are sorted by
their parsed scrape timestamps, and every post is processed whatever its image
count. Results are written directly to the three `data/ner` stage folders; no
archive folders are created. First all datetime files are written, then all
location files, then combined files:

```text
data/ner/
  ner_datetime/fb_ner_datetime_floodData_<scrape timestamp>.csv
  ner_locations/fb_ner_locations_floodData_<scrape timestamp>.csv
  ner_datetime_locations/fb_ner_datetime_locations_floodData_<scrape timestamp>.csv
```

Datetime-only outputs retain source columns, provenance IDs, datetime entities,
parsed candidates, `event_time_primary_iso`/`event_time_primary`, and temporal
status/review fields. Location-only outputs retain source columns, provenance
IDs, location entities/candidates, matched `location`, and spatial status/review
fields. With the current ten-column source schema these have 26 and 22 columns,
respectively. Combined outputs contain both extractions in the seven-column
schema below. The datetime stage keeps every input row, and every row has a
datetime (see Datetime evidence). The location and combined stages keep only
posts with a matched Davao location; the location stage saves the others, with
their evidence, in `_metadata/<output filename stem>_dropped.csv`, and its
manifest `status_counts` include a `dropped` count. Header-only inputs produce
header-only outputs at all three stages. Photos keep their project-relative
paths under `data/raw/facebook` (see Replace screenshots in the NER CSVs).

Each stage saves a matching `_metadata/<output filename stem>_details.csv`
containing the original columns and NER evidence. For individual stages this is
an audited copy of the full public CSV. The combined stage joins the details using
stable `ner_record_id` values, not row position, then formats its compact public
CSV without rerunning inference. IDs distinguish repeated URLs and image
associations. In individual-stage and details CSVs, `ner_source_file`, `ner_source_row` (logical
CSV record, header = 1), and `ner_input_text` record provenance. Model offsets
refer to `ner_input_text`, a minimally whitespace/hashtag-normalized copy; the
original `text` is unchanged there.
Each stage's `_metadata/manifest.csv` records actual processing times, row/status
counts, hashes for both public and detailed CSVs, requested configuration and
resolved model commit. Missing or edited evidence invalidates the checkpoint. Output
filename timestamps identify the source scrape; no processing times are backdated.

### Combined CSV schema

Only `ner_datetime_locations` CSVs have exactly:

```csv
source,page_source,text_content,datetime,location,image_paths,url
```

`page_source` is derived from the post URL using the senior's organization-name
mapping; when the URL does not identify a page, the recorded author is used,
otherwise `Unknown`. Search keywords are not treated as authors. `text_content`
is cleaned with the project's Unicode-aware cleaner and lowercased. Short alerts
and repeated records are retained; posts without a matched location are not
(see CSV stages).

`datetime` displays the value as `August 30, 2025 21:00:00` (English
month name, zero-padded day, 24-hour clock, Philippine time). Date-only evidence
is displayed as `August 30, 2025`, with no invented midnight. When the caption
has no datetime, the post time, scrape time, or current time is shown instead;
the datetime stage's `event_time_source` and `datetime_review_required` columns
identify these fallbacks, which the combined CSV does not mark. Existing
`image_paths` and post `url` values are preserved verbatim. Location summaries
are extracted from the project's lookup tables and retained in the location and
combined outputs. Detailed evidence, flags, IDs, and original image metadata
remain in the separate datetime/location CSVs and each stage's `_metadata`, not
as extra columns in the combined CSVs. These outputs
are not proof that the associated image contains a genuine flood scene.

Previous-format checkpoints must not be copied over the new results. The active
stage folders contain only the current subset's outputs and supporting metadata.
Compatible presentation-only changes are restored from verified extraction
evidence on the next run, without calling either model again. Their
output hashes and recipes are updated, and identical combined results are
preserved when refreshing the dependent audit records. No archive is created.

### Datetime evidence (separate CSVs and `_metadata`)

`datetime_entities`/`time_mentions_raw` preserve model spans and scores.
`datetime_rule_mentions`/`time_parsed` preserve explicit-text helpers, candidate
dates, evidence, timezone assumptions and inference flags. The selected result
is stored in `event_time_primary_iso` (unchanged machine-readable ISO value) and
`event_time_primary` (readable display, e.g. `August 30, 2025 21:00:00`), with
`event_time_source`, `event_time_precision`, `event_time_role`, `datetime_status`
and `datetime_review_required`. Publication time stays in `publication_time_iso`.

English/Filipino month names, common Filipino/Cebuano day-part expressions,
explicit full dates and 12/24-hour clocks are supported by normalization helpers.
Model-assisted and rule-only candidates are labeled separately. Times without a
supported timezone are assumed Philippine time; explicit UTC/offsets are converted.
Date-only evidence remains a date, not an invented midnight timestamp. Ambiguous
slash dates such as 03/04/2026 are not parsed. When a caption has several
distinct datetimes, the first in text order is used, as in the senior project,
and the row is flagged for review.

The senior project's priority order then applies. With no caption datetime,
`event_time_primary` falls back to `post_timestamp`, then `scraped_timestamp`,
then the current time, and `event_time_source` records `post_timestamp`,
`scraped_timestamp`, or `fallback_now`. Relative dates and dates without a year
take the missing part from the same reference time, recorded as `anchor` in
`time_parsed`. Every fallback or inferred value sets `datetime_review_required`.
Because Facebook post times are currently `Unknown`, fallbacks use the scrape
time: when the post was collected, not when the flood happened.
Bulletin issuance is identified where explicit wording is recognized; otherwise
the temporal role is unspecified. Unsupported expressions remain in model/raw
evidence where detected. These are extracted time mentions, not verified events.

### Location evidence (separate CSVs and `_metadata`)

`location_entities` retains model spans; `location_mentions_raw` retains model
and reference-helper mentions. `location_raw` contains matched places/codes as
valid JSON, and `location_candidates` retains ambiguous/unmatched evidence.
`location` is the readable matched-place summary. Source/status/review columns
distinguish model detections from lookup helpers and unresolved cases.

The extractor reads **this project's** `data/lookup/refregion.csv`,
`refprovince.csv`, `refcitymun.csv`, and `refbrgy.csv`. Since 2026-10-23 they
cover Davao Region (Region XI) only: 1 region, 5 provinces, 49 cities and
municipalities, and 1,162 barangays, filtered from the senior's tables. The
full nationwide tables remain in `disaster-scraper/data/lookup`. Compostela
Valley is renamed Davao de Oro, its official name since 2019. The tables are
not claimed to be a current PSGC release.

`ALIASES` in `extract_location.py` also maps plain "Davao" and "Dabaw" to Davao
Region (it can mean the city, a province, or the region, all inside Region XI),
"Compostela Valley" to Davao de Oro, and "Samal"/"IGaCoS" to the Island Garden
City of Samal. Barangay names match only when the model tags them as a place or
the text says "Brgy"/"Barangay". Duplicate names need context rather than
selecting the first row. Places outside Davao Region are not matched, so posts
that mention only such places are dropped with the other unmatched posts.
Matches are place mentions, not verified affected locations.

Some Davao names also name places elsewhere: Barangay Pampanga (Davao City)
shares its name with the province, Barangay Luzon (Governor Generoso) with the
island group, and the municipality of Hagonoy with a Bulacan town. Before
2026-10-25 these alone kept about 95 non-Davao posts (Pampanga and Bulacan
floods, Luzon typhoons). `SHARED_NAMES` in `extract_location.py` lists the 85
such names, taken from the nationwide tables in `disaster-scraper/data/lookup`
(Davao names that also name a region, province, city, or municipality elsewhere,
plus Luzon). A shared name counts only when the post shows it is about Davao:
it names another Davao place (the word "Davao" included), writes
"Brgy."/"Barangay" before the name, or comes from an author whose name contains
"Davao". Otherwise the mention has status `needs_davao_context`, and a post
with nothing else has that status and is dropped. The `ALIASES` above are
deliberate Davao readings and are not treated as shared.

### Individual stages, checkpoints, and limitations

```powershell
.\.venv\Scripts\python.exe src\xlmr-ner\extract_datetime.py --local-files-only
.\.venv\Scripts\python.exe src\xlmr-ner\extract_location.py --local-files-only
.\.venv\Scripts\python.exe src\xlmr-ner\run_ner_pipeline.py --local-files-only
```

The full runner reuses valid earlier stage outputs. `--next` processes just the
oldest pending source through all three stages. `--input-dir`, `--output-dir`,
and `--lookup-dir` support separate experiments. Input filenames must follow
`fb_deduplicate_floodData_MM-DD-YYYY_HHMMSS.csv` (optional numeric suffix); the
default input is `data/deduplicated`. Output from the optional single-image
filter (`fb_single_image_floodData_*`) is accepted with an explicit
`--input-dir`. Mixing both filename prefixes at the same timestamp is rejected
to prevent output collisions.
Changed inputs/lookups/settings, edited outputs, and unaudited files require
explicit `--overwrite`; use the full runner to regenerate the combined results.
Atomic per-file saves preserve completed stages on interruption. If a CSV save
succeeded but its audit save failed, explicit `--overwrite` recovers it.
`--datetime-revision` and `--location-revision` accept model commit IDs for
reproducibility; the default requested revision is `main`.

NER does not classify flood relevance, analyze images, or create human ground
truth. Using the senior's models does not guarantee their reported accuracy on
your English/Filipino/Cebuano posts. Review flags and retained evidence support
the independent human evaluation required to measure that accuracy. The
[datetime model card](https://huggingface.co/programmersilvanus/ner-xlmr) and
[location model card](https://huggingface.co/Davlan/xlm-roberta-base-ner-hrl)
describe their training-language/domain limitations. Overlapping token windows
process long captions instead of silently truncating them.

## Replace screenshots in the NER CSVs

The NER CSVs built before 2026-10-25 pointed at screenshots. To swap them for
the posts' downloaded image files (run from `Flood-scraper`):

```powershell
python src\scripts\download_ner_images.py --limit 3   # try a few posts
python src\scripts\download_ner_images.py             # all posts; stop and rerun at any time
python src\scripts\download_ner_images.py --retry     # reopen posts that failed or were not found
python src\scripts\download_ner_images.py --apply     # back up, then rewrite the image columns
```

The download step opens every post in `data/ner/ner_datetime_locations` with
the scraper's Chrome profile and saves its images as described in Facebook post
images. A post with a link is opened by its link. A post without one is looked
up with Facebook search by its caption; only a result from the same author
whose text closely matches is accepted. Each result is appended to
`data/checkpoints/ner_image_downloads.jsonl`, so a rerun continues where the
last one stopped. Twelve linked posts in a row that cannot be found stop the
run, because that usually means Facebook logged out or limited the account.

`--apply` changes only `image_paths` (and, in the `_metadata` details files,
`image_paths`, `image_count`, and `image_status`). A post gets its downloaded
files; a post whose images could not be downloaded gets none rather than its
old screenshots. It adds a record of each rewritten file to the NER manifest,
so later NER runs accept them. NER's `--overwrite` rebuilds these CSVs from
`data/deduplicated`, which still lists the screenshots. The original files and
`download_log.csv` (each post's old and new images and its result) are kept in
`data/archive/ner_datetime_locations_before_image_download_<time>`.

## Flood image classification (Claude Opus 5.5 with the flood image rubric)

`src/image_classification/image_classifier.py` reads the combined NER CSVs in
`data/ner/ner_datetime_locations` and writes
`data/image_classified/fb_image_classified_floodData_<scrape timestamp>.csv`.
Three files next to it hold its settings:

- `image_classifier_config.py`: paths, the model (`claude-opus-5-5`), effort,
  parallel requests, prices and the spending limit, the keep rule, the post
  severity rule, and the answer format Opus fills in.
- `flood_image_rubric.md`: the rules Opus follows, sent with every request as a
  cached system prompt.
- `flood_image_schema.json`: the only values a final per-image record may hold,
  in record order. Every record is checked against it before it is written,
  and its fields follow the schema's order.

All saved images of a post go to Opus in one request, numbered and without the
caption. For each image Opus reports `is_valid_flood_evidence`,
`flood_presence`, a short `visual_description`, and what it observes: depth
anchors (an adult, vehicle, building, or curb, where the water reaches it, and
whether it stands on a road), major impact cues (a vehicle swept away, water
inside a home, houses submerged to the roof, boat or raft transport, rescue),
and whether a road is visible or blocked. The script then sets `visual_severity`
and `infrastructure_accessibility` with the rubric's first-match rules
(sections 4 and 5), which follow the MMDA Flood Gauge System.

Every record of a post also carries `post_visual_severity`, the post's overall
severity. Its rule is set in `image_classifier_config.py`:

- Only images whose `is_valid_flood_evidence` is true count
  (`POST_SEVERITY_COUNTS_IF_TRUE`).
- The highest `visual_severity` among them wins, in the order `no_flood` <
  `not_assessable` < `minor` < `moderate` < `major` (`POST_SEVERITY_ORDER`).
  A counted image is `not_assessable` only when it shows floodwater with
  nothing to judge its depth, so it ranks above a dry photo.
- A post with no counted image gets `not_assessable`
  (`POST_SEVERITY_IF_NO_IMAGE_COUNTS`).

Each image keeps its own `visual_severity`. For example, a post with two
moderate, one minor, and two major images gets `post_visual_severity: "major"`
on all five records. The script sets this value, not Opus. The config refuses
to load if these settings name a level or field the schema doesn't allow.

The output keeps `source`, `page_source`, `text_content`, `datetime`,
`location`, and `url`, and replaces `image_paths` with `image_classification`, a
JSON list with one record per image:

```json
[{"image_path": "data/raw/facebook/downloaded_images/fb_7e2e59cab528139e_01.jpg", "is_valid_flood_evidence": true, "flood_presence": true, "visual_severity": "moderate", "post_visual_severity": "moderate", "infrastructure_accessibility": "not_passable_light_vehicles", "visual_description": "A residential street under brown water reaching the doorsteps; a man wades with water at his knees past a parked car."}]
```

A post is kept when at least one of its images has `is_valid_flood_evidence`
or `flood_presence` set to true (`KEEP_IF_ANY_IMAGE_TRUE`), so a real photo of
a dry street also keeps its post. Posts without images, or whose images all fail
the rule, go to `_metadata/*_excluded.csv` with the reason.
`_metadata/*_evidence.jsonl` keeps, for every image, Opus's raw observations, the
rule behind each label (`decision_basis`), and the model that answered; use it
to score Opus against human labels. `_metadata/manifest.csv` records hashes and
each file's cost. An image that cannot be judged (missing file, refusal, or no
valid answer) gets a `not_assessable` record with `is_valid_flood_evidence`
false, and `--overwrite` asks about it again.

Put your Claude Console key in `Flood-scraper/.env`, which Git ignores, as
`ANTHROPIC_API_KEY=...`. Then run:

```powershell
python src\image_classification\image_classifier.py --max-files 5   # the five oldest pending files
python src\image_classification\image_classifier.py                 # everything left
```

Opus answers 8 posts at a time with low effort. The first post of a run goes
alone, so the cached rubric is written before the other requests read it;
cached reads cost $0.20 per million tokens instead of $4. If Anthropic's safety
checks decline a request, Anthropic re-runs it on its recommended fallback
model, and the evidence file records which model answered. A run stops when the
credit balance runs out or when its estimated cost reaches `--max-usd` (default
$5; `0` turns it off), and it prints its cost at Anthropic's list prices
(`PRICES`). Every answer is cached per image, model, and prompt version in
`data/checkpoints/image_classification.sqlite3`, so running the same command
again continues where it stopped, and an image shared by several posts is sent
once. `--next` classifies only the oldest pending file. `--overwrite` rebuilds
the outputs from the cache and asks again only about images Opus couldn't
classify. Editing the rubric or the answer format starts a new cache version
automatically (`PROMPT_VERSION`). The schema file and the post severity
settings only shape the script's output, so editing them keeps the cache.
Saved outputs record a hash of both (`OUTPUT_VERSION`), so after such an edit
they need `--overwrite`, which rebuilds them from cached answers. Bump
`VERSION` after changing the other rules in the code or the output files. Images are sent to Anthropic; captions are not.

## Configuration

Edit [`src/scraper/fb_scraping_config.py`](scraper/fb_scraping_config.py) to
change:

- Facebook page usernames in `FACEBOOK["users"]`
- Hashtags and search terms in `FACEBOOK["search_queries"]`
- Flood-related matching terms in `KEYWORDS`
- Philippine location terms in `PH_LOCATIONS`

Only add verified Facebook page usernames. The scraper processes content that
is visible in the browser and does not bypass login screens, CAPTCHA, or other
access controls.

## ChromeDriver troubleshooting

If Selenium cannot start ChromeDriver, provide the executable path directly:

```powershell
python src\scraper\fb_flood_scraper.py `
  --headed `
  --chromedriver C:\tools\chromedriver-win64\chromedriver.exe
```

Alternatively, set the `CHROMEDRIVER_PATH` environment variable for the
current PowerShell session:

```powershell
$env:CHROMEDRIVER_PATH = 'C:\tools\chromedriver-win64\chromedriver.exe'
python src\scraper\fb_flood_scraper.py --headed
```
