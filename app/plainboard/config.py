from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    """Process configuration (environment). User-facing settings live in the DB (services.settings)."""

    model_config = SettingsConfigDict(env_prefix="PLAINBOARD_", extra="ignore")

    data_dir: Path = Path("/data")
    uploads_dir: Path = Path("/uploads")
    static_dir: Path = Path("/app/static")
    runner_url: str = "http://runner:8090"
    # How the runner reaches this app (tool calls from agents).
    internal_url: str = "http://app:8080"
    password: str | None = None
    max_upload_mb: int = 50
    scheduler_interval_seconds: float = 20.0
    # Workspaces as seen by the runner container.
    workspaces_root: str = "/workspaces"
    runner_uploads_dir: str = "/uploads"

    @property
    def database_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.data_dir / 'plainboard.db'}"


@lru_cache
def get_config() -> Config:
    return Config()
