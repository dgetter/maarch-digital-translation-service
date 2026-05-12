import itertools
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

import google.genai as genai
from google.genai.errors import APIError

from src.config import settings

logger = logging.getLogger(__name__)
T = TypeVar("T")

# HTTP 429 (rate limit) + gRPC ResourceExhausted (code 8)
_QUOTA_ERROR_CODES = {429, 8}


class QuotaExhaustedError(Exception):
    """Raised when all configured Vertex AI endpoints are quota-exhausted."""


def _is_quota_error(exc: Exception) -> bool:
    return isinstance(exc, APIError) and exc.code in _QUOTA_ERROR_CODES


class QuotaRouter:
    """
    Round-robin Vertex AI project router with per-request quota failover.

    Each call advances an atomic counter so consecutive requests go to
    different projects (load distribution). If the assigned project is
    quota-exhausted, the same request falls through to the next project
    transparently until one succeeds or all are exhausted.
    """

    def __init__(self, endpoints: list[tuple[str, str]]) -> None:
        # endpoints: [(project_id, location), ...]
        self._endpoints = endpoints
        self._counter = itertools.count()
        self._clients: dict[tuple[str, str], genai.Client] = {}

    @property
    def endpoints(self) -> list[tuple[str, str]]:
        return list(self._endpoints)

    def _get_client(self, project: str, location: str) -> genai.Client:
        key = (project, location)
        if key not in self._clients:
            # ADC is used automatically on Cloud Run — no credentials file needed
            self._clients[key] = genai.Client(
                vertexai=True, project=project, location=location
            )
        return self._clients[key]

    async def call(
        self,
        coro_fn: Callable[[genai.Client], Awaitable[T]],
        label: str = "LLM call",
    ) -> T:
        """
        Execute coro_fn(client) starting from the next project in round-robin
        sequence. On quota error rotates to the next project until all are tried.

        Raises QuotaExhaustedError if every endpoint returns a quota error.
        Raises the original exception immediately for non-quota errors.
        """
        n = len(self._endpoints)
        start = next(self._counter) % n
        last_exc: Exception | None = None

        for i in range(n):
            idx = (start + i) % n
            project, location = self._endpoints[idx]
            client = self._get_client(project, location)
            try:
                return await coro_fn(client)
            except Exception as exc:
                if _is_quota_error(exc):
                    logger.warning(
                        "%s: quota exceeded on project=%s (%d/%d endpoints tried), rotating",
                        label, project, i + 1, n,
                    )
                    last_exc = exc
                    continue
                raise  # non-quota error: bubble up to with_retries

        raise QuotaExhaustedError(
            f"All {n} Vertex AI endpoints are quota-exhausted"
        ) from last_exc


# Module-level singleton initialised from settings at import time.
# Each Cloud Run instance has its own counter; the Internal ALB distributes
# requests across instances, so aggregate distribution across projects is
# approximately uniform without any shared state.
quota_router = QuotaRouter(settings.parsed_endpoints)
