# --- Glossary management backed by Firestore with in-memory TTL cache. ---
import logging
import time
import uuid
from typing import Optional

from google.cloud.firestore_v1 import AsyncClient
from pydantic import ValidationError

from src.config import settings
from src.models import GlossaryEntry

logger = logging.getLogger(__name__)

# ── Firestore client (lazy singleton) ─────────────────────────────

_db: AsyncClient | None = None


async def _get_db() -> AsyncClient:
    global _db
    if _db is None:
        _db = AsyncClient(
            project=settings.google_cloud_project,
            database=settings.firestore_database,
        )
    return _db


def _collection_ref(db: AsyncClient):
    return db.collection(settings.firestore_collection)


# ── TTL cache ─────────────────────────────────────────────────────

_CACHE_TTL = 300  # seconds

_cache: dict[str, tuple[float, list[GlossaryEntry]]] = {}


def _cache_get(key: str) -> list[GlossaryEntry] | None:
    entry = _cache.get(key)
    if entry is None:
        return None
    expires_at, entries = entry
    if time.monotonic() > expires_at:
        del _cache[key]
        return None
    return entries


def _cache_set(key: str, entries: list[GlossaryEntry]) -> None:
    _cache[key] = (time.monotonic() + _CACHE_TTL, entries)


def _cache_clear() -> None:
    _cache.clear()


# ── Public API ────────────────────────────────────────────────────


async def get_all_entries(kind: str | None = None) -> list[GlossaryEntry]:
    """Return all glossary entries, optionally filtered by kind."""
    db = await _get_db()
    col = _collection_ref(db)
    if kind is not None:
        query = col.where("kind", "==", kind)
    else:
        query = col
    docs = [doc async for doc in query.stream()]
    entries = []
    for doc in docs:
        try:
            entries.append(GlossaryEntry(id=doc.id, **doc.to_dict()))
        except ValidationError:
            logger.warning("Skipping invalid glossary document: id=%s", doc.id)
    return entries


async def get_entries_for_kind(kind: str | None) -> list[GlossaryEntry]:
    """Return entries relevant for a translation request.

    - kind=None  → general entries only
    - kind=value → general + kind-specific entries
    """
    cache_key = f"kind:{kind or 'general'}"
    cached = _cache_get(cache_key)
    if cached is not None:
        logger.debug("Glossary cache hit: key=%s (%d entries)", cache_key, len(cached))
        return cached

    db = await _get_db()
    col = _collection_ref(db)

    if kind is None or kind == "general":
        query = col.where("kind", "==", "general")
    else:
        query = col.where("kind", "in", ["general", kind])

    docs = [doc async for doc in query.stream()]
    entries = []
    for doc in docs:
        try:
            entries.append(GlossaryEntry(id=doc.id, **doc.to_dict()))
        except ValidationError:
            logger.warning("Skipping invalid glossary document: id=%s", doc.id)
    logger.debug("Glossary loaded from Firestore: key=%s (%d entries)", cache_key, len(entries))
    _cache_set(cache_key, entries)
    return entries


async def create_entry(
    term: str, hebrew: str, explanation: str = "", kind: str = "general"
) -> GlossaryEntry:
    db = await _get_db()
    col = _collection_ref(db)
    entry_id = str(uuid.uuid4())
    doc_data = {
        "term": term,
        "hebrew": hebrew,
        "explanation": explanation,
        "kind": kind,
    }
    await col.document(entry_id).set(doc_data)
    _cache_clear()
    logger.info("Glossary entry created in Firestore: id=%s, term=%r, kind=%s", entry_id, term, kind)
    return GlossaryEntry(id=entry_id, **doc_data)


async def update_entry(
    entry_id: str,
    term: Optional[str] = None,
    hebrew: Optional[str] = None,
    explanation: Optional[str] = None,
    kind: Optional[str] = None,
) -> Optional[GlossaryEntry]:
    db = await _get_db()
    doc_ref = _collection_ref(db).document(entry_id)
    doc = await doc_ref.get()
    if not doc.exists:
        return None

    updates: dict = {}
    if term is not None:
        updates["term"] = term
    if hebrew is not None:
        updates["hebrew"] = hebrew
    if explanation is not None:
        updates["explanation"] = explanation
    if kind is not None:
        updates["kind"] = kind

    if updates:
        logger.info("Glossary entry updated in Firestore: id=%s, fields=%s", entry_id, list(updates.keys()))
        await doc_ref.update(updates)

    _cache_clear()
    updated = await doc_ref.get()
    try:
        return GlossaryEntry(id=updated.id, **updated.to_dict())
    except ValidationError:
        logger.warning("Updated glossary document failed validation: id=%s", entry_id)
        return None


async def delete_entry(entry_id: str) -> bool:
    db = await _get_db()
    doc_ref = _collection_ref(db).document(entry_id)
    doc = await doc_ref.get()
    if not doc.exists:
        return False
    await doc_ref.delete()
    _cache_clear()
    logger.info("Glossary entry deleted from Firestore: id=%s", entry_id)
    return True


async def get_all_kinds() -> list[str]:
    """Return a sorted list of distinct kind values from all glossary entries."""
    db = await _get_db()
    col = _collection_ref(db)
    kinds: set[str] = set()
    async for doc in col.stream():
        data = doc.to_dict()
        if "kind" in data and data["kind"]:
            kinds.add(data["kind"])
    if "general" not in kinds:
        kinds.add("general")
    return sorted(kinds)


async def format_glossary_for_prompt(kind: str | None = None) -> str:
    """Format glossary entries for injection into the LLM system prompt.

    Each entry's `term` field may contain multiple multilingual variants
    separated by " | " (e.g. "Permanent resident | Постоянный житель | مقيم دائم").
    The LLM should match ANY of these variants in the source text and
    translate to the specified Hebrew term.
    """
    entries = await get_entries_for_kind(kind)
    if not entries:
        return ""
    lines = [
        "\n## Glossary — מילון מונחים",
        "When translating to Hebrew, you MUST use the exact Hebrew terms listed below.",
        "Each entry lists equivalent terms in multiple languages separated by |.",
        "If the source text contains ANY of the listed terms (in any language),",
        "use the Hebrew term exactly as specified.",
        "Even if an exact variant is not listed, if you recognise the same concept",
        "in another language, apply the corresponding Hebrew term.\n",
    ]
    for e in entries:
        line = f"• {e.term} → {e.hebrew}"
        if e.explanation:
            line += f"\n  ({e.explanation})"
        lines.append(line)
    return "\n".join(lines)
