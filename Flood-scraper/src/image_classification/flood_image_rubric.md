# Flood Image Final Parameters

Rubric 2.1 · 2026-10-25 · Model: Claude Opus 5.5 (`claude-opus-5-5`)

Every labelled image ends up as one record with seven parameters. `image_classifier.py` writes each post's records as a JSON list in the `image_classification` column of `data/image_classified/fb_image_classified_floodData_<timestamp>.csv`. `flood_image_schema.json` lists the only choices each parameter can take, and the script checks every record against it before writing, so no other value can appear.

Opus sets three parameters directly from the image. The script sets the other four: `image_path` from the input, `visual_severity` and `infrastructure_accessibility` by applying fixed rules to what Opus observed, and `post_visual_severity` from the `visual_severity` of every image in the post. The rules Opus follows are in `flood_image_rubric.md`.

## Allowed choices (`flood_image_schema.json`)

```json
{
  "image_path": "string: path of the image file",
  "is_valid_flood_evidence": [true, false],
  "flood_presence": [true, false, null],
  "visual_severity": ["no_flood", "minor", "moderate", "major", "not_assessable"],
  "post_visual_severity": ["no_flood", "minor", "moderate", "major", "not_assessable"],
  "infrastructure_accessibility": ["passable_all_vehicles", "not_passable_light_vehicles", "not_passable_all_vehicles", "no_road_visible", "not_assessable"],
  "visual_description": "string: one or two sentences of visible facts, at most 40 words"
}
```

A list means the value must be one of those choices. `image_path` and `visual_description` are the only free-text fields. The script also checks the consistency rules below before writing a record.

## Example record

```json
{
  "image_path": "data/raw/facebook/downloaded_images/fb_7e2e59cab528139e_01.jpg",
  "is_valid_flood_evidence": true,
  "flood_presence": true,
  "visual_severity": "moderate",
  "post_visual_severity": "moderate",
  "infrastructure_accessibility": "not_passable_light_vehicles",
  "visual_description": "A residential street under brown water reaching the doorsteps; a man wades with water at his knees past a parked car."
}
```

## The seven parameters at a glance

| Parameter | Type | Allowed values | Set by |
|---|---|---|---|
| `image_path` | string | Path as listed in the input | Script |
| `is_valid_flood_evidence` | boolean | `true`, `false` | Opus |
| `flood_presence` | boolean or null | `true`, `false`, `null` | Opus |
| `visual_severity` | string | `no_flood`, `minor`, `moderate`, `major`, `not_assessable` | Script, from Opus's observations |
| `post_visual_severity` | string | `no_flood`, `minor`, `moderate`, `major`, `not_assessable` | Script, from the `visual_severity` of the post's images |
| `infrastructure_accessibility` | string | `passable_all_vehicles`, `not_passable_light_vehicles`, `not_passable_all_vehicles`, `no_road_visible`, `not_assessable` | Script, from Opus's observations |
| `visual_description` | string | One or two sentences, at most 40 words | Opus |

## 1. image_path

The image's path exactly as it appears in the input CSV (`image_path` or `image_paths` column) or folder. It links the record back to its post: the post ID is the part of the file name before the image number, for example `fb_7e2e59cab528139e`.

## 2. is_valid_flood_evidence

| Value | Meaning |
|---|---|
| `true` | A real photo, or a clear video or TV frame, of an actual place where the ground, water, people or vehicles can be seen |
| `false` | A weather bulletin, rainfall warning or advisory card; a map, chart, poster or infographic; a text or quote graphic; a meme or edited image; a logo; a portrait or group photo where the ground cannot be seen; officials at a briefing; or an image too dark, blurred, small or cropped to judge |

**Basis:** MEDIC separates informative images from cartoons, banners, logos and advertisements (Alam et al., 2023). Zero-shot models, including Claude 3.5 Sonnet, sorted crisis posts by informativeness with binary F1 of about 0.84–0.86 (McDaniel et al., 2024), and a training-free vision-language model reached F1 0.84 on images (Schwarz et al., 2026). MEDIC counts warnings as informative; here a warning card is `false` because it shows no flood scene.

## 3. flood_presence

| Value | Meaning |
|---|---|
| `true` | Floodwater covers ground that is normally dry: roads, sidewalks, yards, floors, fields. Water spilling over a river bank onto land counts. |
| `false` | Dry or wet surfaces, rain, isolated puddles, or a river, canal or the sea within its normal banks or shoreline |
| `null` | The image is not valid evidence |

**Basis:** MEDIC's flood class shows "flooded areas, houses, roads, and other infrastructures" (Alam et al., 2023). Zero-shot flood detection needs human checking: only 65.8% of images flagged by a vision-language model were truly flooded, with small puddles counted as not flooded (Franchi et al., 2025), and flood-scene detection rose from 79.96% zero-shot to 99.73% after fine-tuning (Fuad & Qian, 2026).

## 4. visual_severity

The script sets this from Opus's anchors (an object and where the water reaches it) and its impact cue. The first rule that fits wins:

1. Not valid evidence → `not_assessable`
2. No floodwater → `no_flood`
3. A major impact cue is visible (vehicle floating or swept away, water inside a home, houses submerged to the roof, boat or raft transport, people being rescued) → `major`
4. At least one anchor → the level of the **deepest** anchor, from the table below
5. Otherwise → `not_assessable`

| Object | Minor | Moderate | Major |
|---|---|---|---|
| Adult standing or wading | Ankle, mid-shin | Knee | Thigh, waist, chest or higher |
| Car, jeepney, motorcycle or tricycle | Below half the tyre | Above half the tyre, below its top | Tyre top, doors or bonnet, windows or roof |
| Building at street level | – | At the doorstep | Above the doorstep, window sills or higher |
| Curb or sidewalk | Below the curb top | Above the curb top | – |

| Level | MMDA Flood Gauge System (2024) | MMDA category | PAGASA colour (context only) | Value for calculations |
|---|---|---|---|---|
| `no_flood` | No floodwater | – | – | 0 |
| `minor` | Gutter-deep, half-knee-deep | Passable to all types of vehicles | Yellow | 1 |
| `moderate` | Half-tire-deep, knee-deep | Not passable to light vehicles | Orange | 2 |
| `major` | Tire-deep, waist-deep, chest-deep | Not passable to all types of vehicles | Red | 3 |
| `not_assessable` | Cannot be judged from the image | – | – | none; leave out of averages |

**Basis:** the levels follow the MMDA Flood Gauge System (2024). The vision model naming the object and submerged part while a rule table sets the level, "the candidate with the largest mapped depth" being used, and no level without a valid object all follow Zhang et al. (2026). Body-part and vehicle-part anchors come from J. Li et al. (2023), Akinboyewa et al. (2024) and Du et al. (2026); a similar three-level scale (safe, unsafe, dangerous) from submerged vehicles is used by Xue et al. (2026). Impact cues follow MEDIC's "severe" damage and "rescue" categories (Alam et al., 2023). PAGASA's colours are rainfall warnings and are shown for familiarity only.

## 4a. post_visual_severity

The post's overall severity. The script sets it from the `visual_severity` of the post's images and gives every image record of the post the same value. Each image keeps its own `visual_severity`, and Opus never reports this field.

Only images whose `is_valid_flood_evidence` is `true` count, so a graphic or an image that could not be judged never sets the post's level. The first rule that fits wins:

1. No image counts → `not_assessable`
2. Otherwise → the **highest** level among the counted images, in the order `no_flood` < `not_assessable` < `minor` < `moderate` < `major`

A counted image is `not_assessable` only when it shows floodwater with nothing to judge its depth, so it ranks above a dry photo. For example, a post with two `moderate`, one `minor` and two `major` images is `major` on all five records.

**Basis:** a post is rated by its most severe image, so one deep-water photo is not averaged away by milder ones. No published source is cited for this rule yet.

## 5. infrastructure_accessibility

The script sets this from the anchors Opus marked as standing on a road, whether the road is blocked, and the impact cue. The first rule that fits wins:

1. Not valid evidence, or no road visible → `no_road_visible`
2. The road is blocked by debris, fallen trees, a landslide or a collapsed section → `not_passable_all_vehicles`
3. No floodwater → `passable_all_vehicles`
4. At least one anchor on the road → from the deepest on-road anchor: Minor → `passable_all_vehicles`, Moderate → `not_passable_light_vehicles`, Major → `not_passable_all_vehicles`
5. A vehicle floating or swept away, or boats carrying people → `not_passable_all_vehicles`
6. Otherwise (a flooded road with nothing on it to judge depth) → `not_assessable`

| Value | MMDA category | Value for calculations |
|---|---|---|
| `passable_all_vehicles` | Passable to all types of vehicles (PATV) | 0 |
| `not_passable_light_vehicles` | Not passable to light vehicles (NPLV) | 1 |
| `not_passable_all_vehicles` | Not passable to all types of vehicles (NPATV) | 2 |
| `no_road_visible` | – | none |
| `not_assessable` | – | none |

**Basis:** the categories are MMDA's own (2024). Road closure from submerged or blocked roads follows Zhu et al. (2024); judging passability from submerged small vehicles follows Xue et al. (2026).

## 6. visual_description

One or two sentences, at most 40 words, describing only what is visible: the scene, where the water is, how high it reaches on people, vehicles or buildings, and visible actions such as wading or rescue. It never contains place names (unless printed on a visible sign), dates, causes, injuries, feelings, exact counts beyond "one", "two" or "several", or anything from the caption.

**Basis:** vision-language models tend to mention objects that usually appear together, such as cars or people in a flood scene, even when they are absent (Y. Li et al., 2023), so raters check each claim. Reasoning that contradicts the image counted as wrong in flood-image question answering (Sun et al., 2023). Generated descriptions did not beat image-only classification (Yang & Zou, 2025), so the description supports the labels but is not evidence on its own.

## Rules every record satisfies

The script's rules always produce records that follow these, and the script refuses to write any record that breaks them:

- `is_valid_flood_evidence` is `false` → `flood_presence` is `null`, `visual_severity` is `not_assessable`, `infrastructure_accessibility` is `no_road_visible`.
- `is_valid_flood_evidence` is `true` → `flood_presence` is `true` or `false`.
- `flood_presence` is `false` → `visual_severity` is `no_flood`, and the road is passable, blocked or not visible.
- `flood_presence` is `true` → `visual_severity` is `minor`, `moderate`, `major` or `not_assessable`.
- `is_valid_flood_evidence` is `true` → `post_visual_severity` is the same as or higher than `visual_severity`, in the order of section 4a.

## Evidence behind each record

The seven parameters are what fusion reads. The audit file `data/image_classified/_metadata/<output name>_evidence.jsonl` keeps the evidence for each image: Opus's anchors, impact cue and road observations, a short `depth_anchor_evidence` sentence such as "Adult: water at the knee.", and a `decision_basis` naming the rule that set each label. Keep it: you need the observations to score Opus against the human gold labels, and to show a panel why any image received its level.

## Sources

- Akinboyewa, T., Ning, H., Lessani, M. N., & Li, Z. (2024). Automated floodwater depth estimation using large multimodal model for rapid flood mapping. *Computational Urban Science*, 4, 12.
- Alam, F., Alam, T., Hasan, M. A., Hasnat, A., Imran, M., & Ofli, F. (2023). MEDIC: A multi-task learning dataset for disaster image classification. *Neural Computing and Applications*, 35(3), 2609–2632.
- Du, Z., Qiu, T., Zhou, S., & Lyu, H. (2026). Mask-guided object detection for vehicle-based urban floodwater-level recognition from crowdsourced street-level images. *Water*, 18(15), 1845.
- Franchi, M., Garg, N., Ju, W., & Pierson, E. (2025). Bayesian modeling of zero-shot classifications for urban flood detection. arXiv 2503.14754.
- Fuad, N., & Qian, X. (2026). LLM-powered flood depth estimation from social media imagery. arXiv 2603.17108.
- Li, J., Cai, R., Tan, Y., Zhou, H., Sadick, A.-M., Shou, W., & Wang, X. (2023). Automatic detection of actual water depth of urban floods from social media images. *Measurement*, 216, 112891.
- Li, Y., Du, Y., Zhou, K., Wang, J., Zhao, W. X., & Wen, J.-R. (2023). Evaluating object hallucination in large vision-language models. *EMNLP 2023*.
- McDaniel, E. L., Scheele, S., & Liu, J. (2024). Zero-shot classification of crisis tweets using instruction-finetuned large language models. arXiv 2410.00182.
- Metro Manila Development Authority (2024). MMDA Flood Gauge System, as reported by Interaksyon (4 September 2024) and Taguig.com. Replace with MMDA's own release before the defense.
- Schwarz, F., et al. (2026). Zero-shot social media crisis classification: A training-free multimodal approach. *Applied Sciences*, 16(5), 2192.
- Sun, Y., Wang, C., & Peng, Y. (2023). Unleashing the potential of large language model: Zero-shot VQA for flood disaster scenario. arXiv 2312.01882.
- Xue, F., Yang, Y., Chen, X., Lü, X., & Cheng, Y. (2026). Constructing an urban waterlogging depth grade assessment model using images captured by the public. *Geomatics and Information Science of Wuhan University*, 51(1), 46–54.
- Yang, Y., & Zou, L. (2025). Perceiving multidimensional disaster damages from street-view images using visual-language models. *Abstracts of the ICA*, 10, 310.
- Zhang, Y., Sun, Y., Zhou, X., Zhang, Y., Yang, H., & Zhang, K. (2026). Urban flood-depth estimation from crowdsourced image–text data using reference-object reasoning and conditional fusion. *ISPRS International Journal of Geo-Information*, 15(9), 430.
- Zhu, H., Meng, J., Yao, J., & Xu, N. (2024). Feasibility of emergency flood traffic road damage assessment by integrating remote sensing images and social media information. *ISPRS International Journal of Geo-Information*, 13(10), 369.
