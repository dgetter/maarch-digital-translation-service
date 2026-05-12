import asyncio
import logging
from typing import Callable, TypeVar

from google.genai.errors import ServerError

from src.config import settings

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Transient errors worth retrying on the same (or next rotated) project.
# Quota errors (429 / ResourceExhausted) are handled by QuotaRouter instead.
_TRANSIENT_EXCEPTIONS = (
    ServerError,        # 5xx from Gemini
    ConnectionError,
    TimeoutError,
    OSError,            # Covers low-level socket errors
)


def _is_transient(exc: Exception) -> bool:
    """Return True if the error is transient and worth retrying."""
    return isinstance(exc, _TRANSIENT_EXCEPTIONS)


async def with_retries(
    fn: Callable[..., T],
    *args,
    label: str = "LLM call",
    **kwargs,
) -> T:
    """
    Call an async function with retry logic for transient failures.

    Uses settings.llm_max_retries for total attempts and
    settings.llm_retry_delay_seconds for the fixed delay between retries.

    Raises the last exception if all attempts fail.
    """
    max_attempts = settings.llm_max_retries
    delay = settings.llm_retry_delay_seconds
    last_exc: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            return await fn(*args, **kwargs)
        except Exception as exc:
            last_exc = exc
            if not _is_transient(exc) or attempt == max_attempts:
                logger.error(
                    "%s failed on attempt %d/%d (non-retryable or last attempt): %s",
                    label, attempt, max_attempts, exc,
                )
                raise
            logger.warning(
                "%s failed on attempt %d/%d (transient, retrying in %.1fs): %s",
                label, attempt, max_attempts, delay, exc,
            )
            await asyncio.sleep(delay)

    # Should never reach here, but satisfy type checker
    raise last_exc  # type: ignore[misc]
