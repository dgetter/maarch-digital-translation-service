from typing import Literal, Optional

import re

from pydantic import BaseModel, Field, field_validator

# Regex for glossary term format: "xx:text | yy:text | ..."
# Each segment is a 2-letter language code, colon, then one or more non-pipe characters.
_TERM_SEGMENT = r"[a-z]{2}:[^|]+"
TERM_PATTERN = re.compile(rf"^{_TERM_SEGMENT}(\s\|\s{_TERM_SEGMENT})*$")

# Translation status values used in metadata
TRANSLATION_STATUS_SUCCESS = "success"
TRANSLATION_STATUS_FAILED = "failed"
TRANSLATION_STATUS_SKIPPED = "skipped"


class FieldToTranslate(BaseModel):
    """Specifies a single field to translate and the translation strategy."""

    field_name: str = Field(
        description="Name of the JSON field to locate and translate. "
        "The service searches for this field recursively in the provided data — "
        "it can be a top-level key, nested inside objects, or inside arrays of objects.",
        examples=["subject_en", "job_title_ar", "notes_es"],
    )
    translation_instructions: Literal["default", "exact"] = Field(
        description="Translation strategy: "
        "'default' produces a natural, fluent Hebrew translation; "
        "'exact' produces a literal/verbatim translation preserving the source structure as closely as possible.",
        examples=["default"],
    )


class TranslateRequest(BaseModel):
    """Request body for the translation endpoint.

    Send the list of fields you want translated in `fields_to_translate`,
    along with the source data as additional top-level keys (the model accepts
    arbitrary extra fields).

    **How field lookup works:**
    The service recursively searches the entire data payload for each
    `field_name`. Fields can live at any nesting depth — inside objects,
    arrays of objects, etc. If a field's value is an object with a `dataText`
    key, the service translates the `dataText` value; otherwise it translates
    the field value directly (e.g. a plain string).

    **Special cases:**
    - Fields whose source language is already Hebrew are **skipped** (returned with `translation_status: "skipped"`).
    - Fields listed in `fields_to_translate` but not found anywhere in the data are **skipped** with an error message `"Field not found in data"`.
    """

    formID: str = Field(
        description="Unique form identifier used for request tracing and logging.",
        examples=["consular@example.gov.il"],
    )
    fields_to_translate: list[FieldToTranslate] = Field(
        description="List of fields to translate. Each entry specifies the field name to look up in the data and the translation strategy to use.",
    )
    kind: str | None = Field(
        default=None,
        description="Form kind (e.g. 'consular', 'immigration'). "
        "When provided, the glossary includes both general entries and entries "
        "specific to this kind. When absent or null, only general glossary entries are used.",
    )

    model_config = {
        "extra": "allow",
        "json_schema_extra": {
            "examples": [
                {
                    "formID": "consular@example.gov.il",
                    "fields_to_translate": [
                        {"field_name": "subject_en", "translation_instructions": "default"},
                        {"field_name": "previouslySent", "translation_instructions": "exact"},
                        {"field_name": "reason_fr", "translation_instructions": "default"},
                        {"field_name": "job_title_ar", "translation_instructions": "exact"},
                        {"field_name": "notes_es", "translation_instructions": "default"},
                        {"field_name": "remarks_he", "translation_instructions": "default"},
                        {"field_name": "nonexistent_field", "translation_instructions": "default"},
                    ],
                    "data": {
                        "entry": {
                            "subject_en": {
                                "dataCode": "1",
                                "dataText": "Request for consular appointment",
                            },
                            "previouslySent": {
                                "dataCode": "2",
                                "dataText": "нет",
                            },
                            "reason_fr": {
                                "dataCode": "3",
                                "dataText": "Mon passeport a expiré et j'ai besoin de le renouveler avant mon voyage.",
                            },
                            "remarks_he": {
                                "dataCode": "4",
                                "dataText": "קיבלתי הודעה על דחיית הבקשה ואני מגיש ערר.",
                            },
                        },
                        "mahutHafnaya": {
                            "tblMahutHafnayaList": [
                                {
                                    "questId": "12345678",
                                    "lname": "محمد",
                                    "fname": "أحمد",
                                    "job_title_ar": {
                                        "dataCode": "5",
                                        "dataText": "مهندس برمجيات",
                                    },
                                    "notes_es": "Soy madre soltera con dos hijos. Solicito la reducción máxima permitida por ley.",
                                    "email": "test@mail.com",
                                    "phoneNumber": "+972501234567",
                                }
                            ]
                        },
                    },
                }
            ]
        },
    }


class EvaluationScores(BaseModel):
    """LLM-as-Judge quality scores for a translated field. Each score ranges from 0 to 10."""

    faithfulness: float = Field(description="How accurately the translation preserves the original meaning (0–10)")
    fluency: float = Field(description="How natural and grammatically correct the Hebrew reads (0–10)")
    glossary_compliance: float = Field(description="How well glossary terms were applied (0–10)")
    overall: float = Field(description="Weighted overall quality score (0–10)")


class TranslatedFieldMeta(BaseModel):
    """Metadata for a single translated field returned in the response."""

    field_name: str = Field(description="Name of the field that was requested for translation")
    language_detected: str = Field(
        description="ISO 639-1 language code detected in the source text (e.g. 'en', 'ar', 'fr'). "
        "Empty string if the field was not found in the data.",
        examples=["en", "ar", "fr", "ru", "es", "he"],
    )
    instruction: Literal["default", "exact"] = Field(description="Translation strategy that was used for this field")
    translation: str = Field(
        description="The translated Hebrew text. Empty string if the field was skipped or not found."
    )
    raw_content: str = Field(
        description="The original source text that was translated. "
        "For fields with a `dataText` sub-key, this is the `dataText` value. "
        "Empty string if the field was not found."
    )
    scores: Optional[EvaluationScores] = Field(
        default=None,
        description="Quality evaluation scores (0–10 each). "
        "null when the field was skipped (already Hebrew or not found).",
    )
    translation_status: str = Field(
        description="'success' — translated and scored; "
        "'skipped' — field already in Hebrew or not found in data; "
        "'failed' — translation attempted but errored.",
        examples=["success", "skipped", "failed"],
    )
    error: str = Field(
        default="",
        description="Error message explaining why translation was skipped or failed. "
        "Empty string on success. Example: 'Field not found in data'.",
    )


class TranslationMetadata(BaseModel):
    """Metadata block appended to the translation response."""

    fields_translated: list[TranslatedFieldMeta]


# ── Glossary models ──────────────────────────────────────────────

class GlossaryEntry(BaseModel):
    """A glossary entry mapping a source term to its Hebrew translation."""

    id: str = Field(description="Unique entry identifier")
    term: str = Field(description="Source term", examples=["machine learning"])
    hebrew: str = Field(description="Hebrew translation", examples=["למידת מכונה"])
    explanation: str = Field(default="", description="Optional context or usage notes")
    kind: str = Field(
        default="general",
        description="Form kind this entry applies to. 'general' means all forms.",
    )


class GlossaryCreateRequest(BaseModel):
    """Request body for creating a new glossary entry."""

    term: str = Field(
        description="Source term with language-prefixed segments separated by ' | '. "
        "Each segment must be a 2-letter ISO 639-1 language code followed by a colon "
        "and the term text. Multiple synonyms per language are comma-separated. "
        "Format: `xx:term | yy:term | ...` "
        "Example: `en:Health fund, Fondo de salud | ru:Больничная касса | ar:صندوق المرضى | fr:Caisse maladie`",
        examples=[
            "en:Health fund | ru:Больничная касса | ar:صندوق המרضى | fr:Caisse maladie",
            "en:Referral | ru:Направление | ar:إحالة, تحويل طبي | fr:Référence médicale | es:Derivación médica",
        ],
    )
    hebrew: str = Field(
        min_length=1,
        description="Hebrew translation for the term. Must not be empty.",
        examples=["קופת חולים"],
    )
    explanation: str = Field(default="", description="Optional context or usage notes")
    kind: str = Field(
        default="general",
        description="Form kind scope. 'general' applies to all forms.",
    )

    @field_validator("term")
    @classmethod
    def validate_term_format(cls, v: str) -> str:
        v = v.strip()
        if not TERM_PATTERN.match(v):
            raise ValueError(
                "term must follow the format 'xx:text | yy:text | ...' "
                "where xx/yy are 2-letter language codes. "
                "Example: 'en:Referral | ru:Направление | ar:إحالة'"
            )
        return v

    @field_validator("hebrew")
    @classmethod
    def hebrew_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("hebrew must not be blank or whitespace-only")
        return v.strip()


class GlossaryUpdateRequest(BaseModel):
    """Request body for updating an existing glossary entry. All fields are optional."""

    term: str | None = Field(
        default=None,
        description="Updated source term. Must follow the format 'xx:text | yy:text | ...'",
    )
    hebrew: str | None = Field(default=None, description="Updated Hebrew translation")
    explanation: str | None = Field(default=None, description="Updated explanation")
    kind: str | None = Field(default=None, description="Updated kind scope")

    @field_validator("term")
    @classmethod
    def validate_term_format(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip()
        if not TERM_PATTERN.match(v):
            raise ValueError(
                "term must follow the format 'xx:text | yy:text | ...' "
                "where xx/yy are 2-letter language codes."
            )
        return v

    @field_validator("hebrew")
    @classmethod
    def hebrew_not_blank(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("hebrew must not be blank or whitespace-only")
        return v.strip() if v else v


# ── Dynamic JSON Schemas for batched LLM calls ─────────────────

SUPPORTED_LANGUAGE_CODES = ["en", "fr", "es", "ar", "ru"]


def build_batch_translation_schema(field_names: list[str]) -> dict:
    """Build a JSON Schema that enforces the LLM to return one translation
    per requested field, with the field_name constrained to the exact set."""
    return {
        "type": "object",
        "properties": {
            "translations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "field_name": {
                            "type": "string",
                            "enum": field_names,
                        },
                        "detected_language": {
                            "type": "string",
                            "enum": SUPPORTED_LANGUAGE_CODES,
                        },
                        "translation": {"type": "string"},
                    },
                    "required": ["field_name", "detected_language", "translation"],
                    "propertyOrdering": ["field_name", "detected_language", "translation"],
                },
                "minItems": len(field_names),
                "maxItems": len(field_names),
            }
        },
        "required": ["translations"],
    }


def build_batch_evaluation_schema(field_names: list[str]) -> dict:
    """Build a JSON Schema that enforces the LLM to return evaluation scores
    for each translated field."""
    return {
        "type": "object",
        "properties": {
            "evaluations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "field_name": {
                            "type": "string",
                            "enum": field_names,
                        },
                        "faithfulness": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 10,
                        },
                        "fluency": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 10,
                        },
                        "glossary_compliance": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 10,
                        },
                    },
                    "required": ["field_name", "faithfulness", "fluency", "glossary_compliance"],
                    "propertyOrdering": ["field_name", "faithfulness", "fluency", "glossary_compliance"],
                },
                "minItems": len(field_names),
                "maxItems": len(field_names),
            }
        },
        "required": ["evaluations"],
    }
