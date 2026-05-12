"""Deep health checks: verifies Firestore connectivity and each Vertex AI endpoint."""

import asyncio
import logging
import time

from google.genai import types

from src.config import settings
from src.quota_router import quota_router

logger = logging.getLogger(__name__)

_HEALTH_PROMPT = "respond with only the word 'ok'"


async def _check_firestore() -> dict:
    try:
        from src.glossary import _get_db
        t0 = time.monotonic()
        db = await _get_db()
        async for _ in db.collection(settings.firestore_collection).limit(1).stream():
            break
        return {
            "status": "ok",
            "project": settings.google_cloud_project,
            "database": settings.firestore_database,
            "latency_ms": round((time.monotonic() - t0) * 1000),
        }
    except Exception as e:
        logger.warning("Firestore health check failed: %s", e)
        return {
            "status": "error",
            "project": settings.google_cloud_project,
            "database": settings.firestore_database,
            "error": str(e),
        }


async def _check_llm_endpoint(project: str, location: str) -> dict:
    try:
        client = quota_router._get_client(project, location)
        t0 = time.monotonic()
        response = await client.aio.models.generate_content(
            model=settings.gemini_model,
            contents=_HEALTH_PROMPT,
            config=types.GenerateContentConfig(
                max_output_tokens=3,
                temperature=0.0,
            ),
        )
        return {
            "status": "ok",
            "project": project,
            "location": location,
            "latency_ms": round((time.monotonic() - t0) * 1000),
            "response": (response.text or "").strip()[:20],
        }
    except Exception as e:
        logger.warning("LLM endpoint health check failed (project=%s): %s", project, e)
        return {
            "status": "error",
            "project": project,
            "location": location,
            "error": str(e),
        }


async def run_deep_health_check() -> dict:
    """
    Concurrently checks Firestore and every configured Vertex AI endpoint.

    Returns a dict with:
      - status: "healthy" | "degraded" | "unhealthy"
      - checks.firestore: connectivity result for the glossary database
      - checks.llm_endpoints: list of per-project connectivity results

    healthy   — Firestore OK + all LLM endpoints OK
    degraded  — Firestore OK + at least one LLM endpoint OK (partial quota loss)
    unhealthy — Firestore failed OR zero LLM endpoints reachable
    """
    endpoints = quota_router.endpoints

    results = await asyncio.gather(
        _check_firestore(),
        *[_check_llm_endpoint(p, l) for p, l in endpoints],
    )

    firestore = results[0]
    llm_endpoints = list(results[1:])

    firestore_ok = firestore["status"] == "ok"
    llm_ok_count = sum(1 for e in llm_endpoints if e["status"] == "ok")

    if firestore_ok and llm_ok_count == len(llm_endpoints):
        overall = "healthy"
    elif firestore_ok and llm_ok_count > 0:
        overall = "degraded"
    else:
        overall = "unhealthy"

    return {
        "status": overall,
        "checks": {
            "firestore": firestore,
            "llm_endpoints": llm_endpoints,
        },
    }
