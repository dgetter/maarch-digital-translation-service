import json
import logging

from google.genai import types

from src.config import form_id_var, settings
from src.prompts import SYSTEM_PROMPT_BATCH_DEFAULT, SYSTEM_PROMPT_BATCH_EXACT
from src.evaluator import evaluate_translations_batch
from src.quota_router import quota_router
from src.retry import with_retries
from src.models import (
    TRANSLATION_STATUS_SUCCESS,
    TRANSLATION_STATUS_FAILED,
    TRANSLATION_STATUS_SKIPPED,
    build_batch_translation_schema,
)

logger = logging.getLogger(__name__)

SUPPORTED_LANGUAGES = {"he", "en", "fr", "es", "ar", "ru"}
LANG_NAMES = {
    "he": "Hebrew",
    "en": "English",
    "fr": "French",
    "es": "Spanish",
    "ar": "Arabic",
    "ru": "Russian",
}

# Script-based language hints used to guard against LLM misdetection on short inputs.
# These are conservative: they only fire when the text is DOMINATED by a specific script.
# Latin-script languages (en/fr/es) are intentionally left to the LLM — they share a script.
_SCRIPT_HINTS = [
    # (unicode_range_start, unicode_range_end, language_code, script_name)
    ('\u0590', '\u05FF', 'he', 'Hebrew'),     # Hebrew block
    ('\uFB1D', '\uFB4F', 'he', 'Hebrew'),     # Hebrew presentation forms
    ('\u0600', '\u06FF', 'ar', 'Arabic'),     # Arabic block
    ('\u0750', '\u077F', 'ar', 'Arabic'),     # Arabic supplement
    ('\uFE70', '\uFEFF', 'ar', 'Arabic'),     # Arabic presentation forms
    ('\u0400', '\u04FF', 'ru', 'Cyrillic'),   # Cyrillic block (covers RU, UK, BG, etc.)
    ('\u0500', '\u052F', 'ru', 'Cyrillic'),   # Cyrillic supplement
]

# Minimum ratio of script characters required to trigger a script-based decision.
# 0.5 means more than half the non-whitespace characters must be in the script.
_SCRIPT_DOMINANCE_THRESHOLD = 0.5


def _dominant_script_lang(text: str) -> str | None:
    """
    Returns a language code if one non-Latin script clearly dominates the text,
    otherwise returns None (leaving the decision to the LLM).

    This prevents the LLM from guessing 'en' on single-word Cyrillic/Arabic inputs.
    Latin-script languages are always left to the LLM since they share a script.
    """
    text_clean = text.strip()
    if not text_clean:
        return None

    chars = [c for c in text_clean if not c.isspace()]
    if not chars:
        return None

    total = len(chars)

    # Count characters per script
    script_counts: dict[str, int] = {}
    for char in chars:
        for start, end, lang, _ in _SCRIPT_HINTS:
            if start <= char <= end:
                script_counts[lang] = script_counts.get(lang, 0) + 1
                break  # A char belongs to at most one script bucket

    if not script_counts:
        return None  # Pure Latin or mixed — let LLM decide

    # Find dominant script
    dominant_lang = max(script_counts, key=lambda l: script_counts[l])
    dominant_count = script_counts[dominant_lang]
    ratio = dominant_count / total

    if ratio >= _SCRIPT_DOMINANCE_THRESHOLD:
        logger.info(
            "Script-based detection: %d/%d chars (%.0f%%) are %s — assigning lang '%s'",
            dominant_count, total, ratio * 100, dominant_lang, dominant_lang,
        )
        return dominant_lang

    logger.info(
        "No dominant script found (best: %s at %.0f%%) — deferring to LLM",
        dominant_lang, ratio * 100,
    )
    return None



def has_hebrew(text: str) -> bool:
    """Check if text contains Hebrew characters (U+0590 to U+05FF)."""
    return any('\u0590' <= char <= '\u05FF' for char in text)


def detect_language(text: str) -> str | None:
    """
    Pre-check language using Unicode script analysis before hitting the LLM.

    Strategy:
    - Hebrew  → return 'he' immediately (skip translation entirely)
    - Arabic  → return 'ar' (tell LLM which language to translate from)
    - Cyrillic → return 'ru' (covers Russian and Ukrainian — both translate correctly)
    - Latin / mixed / ambiguous → return None (LLM will detect)

    Returns:
        Language code string, or None to trigger LLM-based detection.
    """
    try:
        text_clean = text.strip()

        lang = _dominant_script_lang(text_clean)

        if lang == 'he':
            logger.info("Detected Hebrew via script analysis — skipping translation")
            return 'he'

        if lang is not None:
            logger.info("Detected '%s' via script analysis — skipping LLM detection", lang)
            return lang

        # Latin-script or truly mixed content: defer to LLM
        logger.info("Latin/mixed script — deferring language detection to LLM")
        return None

    except Exception as e:
        logger.warning("Script detection failed: %s", e)
        return None  # Fallback to LLM detection


def validate_detected_language(detected: str, text: str) -> str:
    """
    Validate that detected language is in SUPPORTED_LANGUAGES.
    Returns the validated language code, or defaults to 'en' if unsupported.
    """
    if detected in SUPPORTED_LANGUAGES:
        return detected
    logger.warning(
        "Unsupported language '%s' detected for text: %s. Defaulting to 'en'",
        detected, text[:50],
    )
    return "en"


def _build_batch_user_prompt(
    fields: list[tuple[str, str, str | None]],
) -> str:
    """Build the user-content prompt listing all fields for a batch translation call.

    Args:
        fields: list of (field_name, text, pre_detected_lang | None)

    Returns:
        Formatted user prompt string.
    """
    lines = ["Translate the following fields to Hebrew.\n"]
    for i, (field_name, text, lang_hint) in enumerate(fields, 1):
        entry = f'{i}. field_name: "{field_name}" | text: "{text}"'
        if lang_hint:
            lang_name = LANG_NAMES.get(lang_hint, lang_hint)
            entry += f" | hint: language is {lang_name}"
        lines.append(entry)
    return "\n".join(lines)


async def translate_fields_batch(
    fields: list[tuple[str, str, str | None]],
    instruction: str,
    kind: str | None = None,
) -> tuple[dict[str, tuple[str, str]], tuple[int, int]]:
    """Translate multiple fields in a single LLM call.

    Args:
        fields: list of (field_name, text, pre_detected_lang | None)
        instruction: "default" or "exact"
        kind: form kind for glossary filtering (None = general only)

    Returns:
        Tuple of (results_dict, (prompt_tokens, output_tokens)).
        results_dict maps field_name → (translation, detected_language).
    """
    from src.glossary import format_glossary_for_prompt

    if not fields:
        return {}, (0, 0)

    field_names = [f[0] for f in fields]

    # Select system prompt
    if instruction == "exact":
        system_prompt = SYSTEM_PROMPT_BATCH_EXACT
    else:
        system_prompt = SYSTEM_PROMPT_BATCH_DEFAULT

    glossary_text = await format_glossary_for_prompt(kind=kind)
    if glossary_text:
        system_prompt += "\n" + glossary_text

    user_prompt = _build_batch_user_prompt(fields)
    schema = build_batch_translation_schema(field_names)

    async def _call_llm(client):
        return await client.aio.models.generate_content(
            model=settings.gemini_model,
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                response_mime_type="application/json",
                response_json_schema=schema,
                temperature=0.1,
            ),
        )

    logger.info(
        "Calling LLM for batch translation: instruction=%s, fields=%d, model=%s",
        instruction, len(fields), settings.gemini_model,
    )
    response = await with_retries(
        quota_router.call,
        _call_llm,
        label=f"translate_fields_batch({instruction}, {len(fields)} fields)",
    )

    # Extract token usage for cost tracking
    usage = response.usage_metadata
    prompt_tokens = getattr(usage, "prompt_token_count", 0) or 0
    output_tokens = getattr(usage, "candidates_token_count", 0) or 0

    try:
        parsed = json.loads(response.text)
    except json.JSONDecodeError as e:
        logger.error(
            "Failed to parse translation response as JSON: %s | response: %.300s",
            e, response.text,
        )
        return {}, (prompt_tokens, output_tokens)
    translations_list = parsed.get("translations", [])

    results: dict[str, tuple[str, str]] = {}
    for item in translations_list:
        fname = item.get("field_name", "")
        lang = item.get("detected_language", "en")
        translation = item.get("translation", "")
        lang = validate_detected_language(lang, fname)
        results[fname] = (translation, lang)

    # Check for missing fields
    for fname in field_names:
        if fname not in results:
            logger.warning(
                "Batch translation response missing field '%s' — will be marked as failed",
                fname,
            )

    logger.info(
        "Batch translation (%s) completed: %d/%d fields returned",
        instruction, len(results), len(field_names),
    )
    return results, (prompt_tokens, output_tokens)


async def process_translation_request(
    data: dict, fields_to_translate: list[dict], kind: str | None = None
) -> tuple[dict, list[dict]]:
    from src.field_utils import (
        find_field_in_nested_structure,
        get_nested_value,
        set_nested_value,
    )

    result = dict(data)
    metadata: list[dict] = []

    # ── Phase 1: Resolve paths, extract text, pre-filter ────────
    # Collect translatable fields grouped by instruction type.
    # Each entry: (field_name, original_value, detected_lang, field_path, mode, instruction)
    default_fields: list[tuple[str, str, str | None]] = []
    exact_fields: list[tuple[str, str, str | None]] = []
    # Track field context for post-processing
    field_context: dict[str, dict] = {}

    for field_spec in fields_to_translate:
        field_name = field_spec["field_name"]
        instruction = field_spec["translation_instructions"]

        try:
            # --- Locate the field ---
            if "." in field_name:
                field_path = field_name
                logger.info("Using explicit path for field: %s", field_path)
            elif field_name in result:
                field_path = field_name
                logger.info("Field '%s' found at root level", field_name)
            else:
                paths = find_field_in_nested_structure(result, field_name)
                if not paths:
                    logger.warning("Field '%s' not found in request data.", field_name)
                    metadata.append({
                        "field_name": field_name,
                        "language_detected": "",
                        "instruction": instruction,
                        "translation": "",
                        "raw_content": "",
                        "scores": None,
                        "translation_status": TRANSLATION_STATUS_SKIPPED,
                        "error": "Field not found in data",
                    })
                    continue
                field_path = paths[0]
                if len(paths) > 1:
                    logger.warning(
                        "Field '%s' found at multiple paths: %s. Using first match: %s",
                        field_name, paths, field_path,
                    )
                logger.info("Field '%s' found at path: %s", field_name, field_path)

            original_value, mode = get_nested_value(result, field_path)

            if not isinstance(original_value, str) or not original_value.strip():
                logger.warning(
                    "Field '%s' at path '%s' is empty or not a string.",
                    field_name, field_path,
                )
                metadata.append({
                    "field_name": field_name,
                    "language_detected": "",
                    "instruction": instruction,
                    "translation": original_value if isinstance(original_value, str) else "",
                    "raw_content": original_value if isinstance(original_value, str) else "",
                    "scores": None,
                    "translation_status": TRANSLATION_STATUS_SKIPPED,
                    "error": "Field is empty or not a string",
                })
                continue

            detected_lang = detect_language(original_value)

            # --- Already Hebrew ---
            if detected_lang == "he":
                logger.info(
                    "Field '%s' at path '%s' is already Hebrew. Keeping as-is.",
                    field_name, field_path,
                )
                metadata.append({
                    "field_name": field_name,
                    "language_detected": "he",
                    "instruction": instruction,
                    "translation": original_value,
                    "raw_content": original_value,
                    "scores": None,
                    "translation_status": TRANSLATION_STATUS_SKIPPED,
                    "error": "",
                })
                continue

            # --- Queue for batch translation ---
            field_context[field_name] = {
                "field_path": field_path,
                "mode": mode,
                "original_value": original_value,
                "instruction": instruction,
                "detected_lang": detected_lang,
            }

            entry = (field_name, original_value, detected_lang)
            if instruction == "exact":
                exact_fields.append(entry)
            else:
                default_fields.append(entry)

        except Exception as e:
            logger.error("Failed to process field '%s': %s", field_name, e)
            metadata.append({
                "field_name": field_name,
                "language_detected": "",
                "instruction": instruction,
                "translation": "",
                "raw_content": "",
                "scores": None,
                "translation_status": TRANSLATION_STATUS_FAILED,
                "error": f"Unexpected error: {e}",
            })

    logger.info(
        "Phase 1 complete: %d default + %d exact fields queued for translation",
        len(default_fields), len(exact_fields),
    )

    # ── Phase 2: Batch translate ────────────────────────────────
    translation_results: dict[str, tuple[str, str]] = {}
    total_prompt_tokens = 0
    total_output_tokens = 0

    try:
        if default_fields:
            default_results, default_usage = await translate_fields_batch(default_fields, "default", kind=kind)
            translation_results.update(default_results)
            total_prompt_tokens += default_usage[0]
            total_output_tokens += default_usage[1]
    except Exception as e:
        logger.error("Batch translation (default) failed: %s", e)
        for fname, _, _ in default_fields:
            translation_results[fname] = ("", "unknown")
            ctx = field_context[fname]
            ctx["_batch_error"] = f"Batch translation failed: {e}"

    try:
        if exact_fields:
            exact_results, exact_usage = await translate_fields_batch(exact_fields, "exact", kind=kind)
            translation_results.update(exact_results)
            total_prompt_tokens += exact_usage[0]
            total_output_tokens += exact_usage[1]
    except Exception as e:
        logger.error("Batch translation (exact) failed: %s", e)
        for fname, _, _ in exact_fields:
            translation_results[fname] = ("", "unknown")
            ctx = field_context[fname]
            ctx["_batch_error"] = f"Batch translation failed: {e}"

    # ── Phase 3: Write translations back & prepare evaluation ───
    eval_inputs: list[tuple[str, str, str, str, str]] = []

    for field_name, ctx in field_context.items():
        instruction = ctx["instruction"]
        original_value = ctx["original_value"]
        field_path = ctx["field_path"]
        mode = ctx["mode"]
        batch_error = ctx.get("_batch_error", "")

        if field_name in translation_results:
            translated, final_lang = translation_results[field_name]
        else:
            translated = ""
            final_lang = ctx["detected_lang"] or "unknown"
            batch_error = batch_error or "Field missing from batch translation response"

        if batch_error or not translated:
            # Translation failed for this field
            ctx["translated"] = original_value
            ctx["final_lang"] = final_lang
            ctx["translation_status"] = TRANSLATION_STATUS_FAILED
            ctx["error"] = batch_error or "Empty translation returned"
        else:
            set_nested_value(result, field_path, translated, mode)
            logger.info(
                "Field '%s' at path '%s': translated from %s to Hebrew (mode: %s).",
                field_name, field_path, final_lang, mode or "direct",
            )
            ctx["translated"] = translated
            ctx["final_lang"] = final_lang
            ctx["translation_status"] = TRANSLATION_STATUS_SUCCESS
            ctx["error"] = ""
            eval_inputs.append((
                field_name, original_value, translated, final_lang, instruction,
            ))

    # ── Phase 4: Batch evaluate ─────────────────────────────────
    scores_map: dict[str, dict | None] = {}
    if eval_inputs and settings.eval:
        try:
            scores_map = await evaluate_translations_batch(eval_inputs, kind=kind)
        except Exception as e:
            logger.error("Batch evaluation failed: %s", e)
            # All scores will be None — record the error
            for fname, _, _, _, _ in eval_inputs:
                scores_map[fname] = None
                ctx = field_context[fname]
                existing_err = ctx.get("error", "")
                eval_err = "Evaluation failed after retries"
                ctx["error"] = f"{existing_err}; {eval_err}" if existing_err else eval_err

    # ── Phase 5: Assemble metadata ──────────────────────────────
    for field_name, ctx in field_context.items():
        scores = scores_map.get(field_name)
        translation_status = ctx["translation_status"]

        eval_error = ""
        if scores is None and translation_status == TRANSLATION_STATUS_SUCCESS:
            eval_error = "Evaluation failed after retries"

        combined_error = ctx.get("error", "")
        if eval_error:
            combined_error = f"{combined_error}; {eval_error}" if combined_error else eval_error

        metadata.append({
            "field_name": field_name,
            "language_detected": ctx["final_lang"],
            "instruction": ctx["instruction"],
            "translation": ctx["translated"],
            "raw_content": ctx["original_value"],
            "scores": scores,
            "translation_status": translation_status,
            "error": combined_error,
        })

    # ── Phase 6: Log costs ─────────────────────────────────────
    from src.cost_logger import append_cost_row

    fields_success = sum(
        1 for m in metadata
        if m["translation_status"] == TRANSLATION_STATUS_SUCCESS
    )
    try:
        append_cost_row(
            form_id=form_id_var.get("unknown"),
            fields_translated=fields_success,
            prompt_tokens=total_prompt_tokens,
            output_tokens=total_output_tokens,
        )
    except Exception as e:
        logger.warning("Failed to log cost: %s", e)

    fields_skipped = sum(1 for m in metadata if m["translation_status"] == TRANSLATION_STATUS_SKIPPED)
    fields_failed = sum(1 for m in metadata if m["translation_status"] == TRANSLATION_STATUS_FAILED)
    logger.info(
        "Request complete: %d success, %d skipped, %d failed — tokens: %d prompt / %d output",
        fields_success, fields_skipped, fields_failed,
        total_prompt_tokens, total_output_tokens,
    )

    return result, metadata
