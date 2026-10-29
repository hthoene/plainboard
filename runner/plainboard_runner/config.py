import os
from pathlib import Path

WORKSPACES_ROOT = Path(os.environ.get("PLAINBOARD_WORKSPACES", "/workspaces"))
UPLOADS_DIR = Path(os.environ.get("PLAINBOARD_UPLOADS", "/uploads"))
# Max characters of a tool result forwarded to the app (UI/transcript only, not the model).
TOOL_RESULT_PREVIEW_CHARS = 2000
