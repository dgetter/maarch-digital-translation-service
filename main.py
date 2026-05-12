import logging
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from src.config import form_id_var, settings
from src.glossary import create_entry, delete_entry, get_all_entries, get_all_kinds, update_entry
from src.quota_router import QuotaExhaustedError
from src.models import (
    GlossaryCreateRequest,
    GlossaryUpdateRequest,
    TranslateRequest,
    TranslationMetadata,
)
from src.translator import process_translation_request


class _FormIDFilter(logging.Filter):
    """Injects the current request's formID into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        form_id = form_id_var.get("")
        record.form_id = f"[{form_id}] " if form_id else ""
        return True


logging.basicConfig(
    level=getattr(logging, settings.log_level),
    format="%(asctime)s - %(form_id)s%(name)s - %(levelname)s - %(message)s",
)
_form_id_filter = _FormIDFilter()
for _handler in logging.getLogger().handlers:
    _handler.addFilter(_form_id_filter)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Translation Service",
    description="API for translating JSON fields to Hebrew using Google Gemini (Vertex AI) with LLM-as-Judge quality evaluation.",
    version="1.0.0",
    openapi_tags=[
        {"name": "Translation", "description": "Translate JSON fields to Hebrew"},
        {"name": "Glossary", "description": "Manage translation glossary entries"},
        {"name": "System", "description": "Health checks and system status"},
    ],
)

TEMPLATE_DIR = Path(__file__).parent / "templates"

# Mount static files (for SVG logo and other assets)
app.mount("/static", StaticFiles(directory=str(TEMPLATE_DIR)), name="static")


# ── Dashboard ────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard():
    with open(TEMPLATE_DIR / "index.html", "r", encoding="utf-8") as f:
        return f.read()


# ── Translation ──────────────────────────────────────────────────

TRANSLATE_RESPONSE_EXAMPLE = {
    "fields_to_translate": [
        {"field_name": "subject_en", "translation_instructions": "default"},
        {"field_name": "previouslySent", "translation_instructions": "exact"},
        {"field_name": "reason_fr", "translation_instructions": "default"},
        {"field_name": "job_title_ar", "translation_instructions": "exact"},
        {"field_name": "notes_es", "translation_instructions": "default"},
        {"field_name": "remarks_he", "translation_instructions": "default"},
        {"field_name": "nonexistent_field", "translation_instructions": "default"},
    ],
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
                    "overall": 9.5,
                },
                "translation_status": "success",
                "error": "",
            },
            {
                "field_name": "previouslySent",
                "language_detected": "ru",
                "instruction": "exact",
                "translation": "לא",
                "raw_content": "нет",
                "scores": {
                    "faithfulness": 10.0,
                    "fluency": 10.0,
                    "glossary_compliance": 10.0,
                    "overall": 10.0,
                },
                "translation_status": "success",
                "error": "",
            },
            {
                "field_name": "reason_fr",
                "language_detected": "fr",
                "instruction": "default",
                "translation": "הדרכון שלי פג תוקף ואני צריך לחדש אותו לפני הנסיעה שלי.",
                "raw_content": "Mon passeport a expiré et j'ai besoin de le renouveler avant mon voyage.",
                "scores": {
                    "faithfulness": 9.0,
                    "fluency": 9.5,
                    "glossary_compliance": 10.0,
                    "overall": 9.3,
                },
                "translation_status": "success",
                "error": "",
            },
            {
                "field_name": "job_title_ar",
                "language_detected": "ar",
                "instruction": "exact",
                "translation": "מהנדס תוכנה",
                "raw_content": "مهندس برمجيات",
                "scores": {
                    "faithfulness": 10.0,
                    "fluency": 10.0,
                    "glossary_compliance": 10.0,
                    "overall": 10.0,
                },
                "translation_status": "success",
                "error": "",
            },
            {
                "field_name": "notes_es",
                "language_detected": "es",
                "instruction": "default",
                "translation": "אני אם חד-הורית עם שני ילדים. אני מבקשת את ההנחה המרבית המותרת על פי חוק.",
                "raw_content": "Soy madre soltera con dos hijos. Solicito la reducción máxima permitida por ley.",
                "scores": {
                    "faithfulness": 9.0,
                    "fluency": 9.0,
                    "glossary_compliance": 10.0,
                    "overall": 9.2,
                },
                "translation_status": "success",
                "error": "",
            },
            {
                "field_name": "remarks_he",
                "language_detected": "he",
                "instruction": "default",
                "translation": "קיבלתי הודעה על דחיית הבקשה ואני מגיש ערר.",
                "raw_content": "קיבלתי הודעה על דחיית הבקשה ואני מגיש ערר.",
                "scores": None,
                "translation_status": "skipped",
                "error": "",
            },
            {
                "field_name": "nonexistent_field",
                "language_detected": "",
                "instruction": "default",
                "translation": "",
                "raw_content": "",
                "scores": None,
                "translation_status": "skipped",
                "error": "Field not found in data",
            },
        ]
    },
}


@app.post(
    "/api/translate",
    tags=["Translation"],
    summary="Translate fields to Hebrew",
    description=(
        "Translates specified fields from any supported language into Hebrew.\n\n"
        "**Request structure:**\n"
        "- `fields_to_translate` — array listing each field name and its translation strategy (`default` or `exact`).\n"
        "- All remaining top-level keys are treated as source data. The service searches recursively through "
        "the entire data payload to locate each requested field.\n\n"
        "**Field value formats:**\n"
        "- **Object with `dataText`:** If a field's value is an object containing a `dataText` key "
        "(e.g. `{\"dataCode\": \"1\", \"dataText\": \"Hello\"}`), the `dataText` value is translated.\n"
        "- **Plain string:** If the value is a string, it is translated directly.\n\n"
        "**Translation strategies:**\n"
        "- `default` — natural, fluent Hebrew translation.\n"
        "- `exact` — literal/verbatim translation preserving the source structure.\n\n"
        "**Response structure:**\n"
        "- `fields_to_translate` — echoed back from the request.\n"
        "- `translation_metadata.fields_translated` — one entry per requested field with: "
        "detected language, translated text, original text, quality scores, and status.\n\n"
        "**Statuses:**\n"
        "- `success` — field found, translated, and scored.\n"
        "- `skipped` — field already in Hebrew (no translation needed) or field not found in data.\n"
        "- `failed` — translation attempted but encountered an error.\n\n"
        "**Quality scores** (per field, when status is `success`):\n"
        "- `faithfulness` — how accurately the translation preserves the original meaning (0–10).\n"
        "- `fluency` — how natural the Hebrew reads (0–10).\n"
        "- `glossary_compliance` — how well glossary terms were applied (0–10).\n"
        "- `overall` — weighted overall quality score (0–10)."
    ),
    responses={
        200: {
            "description": "Translation completed successfully. Each requested field appears in `translation_metadata.fields_translated` with its translation, scores, and status.",
            "content": {
                "application/json": {
                    "example": TRANSLATE_RESPONSE_EXAMPLE,
                }
            },
        },
        500: {
            "description": "Translation processing failed due to an internal error.",
            "content": {
                "application/json": {
                    "example": {"detail": "Translation failed: <error details>"}
                }
            },
        },
    },
)
async def translate(request: TranslateRequest):
    data = request.model_dump()
    fields_to_translate = data.pop("fields_to_translate")
    form_id = data.pop("formID")
    kind = data.pop("kind", None)
    form_id_var.set(form_id)

    logger.info("Starting translation request with %d field(s), kind=%s", len(fields_to_translate), kind)
    _start = time.monotonic()

    try:
        result, fields_metadata = await process_translation_request(
            data, fields_to_translate, kind=kind
        )
    except QuotaExhaustedError as e:
        logger.warning("Translation failed — all Vertex AI quota exhausted in %.2fs: %s", time.monotonic() - _start, e)
        raise HTTPException(status_code=429, detail=str(e))
    except Exception as e:
        logger.error("Translation processing failed in %.2fs: %s", time.monotonic() - _start, e)
        raise HTTPException(status_code=500, detail=f"Translation failed: {e}")

    logger.info("Translation request finished in %.2fs", time.monotonic() - _start)

    return {
        "fields_to_translate": fields_to_translate,
        "translation_metadata": TranslationMetadata(
            fields_translated=fields_metadata
        ).model_dump(),
    }


# ── Glossary Auth ────────────────────────────────────────────────

@app.post(
    "/api/glossary/auth",
    tags=["Glossary"],
    summary="Verify glossary admin password",
    description="Server-side password verification for glossary management access.",
    include_in_schema=False,
)
async def glossary_auth(body: dict):
    if body.get("password") == settings.glossary_admin_password:
        return {"authenticated": True}
    raise HTTPException(status_code=401, detail="Invalid password")


# ── Glossary CRUD ────────────────────────────────────────────────

@app.get(
    "/api/glossary",
    tags=["Glossary"],
    summary="List all glossary entries",
    description="Returns all glossary entries and their count. Optionally filter by kind.",
)
async def list_glossary(kind: str | None = None):
    entries = await get_all_entries(kind=kind)
    return {"entries": [e.model_dump() for e in entries], "count": len(entries)}


@app.get(
    "/api/glossary/kinds",
    tags=["Glossary"],
    summary="List distinct glossary kinds",
    description="Returns a sorted list of all distinct 'kind' values currently in the glossary.",
)
async def list_glossary_kinds():
    kinds = await get_all_kinds()
    return {"kinds": kinds}


@app.post(
    "/api/glossary",
    tags=["Glossary"],
    summary="Create a glossary entry",
    description="Add a new term-to-Hebrew mapping. The glossary is used during translation to ensure consistent terminology.",
)
async def add_glossary_entry(request: GlossaryCreateRequest):
    entry = await create_entry(
        term=request.term,
        hebrew=request.hebrew,
        explanation=request.explanation,
        kind=request.kind,
    )
    logger.info("Glossary entry created: id=%s, term=%r, kind=%s", entry.id, entry.term, entry.kind)
    return entry.model_dump()


@app.put(
    "/api/glossary/{entry_id}",
    tags=["Glossary"],
    summary="Update a glossary entry",
    description="Update one or more fields of an existing glossary entry by its ID.",
)
async def edit_glossary_entry(entry_id: str, request: GlossaryUpdateRequest):
    entry = await update_entry(
        entry_id=entry_id,
        term=request.term,
        hebrew=request.hebrew,
        explanation=request.explanation,
        kind=request.kind,
    )
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")
    logger.info("Glossary entry updated: id=%s", entry_id)
    return entry.model_dump()


@app.delete(
    "/api/glossary/{entry_id}",
    tags=["Glossary"],
    summary="Delete a glossary entry",
    description="Remove a glossary entry by its ID.",
)
async def remove_glossary_entry(entry_id: str):
    if not await delete_entry(entry_id):
        raise HTTPException(status_code=404, detail="Entry not found")
    logger.info("Glossary entry deleted: id=%s", entry_id)
    return {"status": "deleted", "id": entry_id}


# ── Health ───────────────────────────────────────────────────────

@app.get(
    "/health",
    tags=["System"],
    summary="Health check",
    description="Returns service health status.",
)
async def health():
    return {"status": "ok"}


@app.get(
    "/health/deep",
    tags=["System"],
    summary="Deep health check",
    description=(
        "Probes all external connections: Firestore (glossary database) and each "
        "configured Vertex AI LLM endpoint. Returns per-component status and latency. "
        "HTTP 200 for healthy/degraded, HTTP 503 for unhealthy."
    ),
)
async def deep_health():
    from src.health import run_deep_health_check
    result = await run_deep_health_check()
    status_code = 503 if result["status"] == "unhealthy" else 200
    return JSONResponse(content=result, status_code=status_code)


@app.on_event("startup")
async def startup_health_check():
    """Log connectivity status for all components on startup."""
    from src.health import run_deep_health_check
    logger.info("Running startup connectivity check...")
    result = await run_deep_health_check()
    fs = result["checks"]["firestore"]
    logger.info(
        "Firestore [%s] project=%s database=%s%s",
        fs["status"], fs["project"], fs["database"],
        f" latency={fs['latency_ms']}ms" if fs["status"] == "ok" else f" error={fs['error']}",
    )
    for ep in result["checks"]["llm_endpoints"]:
        logger.info(
            "Vertex AI [%s] project=%s location=%s%s",
            ep["status"], ep["project"], ep["location"],
            f" latency={ep['latency_ms']}ms" if ep["status"] == "ok" else f" error={ep['error']}",
        )
    logger.info("Startup connectivity check complete — overall status: %s", result["status"])
