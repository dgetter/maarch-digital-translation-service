#!/usr/bin/env python3
"""
Import glossary entries from data/glossary.json into Firestore.

Usage:
  source .venv/bin/activate
  python scripts/import_glossary.py [--skip-existing]

Options:
  --skip-existing  Skip entries that already exist in Firestore (by ID)
"""

import asyncio
import json
import logging
import sys
from pathlib import Path

from google.cloud.firestore_v1 import AsyncClient

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Add src to path so we can import config
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import settings


async def import_glossary(skip_existing: bool = False) -> None:
    """Import glossary entries from JSON file to Firestore."""

    # Load glossary JSON
    glossary_path = Path(__file__).parent.parent / "data" / "glossary.json"
    if not glossary_path.exists():
        logger.error(f"Glossary file not found: {glossary_path}")
        return

    with open(glossary_path, "r", encoding="utf-8") as f:
        entries = json.load(f)

    logger.info(f"Loaded {len(entries)} entries from {glossary_path}")

    # Initialize Firestore client
    db = AsyncClient(
        project=settings.google_cloud_project,
        database=settings.firestore_database,
    )
    col = db.collection(settings.firestore_collection)

    # Import entries
    imported = 0
    skipped = 0
    errors = 0

    for i, entry in enumerate(entries, 1):
        entry_id = entry.get("id")
        if not entry_id:
            logger.warning(f"Entry {i} missing 'id' field, skipping")
            errors += 1
            continue

        # Check if entry exists
        if skip_existing:
            doc = await col.document(entry_id).get()
            if doc.exists:
                logger.debug(f"Entry {entry_id} already exists, skipping")
                skipped += 1
                continue

        # Prepare document data
        doc_data = {
            "term": entry.get("term", ""),
            "hebrew": entry.get("hebrew", ""),
            "explanation": entry.get("explanation", ""),
            "kind": entry.get("kind", "general"),  # Default to "general" if not specified
        }

        # Write to Firestore
        try:
            await col.document(entry_id).set(doc_data)
            imported += 1
            if imported % 50 == 0:
                logger.info(f"Progress: {imported}/{len(entries)} imported")
        except Exception as e:
            logger.error(f"Failed to import entry {entry_id}: {e}")
            errors += 1

    await db.close()

    # Summary
    logger.info("=" * 60)
    logger.info(f"Import complete!")
    logger.info(f"  Imported: {imported}")
    logger.info(f"  Skipped:  {skipped}")
    logger.info(f"  Errors:   {errors}")
    logger.info(f"  Total:    {imported + skipped + errors}")
    logger.info("=" * 60)


if __name__ == "__main__":
    skip_existing = "--skip-existing" in sys.argv

    logger.info(f"Importing glossary to Firestore")
    logger.info(f"  Project:    {settings.google_cloud_project}")
    logger.info(f"  Database:   {settings.firestore_database}")
    logger.info(f"  Collection: {settings.firestore_collection}")
    logger.info(f"  Skip existing: {skip_existing}")
    logger.info("")

    asyncio.run(import_glossary(skip_existing=skip_existing))
