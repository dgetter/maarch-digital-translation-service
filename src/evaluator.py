# --- Evaluator module for assessing translation quality --- #

import json
import logging
from google.genai import types

from src.config import settings
from src.prompts import (
    SYSTEM_PROMPT_BATCH_EVALUATE,
    SYSTEM_PROMPT_DEFAULT,
    SYSTEM_PROMPT_EXACT,
)
from src.retry import with_retries
from src.models import build_batch_evaluation_schema

logger = logging.getLogger(__name__)

LANG_NAMES = {
    "he": "Hebrew",
    "en": "English",
    "fr": "French",
    "es": "Spanish",
    "ar": "Arabic",
    "ru": "Russian",
}


async def _build_evaluation_user_prompt(
    fields: list[tuple[str, str, str, str, str]],
    kind: str | None = None,
) -> str:
    """Build the user prompt listing all fields for batch evaluation.

    Args:
        fields: list of (field_name, original_text, translated_text, source_lang, instruction)
        kind: form kind for glossary filtering (None = general only)

    Returns:
        Formatted user prompt string.
    """
    from src.glossary import format_glossary_for_prompt

    sections = []
    for i, (field_name, original_text, translated_text, source_lang, instruction) in enumerate(fields, 1):
        lang_name = LANG_NAMES.get(source_lang, source_lang)

        if instruction == "exact":
            guidelines = SYSTEM_PROMPT_EXACT.format(source_language=lang_name)
        else:
            guidelines = SYSTEM_PROMPT_DEFAULT.format(source_language=lang_name)

        section = (
            f"--- Field {i}: \"{field_name}\" ---\n"
            f"Original text ({lang_name}): {original_text}\n"
            f"System translation (Hebrew): {translated_text}\n"
            f"Translation mode: {instruction}\n"
            f"Guidelines given to translator:\n{guidelines}"
        )
        sections.append(section)

    prompt = "\n\n".join(sections)

    glossary_text = await format_glossary_for_prompt(kind=kind)
    if glossary_text:
        prompt += f"\n\n--- Glossary (applies to all fields) ---\n{glossary_text}"

    return prompt


async def evaluate_translations_batch(
    fields: list[tuple[str, str, str, str, str]],
    kind: str | None = None,
) -> dict[str, dict | None]:
    """Evaluate multiple translations in a single LLM call.

    Args:
        fields: list of (field_name, original_text, translated_text, source_lang, instruction)
        kind: form kind for glossary filtering (None = general only)

    Returns:
        dict mapping field_name → scores dict (with recomputed overall) or None if missing.
    """
    from src.quota_router import quota_router

    if not fields:
        return {}

    field_names = [f[0] for f in fields]
    user_prompt = await _build_evaluation_user_prompt(fields, kind=kind)
    schema = build_batch_evaluation_schema(field_names)

    async def _call_llm(client):
        return await client.aio.models.generate_content(
            model=settings.gemini_model,
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT_BATCH_EVALUATE,
                response_mime_type="application/json",
                response_json_schema=schema,
                # Raised from 0.2 → 0.7 to allow the evaluator to express
                # genuine uncertainty and produce a realistic score distribution.
                # At 0.2 the model anchors to the top of the scale and never
                # deviates, making the evaluator useless as a quality signal.
                temperature=0.7,
            ),
        )

    logger.info(
        "Calling LLM for batch evaluation: fields=%d, model=%s",
        len(fields), settings.gemini_model,
    )
    response = await with_retries(
        quota_router.call,
        _call_llm,
        label=f"evaluate_translations_batch({len(fields)} fields)",
    )

    try:
        parsed = json.loads(response.text)
    except json.JSONDecodeError as e:
        logger.error(
            "Failed to parse evaluation response as JSON: %s | response: %.300s",
            e, response.text,
        )
        return {fname: None for fname in field_names}
    evaluations_list = parsed.get("evaluations", [])

    results: dict[str, dict | None] = {}
    for item in evaluations_list:
        fname = item.get("field_name", "")
        if not fname:
            continue

        # Recompute overall using the defined weights so it is always
        # consistent regardless of what the LLM calculated.
        recomputed_overall = round(
            item.get("faithfulness", 0) * 0.6
            + item.get("fluency", 0) * 0.2
            + item.get("glossary_compliance", 0) * 0.2,
            2,
        )

        results[fname] = {
            "faithfulness": item.get("faithfulness", 0),
            "fluency": item.get("fluency", 0),
            "glossary_compliance": item.get("glossary_compliance", 0),
            "overall": recomputed_overall,
        }

    # Check for missing fields
    for fname in field_names:
        if fname not in results:
            logger.warning(
                "Batch evaluation response missing field '%s'", fname,
            )
            results[fname] = None

    logger.info(
        "Batch evaluation completed: %d/%d fields scored",
        sum(1 for v in results.values() if v is not None), len(field_names),
    )
    return results
