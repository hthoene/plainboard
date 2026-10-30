import os
from pathlib import Path

WORKSPACES_ROOT = Path(os.environ.get("PLAINBOARD_WORKSPACES", "/workspaces"))
UPLOADS_DIR = Path(os.environ.get("PLAINBOARD_UPLOADS", "/uploads"))
# Logs of background jobs. In the permanent home, so they outlive a container rebuild.
JOBS_ROOT = Path(os.environ.get("PLAINBOARD_JOBS", Path.home() / ".plainboard" / "jobs"))
# Max characters of a tool result forwarded to the app (UI/transcript only, not the model).
TOOL_RESULT_PREVIEW_CHARS = 2000
