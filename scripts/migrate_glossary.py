"""One-time migration: data/glossary.json → Firestore glossary_entries collection.

Usage:
    python scripts/migrate_glossary.py

Prerequisites:
    - Firestore database "glossarydb" must exist:
      gcloud firestore databases create --database="glossarydb" --location=us-central1 --type=firestore-native
    - GOOGLE_APPLICATION_CREDENTIALS env var (or ADC) must grant Firestore access
    - google-cloud-firestore must be installed: pip install google-cloud-firestore
"""

import asyncio
import json
import os
import sys
from pathlib import Path

# Ensure project root is on the path so we can import src.config
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from google.cloud.firestore_v1 import AsyncClient


async def migrate():
    from src.config import settings

    # Authentication via ADC (Application Default Credentials).
    # Set GOOGLE_APPLICATION_CREDENTIALS env var before running if not on Cloud Run.

    glossary_file = PROJECT_ROOT / "data" / "glossary.json"
    if not glossary_file.exists():
        print(f"ERROR: {glossary_file} not found")
        sys.exit(1)

    with open(glossary_file, "r", encoding="utf-8") as f:
        entries = json.load(f)

    if not isinstance(entries, list):
        print("ERROR: glossary.json is not a JSON array")
        sys.exit(1)

    print(f"Loaded {len(entries)} entries from {glossary_file}")

    db = AsyncClient(
        project=settings.google_cloud_project,
        database=settings.firestore_database,
    )
    collection = db.collection(settings.firestore_collection)

    # Firestore batch supports up to 500 writes
    batch = db.batch()
    for entry in entries:
        doc_ref = collection.document(entry["id"])
        batch.set(doc_ref, {
            "term": entry["term"],
            "hebrew": entry["hebrew"],
            "explanation": entry.get("explanation", ""),
            "kind": "general",
        })

    await batch.commit()
    print(f"Migrated {len(entries)} entries to Firestore "
          f"(project={settings.google_cloud_project}, "
          f"database={settings.firestore_database}, "
          f"collection={settings.firestore_collection})")


if __name__ == "__main__":
    asyncio.run(migrate())
