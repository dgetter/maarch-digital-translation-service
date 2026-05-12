# Translation Service Flow

## Overview
The translation service processes JSON fields through a batch pipeline that includes language detection, translation, and quality evaluation using Google Gemini (Vertex AI).

## LLM Call Count

**For N fields to translate:**
- **Minimum:** 0 LLM calls (if all fields are already Hebrew or empty)
- **Maximum:** 3 LLM calls (regardless of N):
  - 1 batch translation call for all `default` fields
  - 1 batch translation call for all `exact` fields
  - 1 batch evaluation call for all successfully translated fields (only when `EVAL=true`)

If all fields share the same instruction type, translation uses at most 1 LLM call. Evaluation is disabled by default (`EVAL=false`).

## Detailed Flow

### Phase 1: Resolve Fields & Pre-filter
**File:** `src/translator.py` — `process_translation_request()`

For each field in `fields_to_translate`:

1. **Locate field** in the nested JSON structure (root, nested path, or recursive search via `src/field_utils.py`)
2. **Extract text** — plain string or `dataText` value from `{dataCode, dataText}` objects
3. **Detect language** via Unicode script analysis (zero LLM calls):
   - Hebrew script (U+0590–U+05FF) → mark as `skipped` (already Hebrew)
   - Arabic script (U+0600–U+06FF) → `'ar'`
   - Cyrillic script (U+0400–U+04FF) → `'ru'`
   - Latin / mixed → `None` (LLM will detect during translation)
4. **Sort into batch buckets** by instruction type (`default_fields` or `exact_fields`)

Fields that are missing, empty, or already Hebrew are recorded in metadata immediately and excluded from further processing.

### Phase 2: Batch Translate
**File:** `src/translator.py` — `translate_fields_batch()`

All queued fields are translated in at most **2 LLM calls** — one per instruction type:

| Instruction | Prompt | Description |
|-------------|--------|-------------|
| `default` | `SYSTEM_PROMPT_BATCH_DEFAULT` | Natural, fluent Hebrew translation |
| `exact` | `SYSTEM_PROMPT_BATCH_EXACT` | Literal translation / phonetic transliteration |

Each batch call:
- Builds a numbered list of all fields as a single user prompt
- Appends relevant glossary entries (filtered by `kind`) to the system prompt
- Requests structured JSON output (one translation + detected language per field)
- Uses the quota router for project rotation and failover
- Uses retry logic for transient errors (`src/retry.py`)

```python
response = await client.aio.models.generate_content(
    model=settings.gemini_model,
    contents=user_prompt,
    config=types.GenerateContentConfig(
        system_instruction=system_prompt,
        response_mime_type="application/json",
        response_json_schema=schema,
        temperature=0.1,
    ),
)
```

**Response format:**
```json
{
  "translations": [
    { "field_name": "subject_en", "detected_language": "en", "translation": "בקשה לקביעת תור" },
    { "field_name": "reason_fr", "detected_language": "fr", "translation": "הדרכון שלי פג תוקף" }
  ]
}
```

### Phase 3: Write Translations Back

For each successfully translated field, the translated text is written back into the nested data structure at the original field path. Fields with failed or empty translations retain their original values.

### Phase 4: Batch Evaluate (Optional)
**File:** `src/evaluator.py` — `evaluate_translations_batch()`

Only runs when `EVAL=true` (disabled by default). All successfully translated fields are evaluated in a **single LLM call**.

```python
response = await client.aio.models.generate_content(
    model=settings.gemini_model,
    contents=user_prompt,
    config=types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT_BATCH_EVALUATE,
        response_mime_type="application/json",
        response_json_schema=schema,
        temperature=0.7,
    ),
)
```

**Evaluation criteria (1–10 scale):**
- **faithfulness (60% weight):** Exact meaning preservation, no additions/omissions
- **fluency (20% weight):** Natural Hebrew readability
- **glossary_compliance (20% weight):** Correct use of glossary terms
- **overall:** Weighted average (recomputed server-side for consistency)

### Phase 5: Assemble Metadata

Each field gets a metadata entry with: detected language, translation, original text, quality scores (or `null`), status, and any error messages.

### Phase 6: Log Costs

Token usage from all LLM calls is aggregated and logged. If `LOG_TO_FILE=true`, a row is appended to `costs_log.md`.

## Flow Diagram

```
┌─────────────────────────────────────┐
│ POST /api/translate                 │
│ fields_to_translate: [field1, ...]  │
└──────────────┬──────────────────────┘
               │
               ▼
┌──────────────────────────────────────────┐
│ Phase 1: Resolve & Pre-filter            │
│ For each field:                          │
│  • Locate in nested JSON                 │
│  • Extract text (plain string / dataText)│
│  • Script detection (0 LLM calls)        │
│  • Skip if Hebrew / empty / missing      │
│  • Sort into default[] or exact[]        │
└──────────────┬───────────────────────────┘
               │
        ┌──────┴──────┐
        ▼             ▼
 ┌─────────────┐ ┌─────────────┐
 │ Batch LLM   │ │ Batch LLM   │
 │ translate    │ │ translate    │
 │ (default)   │ │ (exact)      │
 │ 1 call      │ │ 1 call       │
 └──────┬──────┘ └──────┬──────┘
        └──────┬────────┘
               ▼
┌──────────────────────────────────┐
│ Phase 3: Write translations back │
└──────────────┬───────────────────┘
               │
               ▼
┌──────────────────────────────────┐
│ Phase 4: Batch evaluate          │
│ (1 LLM call, only if EVAL=true) │
└──────────────┬───────────────────┘
               │
               ▼
┌──────────────────────────────────┐
│ Phase 5: Assemble metadata       │
│ Phase 6: Log costs               │
└──────────────┬───────────────────┘
               │
               ▼
┌──────────────────────────────┐
│ Return JSON with             │
│ translations & quality scores│
└──────────────────────────────┘
```

## Example: 3 Fields Request

**Request:**
```json
{
  "fields_to_translate": [
    {"field_name": "title", "translation_instructions": "default"},
    {"field_name": "description", "translation_instructions": "exact"},
    {"field_name": "existing_hebrew", "translation_instructions": "default"}
  ],
  "title": "Welcome",
  "description": "User profile",
  "existing_hebrew": "שלום"
}
```

**LLM Call Breakdown:**
1. `existing_hebrew` detected as Hebrew → **skipped** (0 calls)
2. `title` (default) batched with other default fields → **1 batch translation call**
3. `description` (exact) batched with other exact fields → **1 batch translation call**
4. Evaluation (if `EVAL=true`) → **1 batch evaluation call** for `title` + `description`

**Total: 2 LLM calls** (EVAL=false) or **3 LLM calls** (EVAL=true) for 3 fields

## Key Optimizations

1. **Batch processing:** All fields of the same instruction type are translated in a single LLM call, regardless of count
2. **Script-based pre-filtering:** Hebrew detection via Unicode avoids unnecessary translation calls
3. **Lazy LLM detection:** Language detection is deferred to the translation LLM when script analysis is inconclusive (Latin/mixed scripts)
4. **Conditional evaluation:** Quality scoring is off by default (`EVAL=false`) and runs as a single batch call when enabled

## Configuration

**Model:** Configurable via `GEMINI_MODEL` (default: `gemini-2.5-flash`)
**Temperature:**
- Translation: 0.1 (deterministic)
- Evaluation: 0.7 (allows realistic score distribution)

**Settings file:** `src/config.py`
