"""Settings for image_classifier.py: files, model, budget, the keep and post severity rules, and what Opus answers.

Opus follows flood_image_rubric.md, and flood_image_schema.json lists the only values a final
per-image record may hold, in record order. How a post's overall severity (post_visual_severity) is
chosen is set here (POST_SEVERITY_ORDER). Both files are read from this folder, so editing either
changes the next run. A changed rubric or answer format also starts a new answer cache
(PROMPT_VERSION). The schema and the post settings only shape the script's output, so editing them
keeps the cache; saved outputs then need --overwrite (OUTPUT_VERSION).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE_DIR = HERE.parents[1]  # Flood-scraper
INPUT_DIR = BASE_DIR / "data/ner/ner_datetime_locations"
OUTPUT_DIR = BASE_DIR / "data/image_classified"
CACHE_PATH = BASE_DIR / "data/checkpoints/image_classification.sqlite3"
ENV_FILE = BASE_DIR / ".env"
SCHEMA_PATH = HERE / "flood_image_schema.json"
RUBRIC_PATH = HERE / "flood_image_rubric.md"

# Model and requests. Opus 5.5 always thinks before answering; low effort keeps that short.
MODEL = "claude-opus-5-5"
EFFORT = "low"
MAX_TOKENS = 16000
WORKERS = 8
TIMEOUT_SECONDS = 180
RETRIES = 5  # The Anthropic SDK retries rate limits, overloads, and dropped connections itself.
FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5"}  # Models with Anthropic's server-side refusal fallback.

# Budget: Anthropic's USD prices per 1M tokens (October 2026) and the default per-run spending limit.
PRICES = {
    "claude-opus-5-5": {"input": 4.00, "cache_write": 5.00, "cache_read": 0.20, "output": 20.00},
    "claude-sonnet-5-5": {"input": 2.00, "cache_write": 2.50, "cache_read": 0.10, "output": 10.00},
}
MAX_USD = 5.0

# Output: the combined NER columns, with image_paths replaced by a JSON list of per-image records.
OUTPUT_COLUMNS = ["source", "page_source", "text_content", "datetime", "location", "image_classification", "url"]
IMAGE_COLUMN = "image_classification"
VERSION = "3"  # Bump when the rules below or the output files change; OUTPUT_VERSION covers the schema and post settings.

# Keep rule: a post stays when at least one of its images has one of these fields set to true.
# Posts whose images are all false/null for both, and posts without images, are dropped.
KEEP_IF_ANY_IMAGE_TRUE = ("is_valid_flood_evidence", "flood_presence")


def keep_post(records: list[dict]) -> bool:
    return any(record.get(field) is True for record in records for field in KEEP_IF_ANY_IMAGE_TRUE)


def anthropic_api_key(env_file: Path = ENV_FILE) -> str:
    """ANTHROPIC_API_KEY from the environment, else from Flood-scraper/.env. Never printed."""
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key and env_file.is_file():
        for line in env_file.read_text(encoding="utf-8-sig").splitlines():
            name, separator, value = line.partition("=")
            if separator and name.strip().removeprefix("export ").strip() == "ANTHROPIC_API_KEY":
                key = value.strip().strip("\"'")
    if not key:
        raise RuntimeError(f"Set ANTHROPIC_API_KEY in {env_file} or the environment.")
    return key


# Allowed values of every final record, in record order (flood_image_schema.json).
ALLOWED_VALUES = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

# Post level: post_visual_severity in flood_image_schema.json. Every image record of a post gets the
# same value: the highest visual_severity among the post's counted images, in POST_SEVERITY_ORDER.
# Each image keeps its own visual_severity. Only images with POST_SEVERITY_COUNTS_IF_TRUE set to true
# count, so a graphic or an image that couldn't be judged never sets the post's level. A counted image
# is not_assessable only when it shows floodwater with nothing to judge its depth, so it ranks above a
# dry photo. A post with no counted image gets POST_SEVERITY_IF_NO_IMAGE_COUNTS.
POST_SEVERITY_ORDER = ["no_flood", "not_assessable", "minor", "moderate", "major"]  # Lowest to highest.
POST_SEVERITY_COUNTS_IF_TRUE = "is_valid_flood_evidence"
POST_SEVERITY_IF_NO_IMAGE_COUNTS = "not_assessable"


def check_post_settings(allowed: dict) -> None:
    """Refuse post settings that flood_image_schema.json doesn't allow, before any image is sent."""
    image_levels, post_levels = allowed.get("visual_severity"), allowed.get("post_visual_severity")
    problems = []
    if not (isinstance(image_levels, list) and isinstance(post_levels, list)):
        problems.append("flood_image_schema.json needs visual_severity and post_visual_severity lists")
    else:
        if not (POST_SEVERITY_ORDER and len(set(POST_SEVERITY_ORDER)) == len(POST_SEVERITY_ORDER)
                and set(POST_SEVERITY_ORDER) <= set(image_levels) & set(post_levels)):
            problems.append("POST_SEVERITY_ORDER must list distinct levels that both visual_severity and "
                            "post_visual_severity allow")
        if POST_SEVERITY_IF_NO_IMAGE_COUNTS not in post_levels:
            problems.append("POST_SEVERITY_IF_NO_IMAGE_COUNTS must be a post_visual_severity value")
    if allowed.get(POST_SEVERITY_COUNTS_IF_TRUE) not in ([True, False], [False, True]):
        problems.append("POST_SEVERITY_COUNTS_IF_TRUE must name a true/false field of flood_image_schema.json")
    if problems:
        raise ValueError("Post severity settings in image_classifier_config.py: " + "; ".join(problems) + ".")


check_post_settings(ALLOWED_VALUES)
POST_RANK = {level: rank for rank, level in enumerate(POST_SEVERITY_ORDER)}
# Saved outputs count as current only for the same VERSION, schema (with its field order), and post
# settings, so editing the schema or the post settings asks for --overwrite.
OUTPUT_VERSION = VERSION + "-" + hashlib.sha256(json.dumps(
    [ALLOWED_VALUES, POST_SEVERITY_ORDER, POST_SEVERITY_COUNTS_IF_TRUE, POST_SEVERITY_IF_NO_IMAGE_COUNTS]
).encode()).hexdigest()[:12]
RUBRIC = RUBRIC_PATH.read_text(encoding="utf-8")
DESCRIPTION_MAX_WORDS = 40

# Script rules from the rubric (sections 4 and 5). Opus reports each depth anchor as
# "object:where the water reaches it"; the script maps it to a level and the deepest anchor wins.
ANCHOR_LEVELS = {
    "adult:ankle": "minor", "adult:mid_shin": "minor", "adult:knee": "moderate",
    "adult:thigh": "major", "adult:waist": "major", "adult:chest_or_higher": "major",
    "vehicle:below_half_tyre": "minor", "vehicle:above_half_tyre_below_top": "moderate",
    "vehicle:tyre_top": "major", "vehicle:doors_or_bonnet": "major", "vehicle:windows_or_roof": "major",
    "building:at_doorstep": "moderate", "building:above_doorstep": "major",
    "building:window_sills_or_higher": "major",
    "curb:below_curb_top": "minor", "curb:above_curb_top": "moderate",
}
LEVEL_RANK = {"minor": 1, "moderate": 2, "major": 3}
MAJOR_IMPACT_CUES = ["vehicle_floating_or_swept_away", "water_inside_home", "houses_submerged_to_roof",
                     "boat_or_raft_transport", "people_being_rescued"]
ROAD_STOPPING_CUES = {"vehicle_floating_or_swept_away", "boat_or_raft_transport"}
ACCESSIBILITY_BY_LEVEL = {"minor": "passable_all_vehicles", "moderate": "not_passable_light_vehicles",
                          "major": "not_passable_all_vehicles"}

# What Opus returns for each image (structured output). It never sets visual_severity,
# infrastructure_accessibility, or post_visual_severity: the script derives them from these observations.
IMAGE_ANSWER = {
    "type": "object",
    "properties": {
        "image_number": {"type": "integer", "description": "N from the 'Image N:' label before the image."},
        "is_valid_flood_evidence": {"type": "boolean", "description": "Rubric section 2."},
        "flood_presence": {"anyOf": [{"type": "boolean"}, {"type": "null"}],
                           "description": "Rubric section 3. null only when is_valid_flood_evidence is false."},
        "anchors": {
            "type": "array",
            "description": "Every object showing how deep the floodwater is, with where the water reaches it "
                           "(rubric section 4 table) and whether the object stands on a road. Empty when there is "
                           "no floodwater or nothing shows its depth.",
            "items": {
                "type": "object",
                "properties": {"anchor": {"type": "string", "enum": list(ANCHOR_LEVELS)},
                               "on_road": {"type": "boolean"}},
                "required": ["anchor", "on_road"],
                "additionalProperties": False,
            },
        },
        "impact_cues": {"type": "array", "items": {"type": "string", "enum": MAJOR_IMPACT_CUES},
                        "description": "Major impact cues visible in the image (rubric section 4, rule 3)."},
        "road_visible": {"type": "boolean", "description": "A road, street, or bridge is visible."},
        "road_blocked": {"type": "boolean",
                         "description": "The road is blocked by debris, fallen trees, a landslide, or a collapsed section."},
        "depth_anchor_evidence": {"type": "string",
                                  "description": "One short sentence naming the deepest anchor, such as "
                                                 "'Adult in green shirt: water at the thigh.' Empty if there is none."},
        "visual_description": {"type": "string", "description": "Rubric section 6: at most 40 words."},
    },
    "required": ["image_number", "is_valid_flood_evidence", "flood_presence", "anchors", "impact_cues",
                 "road_visible", "road_blocked", "depth_anchor_evidence", "visual_description"],
    "additionalProperties": False,
}
ANSWER_SCHEMA = {"type": "object", "properties": {"images": {"type": "array", "items": IMAGE_ANSWER}},
                 "required": ["images"], "additionalProperties": False}

INSTRUCTIONS = """You label images from public Facebook posts about flooding in the Philippines. Follow the rubric below exactly.

For every image, return is_valid_flood_evidence, flood_presence, anchors, impact_cues, road_visible, road_blocked, depth_anchor_evidence, and visual_description. Do not decide visual_severity, infrastructure_accessibility, or post_visual_severity: the script sets them from your observations with the rules in the rubric.

- Judge each image on its own, from what is visible. Ignore the post caption and words in the image, such as headlines, warning levels, and logos, except place names printed on a visible sign.
- A saved image may be a screenshot of a Facebook post or photo grid with several photos, some cut off. Ignore the app interface and caption text. Treat the real photos in one screenshot together: report anchors, impact cues, and road observations from any of them, and describe the most flooded one.
- Report a building anchor only when water reaches its doorstep or higher. List each anchor that shows the depth, and mark on_road for anchors standing on a road, street, or bridge.
- When is_valid_flood_evidence is false, flood_presence is null, anchors and impact_cues are empty, and road_visible and road_blocked are false."""
SYSTEM_PROMPT = f"{INSTRUCTIONS}\n\n<rubric>\n{RUBRIC}\n</rubric>"
USER_PROMPT = ("Label the {count} images above, labeled Image 1 to Image {count}. "
               "Return one entry per image with its image_number.")

# Cached answers are reused only for the same instructions, rubric, answer format, and effort.
PROMPT_VERSION = hashlib.sha256(json.dumps([SYSTEM_PROMPT, USER_PROMPT, ANSWER_SCHEMA, EFFORT],
                                           sort_keys=True).encode()).hexdigest()[:16]
