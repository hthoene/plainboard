from functools import cached_property, lru_cache
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    """Process configuration (environment). User-facing settings live in the DB (services.settings)."""

    model_config = SettingsConfigDict(env_prefix="PLAINBOARD_", extra="ignore")

    data_dir: Path = Path("/data")
    # Empty or "sqlite": SQLite in the data folder. A PostgreSQL database: postgresql://user:password@host:5432/name.
    # "auto" (the compose file): the bundled PostgreSQL "db", unless the data folder holds a SQLite installation
    # that was not asked to move yet (db_move.resolve_auto).
    database: str = ""
    uploads_dir: Path = Path("/uploads")
    static_dir: Path = Path("/app/static")
    runner_url: str = "http://runner:8090"
    # How the runner reaches this app (tool calls from agents).
    internal_url: str = "http://app:8080"
    password: str | None = None
    max_upload_mb: int = 50
    max_import_mb: int = 4096  # a project file (services/transfer.py), compressed; what it unpacks to is capped there
    # Security (api/guard.py). A separate address for previews, e.g. https://preview.example.com: agents'
    # servers then never run on PlainBoard's own origin. Empty: they run under /preview/<port>/ on it.
    preview_origin: str = ""
    preview_sandbox: bool = False  # without preview_origin, opt-in: the CSP sandbox (no access to PlainBoard, but a private preview loads nothing)
    secure_cookies: str = ""  # true | false; empty: Secure when the request came over HTTPS (also X-Forwarded-Proto)
    hsts: bool = True  # Strict-Transport-Security, only on responses over HTTPS
    allowed_origins: str = ""  # further origins (comma separated) whose pages may use the signed-in API and sockets
    scheduler_interval_seconds: float = 20.0
    job_watch_interval_seconds: float = 10.0
    limits_interval_seconds: float = 900.0  # how often the plan limits of all subscriptions are read
    landing_interval_seconds: float = 30.0  # how often trains of finished branches are looked at
    tidy_interval_seconds: float = 600.0  # how often it is looked whether a repository is due for removing merged branches
    cleanup_interval_seconds: float = 3600.0  # how often disk and memory are looked at for an automatic cleanup (statvfs: cheap)
    docs_interval_seconds: float = 300.0  # how often the docs watcher looks for landed changes that concern doc pages (orchestration/docs_watcher.py)
    search_interval_seconds: float = 30.0  # how often the semantic search index looks for changes (orchestration/search_indexer.py)
    # Threads that embed for semantic search, at lowered priority (services/search/embedding.py). 0: up to 8, two cores left free.
    search_threads: int = 0
    standstill_interval_seconds: float = 120.0  # how often the boards are looked at for ready work next to free slots
    # A train waits this long for further branches after the first one is ready: members often
    # finish within minutes of each other, and whoever misses a train waits a whole check run.
    landing_linger_seconds: float = 90.0
    # Newer versions of PlainBoard itself: the commit this build is (written by the image build; the
    # variable overrides it), where newer ones come from (a fork names its own repository) and how
    # often to ask. See services/version.py.
    commit: str = ""
    commit_file: Path = Path("/app/commit")
    # The release this image is ("X.Y.Z", from the tag vX.Y.Z it was built for; the variable overrides
    # it); empty for a build of a commit (:edge, a local build). See RELEASING.md.
    version: str = ""
    version_file: Path = Path("/app/version")
    update_repository: str = "hthoene/plainboard"
    update_branch: str = "main"
    update_interval_seconds: float = 6 * 3600.0
    # Workspaces as seen by the runner container.
    workspaces_root: str = "/workspaces"
    runner_uploads_dir: str = "/uploads"

    @property
    def auto(self) -> bool:
        return self.database.strip().lower() == "auto"

    @cached_property
    def effective_database(self) -> str:
        """What `database` comes to: "" (SQLite) or a PostgreSQL URL. For "auto" decided once, at the first look."""
        if self.auto:
            from .db_move import resolve_auto

            return resolve_auto(self.data_dir / "plainboard.db")
        return "" if self.database.strip().lower() == "sqlite" else self.database.strip()

    def resolve_again(self) -> None:
        self.__dict__.pop("effective_database", None)

    @property
    def postgres(self) -> bool:
        return _is_postgres(self.effective_database)

    @property
    def database_url(self) -> str:
        """For the app (async)."""
        return self._postgres_url() if self.postgres else f"sqlite+aiosqlite:///{self.data_dir / 'plainboard.db'}"

    @property
    def sync_database_url(self) -> str:
        """For the migrations (sync). psycopg speaks both ways."""
        return self._postgres_url() if self.postgres else f"sqlite:///{self.data_dir / 'plainboard.db'}"

    def _postgres_url(self) -> str:
        return "postgresql+psycopg://" + self.effective_database.split("://", 1)[1]

    @model_validator(mode="after")
    def _known_database(self) -> "Config":
        value = self.database.strip()
        if value and value.lower() not in ("sqlite", "auto") and not _is_postgres(value):
            raise ValueError("PLAINBOARD_DATABASE must be empty or sqlite (SQLite), auto, or start with postgresql://")
        return self


def _is_postgres(value: str) -> bool:
    return value.startswith(("postgresql://", "postgres://", "postgresql+psycopg://"))


@lru_cache
def get_config() -> Config:
    return Config()
