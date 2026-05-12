import logging
from datetime import datetime
from pathlib import Path

from src.config import settings

logger = logging.getLogger(__name__)

COSTS_LOG_PATH = Path(__file__).parent.parent / "costs_log.md"

# Gemini 2.5 Flash pricing (per token)
_INPUT_PRICE_PER_TOKEN = 0.30 / 1_000_000
_OUTPUT_PRICE_PER_TOKEN = 2.50 / 1_000_000

_TABLE_HEADER = (
    "| timestamp | formID | fields_translated | prompt_tokens | output_tokens | cost_usd |\n"
    "| --- | --- | --- | --- | --- | --- |\n"
)


def _calculate_cost(prompt_tokens: int, output_tokens: int) -> float:
    return prompt_tokens * _INPUT_PRICE_PER_TOKEN + output_tokens * _OUTPUT_PRICE_PER_TOKEN


def append_cost_row(
    form_id: str,
    fields_translated: int,
    prompt_tokens: int,
    output_tokens: int,
) -> None:
    cost = _calculate_cost(prompt_tokens, output_tokens)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    row = f"| {timestamp} | {form_id} | {fields_translated} | {prompt_tokens} | {output_tokens} | ${cost:.6f} |\n"

    if settings.log_to_file:
        if not COSTS_LOG_PATH.exists():
            COSTS_LOG_PATH.write_text(_TABLE_HEADER)
        with open(COSTS_LOG_PATH, "a") as f:
            f.write(row)

    logger.info("Cost logged: %s", row.strip())
