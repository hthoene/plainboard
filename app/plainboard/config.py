from functools import lru_cache
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    """Process configuration (environment). User-facing settings live in the DB (services.settings)."""

    model_config = SettingsConfigDict(env_prefix="PLAINBOARD_", extra="ignore")

    data_dir: Path = Path("/data")
    # Empty: SQLite in the data folder. Or a PostgreSQL database: postgresql://user:password@host:5432/name
    database: str = ""
    uploads_dir: Path = Path("/uploads")
    static_dir: Path = Path("/app/static")
    runner_url: str = "http://runner:8090"
    # How the runner reaches this app (tool calls from agents).
    internal_url: str = "http://app:8080"
    password: str | None = None
    max_upload_mb: int = 50
    scheduler_interval_seconds: float = 20.0
    job_watch_interval_seconds: float = 10.0
    limits_interval_seconds: float = 900.0  # how often the plan limits of all subscriptions are read
    landing_interval_seconds: float = 30.0  # how often trains of finished branches are looked at
    tidy_interval_seconds: float = 600.0  # how often it is looked whether a repository is due for removing merged branches
    cleanup_interval_seconds: float = 3600.0  # how often disk and memory are looked at for an automatic cleanup (statvfs: cheap)
    standstill_interval_seconds: float = 120.0  # how often the boards are looked at for ready work next to free slots
    # A train waits this long for further branches after the first one is ready: members often
    # finish within minutes of each other, and whoever misses a train waits a whole check run.
    landing_linger_seconds: float = 90.0
    # Newer versions of PlainBoard itself: the commit this build is (written by the image build; the
    # variable overrides it), where newer ones come from (a fork names its own repository) and how
    # often to ask. See services/version.py.
    commit: str = ""
    commit_file: Path = Path("/app/commit")
    update_repository: str = "hthoene/plainboard"
    update_branch: str = "main"
    update_interval_seconds: float = 6 * 3600.0
    # Workspaces as seen by the runner container.
    workspaces_root: str = "/workspaces"
    runner_uploads_dir: str = "/uploads"

    @property
    def postgres(self) -> bool:
        return self.database.strip().startswith(("postgresql://", "postgres://", "postgresql+psycopg://"))

    @property
    def database_url(self) -> str:
        """For the app (async)."""
        return self._postgres_url() if self.postgres else f"sqlite+aiosqlite:///{self.data_dir / 'plainboard.db'}"

    @property
    def sync_database_url(self) -> str:
        """For the migrations (sync). psycopg speaks both ways."""
        return self._postgres_url() if self.postgres else f"sqlite:///{self.data_dir / 'plainboard.db'}"

    def _postgres_url(self) -> str:
        return "postgresql+psycopg://" + self.database.strip().split("://", 1)[1]

    @model_validator(mode="after")
    def _known_database(self) -> "Config":
        if self.database.strip() and not self.postgres:
            raise ValueError("PLAINBOARD_DATABASE must be empty (SQLite) or start with postgresql://")
        return self


@lru_cache
def get_config() -> Config:
    return Config()
