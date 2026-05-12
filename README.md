# Translation Service

Automatic Hebrew translation service using Google Gemini (Vertex AI) with LLM-as-Judge quality evaluation.

## Use Case & Purpose

Built for **the Digital Division** to translate digital forms filled out by users in multiple languages.

**Supported Source Languages**: English, French, Spanish, Arabic, Russian, Hebrew
**Target Language**: Always Hebrew

## What the System Does

- Accepts JSON with text fields and translates them to Hebrew
- Automatically detects source language per field
- Recursively searches nested JSON structures to locate fields
- Two translation modes (`default` / `exact`) configurable per field
- Evaluates translation quality across 4 dimensions (faithfulness, fluency, glossary_compliance, overall)
- Custom terminology glossary stored in Firestore and injected into prompts
- Built-in HTML dashboard at `http://localhost:8000/`

---

## Installation & Running

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Configure environment variables (see Configuration section)

# Import glossary into Firestore (one-time setup)
python scripts/import_glossary.py

uvicorn main:app --reload
```

Dashboard: `http://localhost:8000/`
Interactive API docs: `http://localhost:8000/docs`

---

## Configuration

### Environment Variables (`.env`)

```
GOOGLE_CLOUD_PROJECT=your-project-id
GOOGLE_CLOUD_LOCATION=us-central1
VERTEX_AI_ENDPOINTS=project-a:us-central1,project-b:europe-west4
GEMINI_MODEL=gemini-2.5-flash
LOG_LEVEL=INFO
LOG_TO_FILE=False
LLM_MAX_RETRIES=3
LLM_RETRY_DELAY_SECONDS=2.0
EVAL=false
GLOSSARY_ADMIN_PASSWORD=change-me
FIRESTORE_DATABASE=glossarydb
FIRESTORE_COLLECTION=glossary_entries
```

### GCP Project Layout

Two separate concerns, two separate project configurations:

| Concern | Config var | Description |
|---------|-----------|-------------|
| **Glossary (Firestore)** | `GOOGLE_CLOUD_PROJECT` | Single fixed project that hosts the Firestore glossary database. Never rotated. |
| **LLM calls (Vertex AI)** | `VERTEX_AI_ENDPOINTS` | One or more `project:location` pairs. The quota router distributes LLM requests across these projects. |

**`VERTEX_AI_ENDPOINTS`** accepts a comma-separated list of `project:location` pairs:
```
VERTEX_AI_ENDPOINTS=proj-a:us-central1,proj-b:europe-west4,proj-c:us-central1
```
Each entry is an independent GCP project with its own Vertex AI quota. If this variable is not set, the service falls back to `GOOGLE_CLOUD_PROJECT` + `GOOGLE_CLOUD_LOCATION` as a single endpoint.

**Authentication**: On Cloud Run, credentials are handled automatically via ADC (Application Default Credentials). The Cloud Run service account must be granted `roles/aiplatform.user` in every project listed in `VERTEX_AI_ENDPOINTS`, and `roles/datastore.user` in the `GOOGLE_CLOUD_PROJECT` that hosts Firestore.

### Quota Router Behaviour

Every incoming translation request is assigned the next Vertex AI project in round-robin sequence (distributing load across all configured projects). If that project returns a quota error (`429` / `ResourceExhausted`), the request automatically falls back to the next project in the list — transparently, without returning an error to the caller. If every project is quota-exhausted the service returns HTTP `429`.

The Firestore glossary is always read from `GOOGLE_CLOUD_PROJECT` and is never part of the rotation.

---

## API Reference

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | HTML Dashboard |
| `POST` | `/api/translate` | Translate JSON fields to Hebrew |
| `GET` | `/api/glossary` | List glossary entries (optional `?kind=` filter) |
| `GET` | `/api/glossary/kinds` | List distinct glossary kind values |
| `POST` | `/api/glossary` | Add glossary entry |
| `PUT` | `/api/glossary/{id}` | Update glossary entry |
| `DELETE` | `/api/glossary/{id}` | Delete glossary entry |
| `GET` | `/health` | Health check |
| `GET` | `/health/deep` | Deep health check (probes Firestore + all Vertex AI endpoints) |

---

## Translation API

### `POST /api/translate`

Translates specified fields from any supported language to Hebrew.

#### Request Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `formID` | string | Yes | Unique form identifier used for tracing and logging |
| `fields_to_translate` | array | Yes | List of fields to translate (see below) |
| `kind` | string | No | Form kind (e.g. `"consular"`, `"immigration"`). Controls which glossary entries are applied — `general` entries always apply; kind-specific entries apply only when `kind` matches |
| *(any other keys)* | any | — | Source data. The service recursively searches the entire payload to locate each requested field |

#### `fields_to_translate` item

| Field | Type | Values | Description |
|-------|------|--------|-------------|
| `field_name` | string | — | Name of the JSON field to find and translate |
| `translation_instructions` | string | `default` \| `exact` | Translation strategy (see below) |

#### Translation Strategies

**`default`** — Natural, fluent translation
Use for free-text fields: descriptions, comments, explanatory text.
> `"Mon passeport a expiré"` → `"הדרכון שלי פג תוקף"`

**`exact`** — Literal / verbatim translation
Use for structured fields: names, places, dropdown values, document types.
- Proper nouns → phonetic transliteration: `"London"` → `"לונדון"`, `"John"` → `"ג'ון"`
- Common terms → literal meaning: `"no"` → `"לא"`, `"identity document"` → `"תעודת זהות"`

#### Field Value Formats

The service handles two field value formats:

- **Plain string** — the string is translated directly.
- **Object with `dataText`** — only the `dataText` value is translated; the rest of the object (`dataCode`, etc.) is preserved.

```json
"previouslySent": { "dataCode": "2", "dataText": "нет" }
```
→ translates `"нет"`, returns `"לא"` in the translation metadata.

#### Request Example

```json
{
  "formID": "consular@example.gov.il",
  "kind": "consular",
  "fields_to_translate": [
    { "field_name": "subject_en",    "translation_instructions": "default" },
    { "field_name": "previouslySent","translation_instructions": "exact" },
    { "field_name": "reason_fr",     "translation_instructions": "default" },
    { "field_name": "job_title_ar",  "translation_instructions": "exact" }
  ],
  "data": {
    "entry": {
      "subject_en":    { "dataCode": "1", "dataText": "Request for consular appointment" },
      "previouslySent":{ "dataCode": "2", "dataText": "нет" },
      "reason_fr":     { "dataCode": "3", "dataText": "Mon passeport a expiré et j'ai besoin de le renouveler avant mon voyage." }
    },
    "mahutHafnaya": {
      "tblMahutHafnayaList": [
        {
          "job_title_ar": { "dataCode": "5", "dataText": "مهندس برمجيات" }
        }
      ]
    }
  }
}
```

#### Response Structure

The response echoes `fields_to_translate` and appends `translation_metadata`:

```json
{
  "fields_to_translate": [...],
  "translation_metadata": {
    "fields_translated": [
      {
        "field_name": "subject_en",
        "language_detected": "en",
        "instruction": "default",
        "translation": "בקשה לקביעת תור קונסולרי",
        "raw_content": "Request for consular appointment",
        "scores": {
          "faithfulness": 9.5,
          "fluency": 9.0,
          "glossary_compliance": 10.0,
          "overall": 9.5
        },
        "translation_status": "success",
        "error": ""
      },
      {
        "field_name": "previouslySent",
        "language_detected": "ru",
        "instruction": "exact",
        "translation": "לא",
        "raw_content": "нет",
        "scores": { "faithfulness": 10.0, "fluency": 10.0, "glossary_compliance": 10.0, "overall": 10.0 },
        "translation_status": "success",
        "error": ""
      }
    ]
  }
}
```

#### Response Field Reference

| Field | Description |
|-------|-------------|
| `field_name` | Name of the requested field |
| `language_detected` | ISO 639-1 code of detected source language (`en`, `fr`, `es`, `ar`, `ru`) |
| `instruction` | Strategy used (`default` or `exact`) |
| `translation` | Translated Hebrew text |
| `raw_content` | Original source text that was translated |
| `scores` | Quality scores object (see below), or `null` if skipped |
| `translation_status` | `success` / `skipped` / `failed` |
| `error` | Error message, or empty string on success |

#### Translation Statuses

| Status | Meaning |
|--------|---------|
| `success` | Field found, translated, and quality-scored |
| `skipped` | Field already in Hebrew (no translation needed), or field not found in data |
| `failed` | Translation was attempted but encountered an error |

#### Quality Scores (0–10)

| Score | Description |
|-------|-------------|
| `faithfulness` | How accurately the translation preserves the original meaning |
| `fluency` | How natural and grammatically correct the Hebrew reads |
| `glossary_compliance` | How well required glossary terms were applied |
| `overall` | Weighted overall quality score |

Scores are `null` when `translation_status` is `skipped`.

---

## Glossary API

The glossary maps source terms to their required Hebrew translations. It is injected into every translation prompt to ensure consistent terminology. The glossary is stored in Firestore and cached in-memory for 5 minutes.

### Glossary Entry Fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Unique entry identifier (auto-generated UUID) |
| `term` | string | Source term. Can contain multilingual variants separated by ` \| ` (e.g. `"Permanent resident \| Постоянный житель \| مقيم دائم"`) |
| `hebrew` | string | Required Hebrew translation |
| `explanation` | string | Optional context or usage notes |
| `kind` | string | Scope: `"general"` applies to all forms; any other value (e.g. `"consular"`) applies only when the translation request specifies that `kind` |

### How `kind` Scoping Works

- `kind = "general"` → entry is included in **all** translation requests
- `kind = "consular"` → entry is included only when the translation request has `"kind": "consular"`
- A request with `"kind": "consular"` receives both `general` entries and `consular` entries
- A request without `kind` (or `kind: null`) receives only `general` entries

### `GET /api/glossary`

List all glossary entries. Optionally filter by kind.

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `kind` | string | Optional. Filter entries by kind (e.g. `?kind=consular`) |

**Response:**
```json
{
  "entries": [
    {
      "id": "uuid-string",
      "term": "Permanent resident | Постоянный житель",
      "hebrew": "תושב קבע",
      "explanation": "Legal residency status",
      "kind": "general"
    }
  ],
  "count": 1
}
```

### `POST /api/glossary`

Add a new glossary entry.

**Request Body:**
```json
{
  "term": "software engineer | مهندس برمجيات",
  "hebrew": "מהנדס תוכנה",
  "explanation": "Job title",
  "kind": "general"
}
```

**Response:** The created entry including its generated `id`.

### `PUT /api/glossary/{id}`

Update an existing entry. All fields are optional — only provided fields are updated.

**Request Body (all optional):**
```json
{
  "term": "updated term",
  "hebrew": "עברית מעודכנת",
  "explanation": "updated explanation",
  "kind": "consular"
}
```

**Response:** The updated entry.

### `DELETE /api/glossary/{id}`

Delete a glossary entry by ID.

**Response:**
```json
{ "status": "deleted", "id": "uuid-string" }
```

Returns `404` if the entry does not exist.

### Importing the Pre-populated Glossary

The repository includes `data/glossary.json` with 600+ pre-built terms.

```bash
python scripts/import_glossary.py                # Import all entries
python scripts/import_glossary.py --skip-existing  # Skip entries already in Firestore
```

### Multilingual Term Variants

A single glossary entry can cover the same concept in multiple languages by separating variants with ` | `:

```json
{
  "term": "Permanent resident | Постоянный житель | مقيم دائم | Résident permanent",
  "hebrew": "תושב קבע",
  "kind": "general"
}
```

The LLM will apply the Hebrew term whenever it encounters **any** of the listed variants in the source text, regardless of language.

---

## Project Structure

```
main.py              — FastAPI app, routes
src/
├── config.py        — Settings from .env
├── models.py        — Pydantic request/response schemas
├── translator.py    — Language detection, batch translation
├── evaluator.py     — Batch quality evaluation (LLM-as-Judge)
├── quota_router.py  — Round-robin Vertex AI project router with quota failover
├── prompts.py       — Prompt templates
├── glossary.py      — Glossary management (Firestore + cache)
├── field_utils.py   — Recursive field resolution utilities
├── health.py        — Deep health checks (Firestore + Vertex AI endpoints)
├── retry.py         — Retry logic for transient LLM failures
├── cost_logger.py   — Per-request cost tracking and logging
templates/
└── index.html       — HTML dashboard
data/
└── glossary.json    — Pre-populated glossary with 600+ terms
scripts/
├── import_glossary.py   — Import glossary.json entries into Firestore
└── migrate_glossary.py  — Glossary migration utilities
```

## Tech Stack

Python 3.11+, FastAPI, Google Gen AI SDK (Vertex AI), Google Cloud Firestore, langdetect, pydantic-settings
