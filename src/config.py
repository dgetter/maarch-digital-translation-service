from contextvars import ContextVar

from pydantic import field_validator
from pydantic_settings import BaseSettings

# Per-request form ID, set at the start of each /api/translate call.
# The logging filter reads this to prefix every log line.
form_id_var: ContextVar[str] = ContextVar("form_id", default="")


class Settings(BaseSettings):
    google_cloud_project: str = "<project-id-placeholder>"
    google_cloud_location: str = "global"
    # Comma-separated list of "project:location" pairs for Vertex AI.
    # Example: "proj-a:us-central1,proj-b:europe-west4"
    # Falls back to google_cloud_project + google_cloud_location when empty.
    vertex_ai_endpoints: str = ""
    gemini_model: str = "gemini-2.5-flash"
    log_level: str = "INFO"

    # Retry settings
    llm_max_retries: int = 3          # Total attempts (1 initial + 2 retries)
    llm_retry_delay_seconds: float = 2.0  # Fixed delay between retries

    # Evaluator settings
    eval: bool = False

    # Cost logging
    log_to_file: bool = False

    # Glossary admin
    glossary_admin_password: str = "admin"

    # Firestore settings
    firestore_database: str = "glossarydb"
    firestore_collection: str = "glossary_entries"

    model_config = {"env_file": ".env", "extra": "ignore"}

    @field_validator("llm_max_retries")
    @classmethod
    def clamp_min_retries(cls, v: int) -> int:
        return max(v, 1)

    @property
    def parsed_endpoints(self) -> list[tuple[str, str]]:
        """Return list of (project_id, location) tuples from VERTEX_AI_ENDPOINTS.

        Falls back to (google_cloud_project, google_cloud_location) when the
        env var is not set, so local dev with a single project still works.
        """
        raw = self.vertex_ai_endpoints.strip()
        if not raw:
            return [(self.google_cloud_project, self.google_cloud_location)]
        result = []
        for entry in raw.split(","):
            entry = entry.strip()
            if not entry:
                continue
            parts = entry.split(":", 1)
            if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
                raise ValueError(
                    f"Invalid VERTEX_AI_ENDPOINTS entry: '{entry}'. "
                    f"Expected format 'project_id:location' (e.g. 'my-project:us-central1')."
                )
            result.append((parts[0].strip(), parts[1].strip()))
        return result


settings = Settings()
