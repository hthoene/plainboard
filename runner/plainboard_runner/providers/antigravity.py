"""Google Antigravity provider built on the Antigravity CLI's ACP server (``agy acp``: JSON-RPC over stdio).

Auth: a Google account signed in on the runner (browser OAuth link + pasted code/URL via
``agy acp --login``), or a Gemini API key (``gemini-api-key``). Every account
(``Credentials.antigravity_account``) has its own isolated ``GEMINI_HOME`` directory so one sign-in,
hook or MCP config never interferes with another.

Key operational safeguards distilled from T3 Code's Antigravity integration:
- Pinning & auto-update suppression (``AGY_CLI_DISABLE_AUTO_UPDATE=1``) and minimum version >= 1.5.0.
- Per-account file-backed OAuth storage (``SSH_CONNECTION`` + ``AGY_ACP_FORCE_FILE_STORAGE=1``) so
  multiple Google accounts never collide in a shared OS keyring entry.
- Per-process ``TMPDIR`` cleaned up on exit (the one-file binary unpacks ~1 GB per launch).
- Read-only SQLite inspection of ``conversations/<session_id>.db`` after every turn to catch
  upstream API errors (502, 429/QUOTA_EXHAUSTED, mid-turn 404, INVALID_THINKING_SIGNATURE) that
  Antigravity otherwise reports as ``end_turn``.
- Accurate Gemini 1.5+ token accounting (``inputTokens`` excludes ``cachedReadTokens``;
  ``outputTokens`` already includes ``thoughtTokens`` — never double-counted).
- Quota limits via ``v1internal:retrieveUserQuotaSummary`` (fallback ``fetchAvailableModels``) with
  the ``antigravity/hub/2.24.0`` User-Agent.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import logging
import os
import platform
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import urllib.parse
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from plainboard_protocol import (
    ACCOUNT_KEY_PATTERN,
    DEFAULT_ACCOUNT,
    AssistantTextEvent,
    CompactionEvent,
    CompletionResult,
    CompletionSpec,
    Credentials,
    LimitsEvent,
    LimitWindow,
    McpServerSpec,
    ModelInfo,
    ProviderAccount,
    ProviderInfo,
    ResultEvent,
    RunEvent,
    RunSpec,
    SessionEvent,
    TextDeltaEvent,
    ToolResultEvent,
    ToolUseEvent,
    UsageEvent,
)

from .. import notices
from ..config import TOOL_RESULT_PREVIEW_CHARS
from ..mcp_stdio import BRIDGE_FILE_ENV, write_bridge_file
from ..tool_bridge import ToolBridge
from ..workspace import tool_env
from .base import Provider

log = logging.getLogger(__name__)

ANTIGRAVITY_BIN_ENV = "PLAINBOARD_ANTIGRAVITY_BIN"
ACCOUNTS_HOME = Path.home() / ".plainboard" / "antigravity-accounts"
BRIDGE_DIR = Path(tempfile.gettempdir()) / "plainboard-bridge"
MCP_SERVER_NAME = "plainboard"
MAX_LINE_BYTES = 64 * 1024 * 1024
REQUEST_TIMEOUT = 120
STARTUP_TIMEOUT = 120
PROMPT_INACTIVITY_TIMEOUT = 15 * 60
NOTICE_GRACE_SECONDS = 60
NOTICE_CHECK_SECONDS = 2
LOGIN_TIMEOUT_SECONDS = 15 * 60
MODELS_TTL_SECONDS = 3600

MIN_RUNTIME_VERSION = (1, 5, 0)
OPEN_MODEL_ACCESS_VERSION = (1, 5, 1)
PROMPT_STEERING_VERSION = (1, 5, 0)

# Curated fallback list shown before any Google account signs in or when offline.
MODELS = [
    ModelInfo(id="gemini-4-neon", label="Gemini 4 Neon", tier="xlarge"),
    ModelInfo(id="gemini-4-carbon", label="Gemini 4 Carbon", tier="large"),
    ModelInfo(id="gemini-4-argon", label="Gemini 4 Argon", tier="medium"),
    ModelInfo(id="gemini-3.8-flash", label="Gemini 3.8 Flash", tier="small"),
]
EFFORTS = ["minimal", "low", "medium", "high", "xhigh", "max"]

# Ambient Google / Gemini / Antigravity variables stripped before every launch so host credentials
# never leak into an isolated account profile.
REMOVED_ENV_KEYS = frozenset(
    {
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_CLOUD_LOCATION",
        "GOOGLE_CLOUD_QUOTA_PROJECT",
        "GOOGLE_GENAI_USE_VERTEXAI",
        "GCLOUD_PROJECT",
        "CLOUDSDK_CORE_PROJECT",
        "AGY_ACP_CCPA_PROJECT",
        "AGY_ACP_ENABLE_OAUTH",
        "GEMINI_HOME",
        "AGY_ACP_FORCE_FILE_STORAGE",
        "AGY_CLI_DISABLE_AUTO_UPDATE",
        "BROWSER",
        "PYTHONUNBUFFERED",
        "ELECTRON_RUN_AS_NODE",
        "CHROME_CRASHPAD_PIPE_NAME",
        "DISPLAY",
        "WAYLAND_DISPLAY",
    }
)

CA_BUNDLE_CANDIDATES = (
    "/etc/ssl/certs/ca-bundle.crt",
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/etc/ssl/cert.pem",
)

# SQLite trajectory step types (AntigravityTurnOutcome.ts)
ERROR_STEP_TYPE = 17  # CORTEX_STEP_TYPE_ERROR_MESSAGE
PLANNER_RESPONSE_STEP_TYPE = 15  # CORTEX_STEP_TYPE_PLANNER_RESPONSE
MAX_ERROR_PAYLOAD_BYTES = 16_384
NATIVE_SESSION_ID_RE = re.compile(r"^[\w-]+$")

# Error & reset-time patterns from Antigravity's Go/Python harness
API_ERROR_RE = re.compile(
    r"Error (\d{3}), Message: ((?:(?!, Status: |Error \d{3}, Message: ).)+), Status: ([A-Z_]+|\d{3}[^,\n]*)",
    re.S,
)
STATUS_ERROR_RE = re.compile(r"(?<![A-Z_])([A-Z_]+) \(code (\d{3})\): ([^\x00-\x1f\x7f-\x9f]+)")
QUOTA_EXHAUSTED_RE = re.compile(r"\breason:QUOTA_EXHAUSTED\b")
INVALID_THINKING_SIGNATURE_RE = re.compile(
    r"\bmessages\.\d+\.content\.\d+(?:\.thinking\.signature: Field required|: Invalid `signature` in `thinking` block)"
)
RESET_TIMESTAMP_RE = re.compile(r"\bquotaResetTimeStamp:([^\s\]]+)")
RESET_DELAY_RE = re.compile(r"\bquotaResetDelay:((?:\d+(?:\.\d+)?(?:ns|us|µs|μs|ms|h|m|s))+)(?=[\s\]])")
RETRY_DELAY_RE = re.compile(r"\bretryDelay:((?:\d+(?:\.\d+)?(?:ns|us|µs|μs|ms|h|m|s))+)(?=[\s\]])")
GO_DURATION_PART_RE = re.compile(r"(\d+(?:\.\d+)?)(ns|us|µs|μs|ms|h|m|s)")
GO_DURATION_UNIT_SECONDS = {
    "ns": 1e-9,
    "us": 1e-6,
    "µs": 1e-6,
    "μs": 1e-6,
    "ms": 1e-3,
    "s": 1.0,
    "m": 60.0,
    "h": 3600.0,
}

# OAuth --login helpers
CODE_REDIRECT_URI = "https://antigravity.google/oauth-callback"
MAX_AUTH_URL_LEN = 16_384
BARE_LOGIN_CODE_RE = re.compile(r"^[\w./~-]{1,2048}$")
NATIVE_CLI_NO_BROWSER_PREFIX = "No browser is available here"
SIGN_IN_REQUIRED_MESSAGE = "Sign in to Antigravity in Settings before you continue."

# Quota limits probe
DAILY_QUOTA_ENDPOINT = "https://daily-cloudcode-pa.googleapis.com"
PROD_QUOTA_ENDPOINT = "https://cloudcode-pa.googleapis.com"
QUOTA_ENDPOINTS = (DAILY_QUOTA_ENDPOINT, PROD_QUOTA_ENDPOINT)
SUMMARY_WINDOW_SECONDS = {"5h": 5 * 3600, "weekly": 7 * 24 * 3600}

# Tool normalization
MCP_TITLE_RE = re.compile(r"^Run(?:ning)?\s+mcp_([A-Za-z0-9_-]+?)_([A-Za-z0-9_]+)\??$")
COMMAND_LINE_LIMIT = 8_000
COMMAND_TRUNCATED_SUFFIX = "\n\n[Command truncated]"
OUTPUT_TRUNCATED_PREFIX = "[Earlier output truncated]\n\n"
READ_ONLY_KINDS = frozenset({"read", "search", "think", "fetch"})


class AntigravityError(RuntimeError):
    pass


def resolve_binary() -> str:
    """Resolve the Antigravity CLI binary: env override, then ``agy`` or ``antigravity`` on PATH."""
    configured = os.environ.get(ANTIGRAVITY_BIN_ENV, "").strip()
    if configured:
        return os.path.expanduser(configured)
    for name in ("agy", "antigravity"):
        if found := shutil.which(name):
            return found
    return "agy"


def parse_version(version: str | None) -> tuple[int, int, int] | None:
    if not version:
        return None
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", version.strip())
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def guess_tier(model_id: str) -> str:
    low = model_id.lower()
    if any(k in low for k in ("neon", "astra", "fable", "ultra")):
        return "xlarge"
    if any(k in low for k in ("carbon", "pro", "opus", "sol")):
        return "large"
    if any(k in low for k in ("flash", "haiku", "mini", "nano", "luna", "lite")):
        return "small"
    return "medium"


def account_home(key: str, api_key: bool = False) -> Path:
    """Isolated ``GEMINI_HOME`` directory for one Antigravity account."""
    if not re.fullmatch(ACCOUNT_KEY_PATTERN, key):
        raise ValueError(f"Invalid account key: {key!r}")
    return ACCOUNTS_HOME / (f"{key}-api-key" if api_key else key)


def auth_method_for(creds: Credentials) -> str:
    return "gemini-api-key" if creds.antigravity_auth_mode == "api_key" else "oauth-personal"


def prepare_profile(home: Path, auth_method: str) -> Path:
    """Ensure the isolated profile directory and ``settings.json`` exist with 0700 permissions."""
    acp_dir = home / "antigravity-acp"
    for directory in (home, acp_dir):
        directory.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(directory, 0o700)
    settings_path = acp_dir / "settings.json"
    settings_path.write_text(json.dumps({"auth": {"type": auth_method}}) + "\n", encoding="utf-8")
    return acp_dir


def _find_ca_bundle(base_env: dict[str, str]) -> str | None:
    if sys.platform != "linux":
        return None
    if base_env.get("SSL_CERT_FILE") or base_env.get("SSL_CERT_DIR"):
        return None
    candidates = [base_env.get("NIX_SSL_CERT_FILE"), *CA_BUNDLE_CANDIDATES]
    for candidate in candidates:
        if candidate and os.path.isabs(candidate) and os.path.isfile(candidate):
            return candidate
    return None


def build_env(home: Path, creds: Credentials, base_env: dict[str, str], temp_dir: Path) -> dict[str, str]:
    """Build an isolated subprocess environment for ``agy acp`` (matching T3 Code's safeguards)."""
    env = {k: v for k, v in base_env.items() if k.upper() not in REMOVED_ENV_KEYS}
    auth_method = auth_method_for(creds)
    if auth_method == "gemini-api-key":
        if not creds.gemini_api_key:
            raise AntigravityError("Gemini API key is not configured (Settings → Antigravity).")
        env["GEMINI_API_KEY"] = creds.gemini_api_key
    else:
        # CLI 1.5+ ignores AGY_ACP_FORCE_FILE_STORAGE and writes to a single shared OS keyring entry
        # unless it believes it is running over SSH. Setting SSH_CONNECTION isolates every account's
        # OAuth token into <GEMINI_HOME>/antigravity-acp/antigravity-oauth-token.
        env["SSH_CONNECTION"] = base_env.get("SSH_CONNECTION") or "127.0.0.1 0 127.0.0.1 0"
    if ca := _find_ca_bundle(base_env):
        env["SSL_CERT_FILE"] = ca
    env["GEMINI_HOME"] = str(home)
    env["AGY_ACP_FORCE_FILE_STORAGE"] = "1"
    env["AGY_CLI_DISABLE_AUTO_UPDATE"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    if sys.platform == "win32":
        env["TEMP"] = str(temp_dir)
        env["TMP"] = str(temp_dir)
    else:
        env["TMPDIR"] = str(temp_dir)
    return env


def bound_command_line(text: str, limit: int = COMMAND_LINE_LIMIT) -> str:
    """Keep the START of a command line (program and target live at the head, not the tail)."""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}{COMMAND_TRUNCATED_SUFFIX}"


def bound_output_tail(text: str, limit: int = TOOL_RESULT_PREVIEW_CHARS) -> str:
    """Keep the TAIL of long tool output."""
    if len(text) <= limit:
        return text
    keep = max(0, limit - len(OUTPUT_TRUNCATED_PREFIX))
    return f"{OUTPUT_TRUNCATED_PREFIX}{text[-keep:]}" if keep else text[-limit:]


def external_mcp_config(server: McpServerSpec, *, v2: bool = False) -> dict[str, Any]:
    """Format an external MCP server for ACP ``session/new`` / ``session/resume``."""
    if server.transport == "stdio":
        entry: dict[str, Any] = {
            "name": server.name,
            "command": server.command or "",
            "args": list(server.args),
            "env": [{"name": k, "value": v} for k, v in server.env.items()],
        }
        if v2:
            entry["type"] = "stdio"
        return entry
    return {
        "type": "sse" if server.transport == "sse" else "http",
        "name": server.name,
        "url": server.url or "",
        "headers": [{"name": k, "value": v} for k, v in server.headers.items()],
    }


def mcp_servers_config(spec: RunSpec, bridge_file: Path, *, v2: bool = False) -> list[dict[str, Any]]:
    """PlainBoard stdio MCP bridge plus any external MCP servers configured for the agent."""
    servers = [external_mcp_config(s, v2=v2) for s in spec.mcp_servers if s.name != MCP_SERVER_NAME]
    pb: dict[str, Any] = {
        "name": MCP_SERVER_NAME,
        "command": sys.executable,
        "args": ["-m", "plainboard_runner.mcp_stdio"],
        "env": [{"name": BRIDGE_FILE_ENV, "value": str(bridge_file)}],
    }
    if v2:
        pb["type"] = "stdio"
    servers.append(pb)
    return servers


def supports_system_prompt_append(init_result: dict[str, Any]) -> bool:
    caps = init_result.get("agentCapabilities") or {}
    ext = (caps.get("_meta") or {}).get("antigravity")
    return isinstance(ext, dict) and isinstance(ext.get("systemPrompt"), dict) and ext["systemPrompt"].get("append") is True


def flatten_select_options(option: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten ACP ``SessionConfigSelectOptions`` (flat list or grouped option lists)."""
    out: list[dict[str, Any]] = []
    for entry in option.get("options") or []:
        if not isinstance(entry, dict):
            continue
        if "value" in entry:
            out.append(entry)
        elif isinstance(entry.get("options"), list):
            out.extend(sub for sub in entry["options"] if isinstance(sub, dict) and "value" in sub)
    return out


def find_config_option(config_options: list[dict[str, Any]] | None, *, option_id: str | None = None, category: str | None = None) -> dict[str, Any] | None:
    for opt in config_options or []:
        if not isinstance(opt, dict) or opt.get("type") != "select":
            continue
        if category and opt.get("category") == category:
            return opt
        if option_id and opt.get("id") == option_id:
            return opt
    return None


def models_from_setup(setup: dict[str, Any]) -> list[ModelInfo]:
    """Extract available models from an ACP ``session/new`` response."""
    known = {m.id: m for m in MODELS}
    config = find_config_option(setup.get("configOptions"), option_id="model", category="model")
    entries: list[tuple[str, str]] = []
    if config:
        for opt in flatten_select_options(config):
            val = str(opt.get("value") or "").strip()
            name = str(opt.get("name") or val).strip()
            if val:
                entries.append((val, name))
    else:
        for model in (setup.get("models") or {}).get("availableModels") or []:
            if isinstance(model, dict):
                val = str(model.get("modelId") or "").strip()
                name = str(model.get("name") or val).strip()
                if val:
                    entries.append((val, name))
    seen: set[str] = set()
    used_names: set[str] = set()
    result: list[ModelInfo] = []
    for val, base_name in entries:
        if val in seen:
            continue
        seen.add(val)
        label = f"{base_name} ({val})" if base_name in used_names else base_name
        used_names.add(base_name)
        result.append(known.get(val) or ModelInfo(id=val, label=label, tier=guess_tier(val)))
    return result


def match_thinking_level(effort: str | None, thinking_opt: dict[str, Any] | None) -> str | None:
    """Map PlainBoard's effort level to the closest value offered by the model's ``thought_level`` option."""
    if not effort or not thinking_opt:
        return None
    values = [str(opt.get("value") or "") for opt in flatten_select_options(thinking_opt) if opt.get("value")]
    if not values:
        return None
    by_lower = {v.lower(): v for v in values}
    want = effort.strip().lower()
    if want in by_lower:
        return by_lower[want]
    fallbacks: dict[str, tuple[str, ...]] = {
        "max": ("xhigh", "high", "medium"),
        "xhigh": ("max", "high", "medium"),
        "minimal": ("low", "off", "none"),
        "low": ("minimal", "medium"),
        "medium": ("high", "low"),
        "high": ("xhigh", "max", "medium"),
    }
    for candidate in fallbacks.get(want, ()):
        if candidate in by_lower:
            return by_lower[candidate]
    return None


def remove_session_files(home: Path, session_id: str | None) -> None:
    """Delete disposable session artifacts (summaries, model catalog probes) so they never accumulate."""
    if not session_id or not NATIVE_SESSION_ID_RE.fullmatch(session_id):
        return
    acp_dir = home / "antigravity-acp"
    base = acp_dir / "conversations" / session_id
    for suffix in (".db", ".db-wal", ".db-shm", ".db-journal", ".meta"):
        Path(f"{base}{suffix}").unlink(missing_ok=True)
    shutil.rmtree(acp_dir / "brain" / session_id, ignore_errors=True)
    (acp_dir / "annotations" / f"{session_id}.pbtxt").unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Post-turn SQLite session log inspection (T3 Code AntigravityTurnOutcome.ts)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TurnFailure:
    message: str
    is_quota: bool = False
    reset_at: int | None = None
    session_broken: bool = False
    retry_continue: bool = False


def parse_go_duration_seconds(duration: str) -> float:
    total = 0.0
    for amount, unit in GO_DURATION_PART_RE.findall(duration):
        total += float(amount) * GO_DURATION_UNIT_SECONDS.get(unit, 0.0)
    return total


def parse_iso_timestamp(value: str | None) -> int | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        return int(dt.timestamp())
    except ValueError:
        return None


def reset_at_from_error(text: str, now: float | None = None) -> int | None:
    if m := RESET_TIMESTAMP_RE.search(text):
        if ts := parse_iso_timestamp(m.group(1)):
            return ts
    delay_match = RESET_DELAY_RE.search(text) or RETRY_DELAY_RE.search(text)
    if delay_match and now is not None:
        seconds = parse_go_duration_seconds(delay_match.group(1))
        if seconds > 0:
            return int(now + seconds)
    return None


def _read_session_db(db_path: Path, fn):
    if not db_path.is_file() or db_path.is_symlink():
        return None
    uri = f"file:{urllib.parse.quote(str(db_path))}?mode=ro"
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=0.1)
        conn.execute("PRAGMA busy_timeout = 100")
        has_steps = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'steps'").fetchone()
        if not has_steps:
            return None
        return fn(conn)
    except sqlite3.Error:
        return None
    finally:
        if conn is not None:
            with contextlib.suppress(sqlite3.Error):
                conn.close()


def read_last_step_index(home: Path, session_id: str | None) -> int:
    if not session_id or not NATIVE_SESSION_ID_RE.fullmatch(session_id):
        return -1
    db_path = home / "antigravity-acp" / "conversations" / f"{session_id}.db"

    def _query(conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT max(idx) FROM steps").fetchone()
        return int(row[0]) if row and isinstance(row[0], int) else -1

    res = _read_session_db(db_path, _query)
    return res if isinstance(res, int) else -1


def read_last_step_after(home: Path, session_id: str | None, start_index: int) -> tuple[int, bytes, bool] | None:
    if not session_id or not NATIVE_SESSION_ID_RE.fullmatch(session_id):
        return None
    db_path = home / "antigravity-acp" / "conversations" / f"{session_id}.db"

    def _query(conn: sqlite3.Connection):
        row = conn.execute(
            "SELECT step_type, substr(step_payload, 1, ?) FROM steps WHERE idx > ? ORDER BY idx DESC LIMIT 1",
            (MAX_ERROR_PAYLOAD_BYTES, start_index),
        ).fetchone()
        if not row or not isinstance(row[0], int):
            return None
        reached = (
            conn.execute("SELECT 1 FROM steps WHERE idx > ? AND step_type = ? LIMIT 1", (start_index, PLANNER_RESPONSE_STEP_TYPE)).fetchone()
            is not None
        )
        payload = row[1] if isinstance(row[1], (bytes, bytearray)) else (str(row[1]).encode() if row[1] is not None else b"")
        return row[0], bytes(payload), reached

    return _read_session_db(db_path, _query)


def analyze_turn_outcome(stop_reason: str | None, last_step: tuple[int, bytes, bool] | None, now: float | None = None) -> TurnFailure | None:
    """Detect upstream API failures that Antigravity reports as ``end_turn`` + chat text."""
    error_text: str | None = None
    reached_model = False
    if last_step is not None:
        step_type, payload, reached_model = last_step
        if step_type == ERROR_STEP_TYPE:
            error_text = payload.decode("utf-8", errors="replace")
    if error_text is None and stop_reason != "refusal":
        return None
    text = error_text or ""
    api_match = API_ERROR_RE.search(text)
    status_match = None if api_match else STATUS_ERROR_RE.search(text)
    http_status = api_match.group(1) if api_match else (status_match.group(2) if status_match else None)
    message = api_match.group(2).strip() if api_match else (status_match.group(3).strip() if status_match else None)
    status_code = api_match.group(3).strip() if api_match else (status_match.group(1).strip() if status_match else None)

    if stop_reason == "refusal" or QUOTA_EXHAUSTED_RE.search(text):
        return TurnFailure(
            message="You have reached your Antigravity quota for this period.",
            is_quota=True,
            reset_at=reset_at_from_error(text, now),
        )
    if INVALID_THINKING_SIGNATURE_RE.search(text):
        return TurnFailure(
            message="Antigravity session can no longer be resumed (invalid thinking signature). The next turn starts a fresh session.",
            session_broken=True,
        )
    if http_status is None:
        return TurnFailure(message="Antigravity stopped this turn because of an error.")
    rate_limited = http_status == "429" or status_code == "RESOURCE_EXHAUSTED"
    if rate_limited:
        return TurnFailure(
            message=message or "Antigravity rate limit reached.",
            is_quota=True,
            reset_at=reset_at_from_error(text, now),
        )
    transient_404 = http_status == "404" and reached_model
    return TurnFailure(
        message=message or f"Antigravity error ({http_status} {status_code or ''}).".strip(),
        retry_continue=transient_404,
    )


# ---------------------------------------------------------------------------
# OAuth URL & code validation (T3 Code antigravityAuthSupport.ts)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PendingAuth:
    authorization_url: str
    redirect_uri: str
    state: str


def parse_authorization_url(raw_url: str) -> PendingAuth:
    """Validate a Google OAuth URL printed by ``agy acp --login``."""
    url_str = raw_url.strip()
    if not url_str or len(url_str) > MAX_AUTH_URL_LEN or any(c.isspace() for c in url_str):
        raise AntigravityError("Antigravity returned an invalid Google sign-in URL.")
    parsed = urllib.parse.urlsplit(url_str)
    params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    state_list = params.get("state") or []
    redirect_list = params.get("redirect_uri") or []
    response_type = params.get("response_type") or []
    if (
        parsed.scheme != "https"
        or parsed.netloc != "accounts.google.com"
        or parsed.path not in ("/o/oauth2/auth", "/o/oauth2/v2/auth")
        or parsed.username
        or parsed.password
        or parsed.fragment
        or len(state_list) != 1
        or not state_list[0]
        or len(state_list[0]) > 512
        or any(c.isspace() for c in state_list[0])
        or len(redirect_list) != 1
        or redirect_list[0] != CODE_REDIRECT_URI
        or response_type != ["code"]
    ):
        raise AntigravityError("Antigravity returned an invalid Google sign-in URL.")
    return PendingAuth(authorization_url=url_str, redirect_uri=redirect_list[0], state=state_list[0])


def validate_login_response(pending: PendingAuth, response: str) -> str:
    """Validate the code or full Antigravity callback URL pasted by the user."""
    value = response.strip()
    if not value or len(value) > MAX_AUTH_URL_LEN or any(c.isspace() for c in value):
        raise ValueError("Paste the code from the Antigravity page, or that page's full URL.")
    if BARE_LOGIN_CODE_RE.fullmatch(value):
        return value
    parsed = urllib.parse.urlsplit(value)
    params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    base = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    if (
        base != pending.redirect_uri
        or parsed.username
        or parsed.password
        or params.get("state") != [pending.state]
        or len(params.get("code") or []) != 1
        or not params["code"][0]
    ):
        raise ValueError("Paste the code from the Antigravity page, or that page's full URL.")
    return value


# ---------------------------------------------------------------------------
# Quota / limits probe (T3 Code antigravityUsageLimits.ts)
# ---------------------------------------------------------------------------


def quota_user_agent() -> str:
    os_name = "linux" if sys.platform.startswith("linux") else "darwin" if sys.platform == "darwin" else "win32"
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
    return f"antigravity/hub/2.24.0 (aidev_client; os_type={os_name}; arch={arch}; cl=963137146)"


def _summary_reset_ts(bucket: dict[str, Any], window_seconds: int | None, requested_at: float) -> int | None:
    reset_ts = parse_iso_timestamp(bucket.get("resetTime"))
    if reset_ts is None:
        return None
    # Untouched summary buckets report a rolling full-window reset; omit it so the UI doesn't show noise.
    if (
        bucket.get("remainingFraction") == 1
        and bucket.get("description") is None
        and window_seconds is not None
        and abs(reset_ts - requested_at - window_seconds) <= 60
    ):
        return None
    return reset_ts


def _bucket_window_name(bucket_id: str, window_kind: str) -> str:
    suffix = "seven_day" if window_kind == "weekly" else "five_hour" if window_kind == "5h" else (window_kind or "quota")
    if bucket_id.startswith("gemini-argon-"):
        family = "argon"
    elif bucket_id.startswith("gemini-carbon-"):
        family = "carbon"
    elif bucket_id.startswith("gemini-"):
        family = "gemini"
    elif bucket_id.startswith("3p-"):
        family = "shared"
    else:
        family = re.sub(r"[^a-z0-9]+", "_", bucket_id.lower()).strip("_") or "quota"
        return f"antigravity_{family}"
    return f"antigravity_{family}_{suffix}"


def parse_summary_windows(response: dict[str, Any], requested_at: float) -> list[LimitWindow]:
    """Parse ``v1internal:retrieveUserQuotaSummary`` into PlainBoard ``LimitWindow`` objects."""
    groups = response.get("groups") or []
    if not any(isinstance(g, dict) and g.get("buckets") for g in groups):
        groups = [{"buckets": response.get("buckets") or []}]
    windows: dict[str, LimitWindow] = {}
    for group in groups:
        if not isinstance(group, dict):
            continue
        for bucket in group.get("buckets") or []:
            if not isinstance(bucket, dict) or bucket.get("disabled"):
                continue
            bucket_id = str(bucket.get("bucketId") or "").strip()
            remaining = bucket.get("remainingFraction")
            if not bucket_id or not isinstance(remaining, (int, float)):
                continue
            used = round(1.0 - max(0.0, min(1.0, float(remaining))), 4)
            window_kind = str(bucket.get("window") or "")
            name = _bucket_window_name(bucket_id, window_kind)
            resets_at = _summary_reset_ts(bucket, SUMMARY_WINDOW_SECONDS.get(window_kind), requested_at)
            windows[name] = LimitWindow(
                name=name,
                utilization=used,
                resets_at=resets_at,
                status="rejected" if used >= 1.0 else None,
            )
    return list(windows.values())


def parse_model_quota_windows(response: dict[str, Any]) -> list[LimitWindow]:
    """Fallback parser for ``v1internal:fetchAvailableModels`` when summary buckets are absent."""
    models = response.get("models")
    if not isinstance(models, dict):
        return []
    windows: dict[str, LimitWindow] = {}
    for model_id, info in models.items():
        if not isinstance(info, dict):
            continue
        provider = info.get("modelProvider")
        if provider == "MODEL_PROVIDER_GOOGLE" or model_id.startswith("gemini-"):
            family = "gemini"
        elif provider == "MODEL_PROVIDER_ANTHROPIC" or model_id.startswith("claude-"):
            family = "claude"
        elif provider == "MODEL_PROVIDER_OPENAI" or model_id.startswith("gpt-"):
            family = "gpt"
        else:
            continue
        for suffix, key in (("quota", "quotaInfo"), ("seven_day", "weeklyQuotaInfo")):
            quota = info.get(key)
            if not isinstance(quota, dict):
                continue
            remaining = quota.get("remainingFraction")
            if not isinstance(remaining, (int, float)):
                continue
            used = round(1.0 - max(0.0, min(1.0, float(remaining))), 4)
            name = f"antigravity_{family}_{suffix}"
            win = LimitWindow(
                name=name,
                utilization=used,
                resets_at=parse_iso_timestamp(quota.get("resetTime")),
                status="rejected" if used >= 1.0 else None,
            )
            prev = windows.get(name)
            if prev is None or win.utilization > prev.utilization:
                windows[name] = win
    return list(windows.values())


async def read_stored_access_token(home: Path, client: httpx.AsyncClient) -> tuple[str, str | None] | None:
    """Read an access token from CLI 1.5 ``antigravity-oauth-token`` or legacy ``acp_token.json``."""
    acp_dir = home / "antigravity-acp"
    token_json = acp_dir / "acp_token.json"
    cli_token = acp_dir / "antigravity-oauth-token"
    now = time.time()
    if token_json.is_file():
        try:
            data = json.loads(token_json.read_text(encoding="utf-8"))
            resp = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": data["client_id"],
                    "client_secret": data["client_secret"],
                    "refresh_token": data["refresh_token"],
                    "grant_type": "refresh_token",
                },
                timeout=10,
            )
            resp.raise_for_status()
            return str(resp.json()["access_token"]), data.get("project_id")
        except Exception:
            return None
    if cli_token.is_file():
        try:
            data = json.loads(cli_token.read_text(encoding="utf-8"))
            tok = data.get("token") or {}
            access = tok.get("access_token")
            expiry = parse_iso_timestamp(tok.get("expiry"))
            if access and (expiry is None or expiry > now + 60):
                return str(access), None
        except Exception:
            return None
    return None


def has_stored_oauth_login(home: Path) -> bool:
    acp_dir = home / "antigravity-acp"
    for path in (acp_dir / "antigravity-oauth-token", acp_dir / "acp_token.json"):
        if path.is_file() and path.stat().st_size > 2:
            return True
    return False


# ---------------------------------------------------------------------------
# ACP stdio JSON-RPC client
# ---------------------------------------------------------------------------


class AcpServer:
    """One ``agy acp`` process communicating via ACP JSON-RPC 2.0 over stdio."""

    def __init__(self, home: Path, creds: Credentials, env: dict[str, str] | None = None, cwd: Path | None = None) -> None:
        self.home = home
        self.creds = creds
        self.extra_env = env or {}
        self.cwd = cwd
        self.stderr_tail: collections.deque[str] = collections.deque(maxlen=40)
        self._proc: asyncio.subprocess.Process | None = None
        self._temp_dir: tempfile.TemporaryDirectory[str] | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._messages: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._ids = 0
        self._tasks: list[asyncio.Task[None]] = []
        self.init_result: dict[str, Any] = {}
        self.is_v2: bool = False
        self.version: tuple[int, int, int] | None = None

    async def __aenter__(self) -> AcpServer:
        prepare_profile(self.home, auth_method_for(self.creds))
        self._temp_dir = tempfile.TemporaryDirectory(prefix="plainboard-agy-tmp-")
        merged_base = {**os.environ, **self.extra_env}
        env = build_env(self.home, self.creds, merged_base, Path(self._temp_dir.name))
        binary = resolve_binary()
        args = [binary, "acp", "--gemini-dir", str(self.home), "--app-data-dir", "antigravity-acp"]
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=str(self.cwd) if self.cwd else None,
                limit=MAX_LINE_BYTES,
            )
        except FileNotFoundError as exc:
            self._cleanup_temp()
            raise AntigravityError("The Antigravity CLI (agy) is not installed in the runner.") from exc
        self._tasks = [asyncio.create_task(self._read()), asyncio.create_task(self._read_stderr())]
        try:
            init = await self.request(
                "initialize",
                {
                    "protocolVersion": 1,
                    "clientCapabilities": {
                        "fs": {"readTextFile": False, "writeTextFile": False},
                        "terminal": False,
                    },
                    "clientInfo": {"name": "plainboard", "title": "PlainBoard", "version": "0.1"},
                },
                timeout=STARTUP_TIMEOUT,
            )
            self.init_result = init if isinstance(init, dict) else {}
            self.is_v2 = isinstance(self.init_result.get("capabilities"), dict) and isinstance(self.init_result.get("info"), dict)
            info = self.init_result.get("info") if self.is_v2 else self.init_result.get("agentInfo")
            raw_ver = info.get("version") if isinstance(info, dict) else None
            self.version = parse_version(raw_ver)
            if self.version is not None and self.version < MIN_RUNTIME_VERSION:
                raise AntigravityError(
                    f"Antigravity runtime {raw_ver} is below the minimum supported version "
                    f"{'.'.join(str(x) for x in MIN_RUNTIME_VERSION)}. Update the Antigravity CLI."
                )
        except BaseException:
            await self.close()
            raise
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    def _cleanup_temp(self) -> None:
        if self._temp_dir is not None:
            with contextlib.suppress(Exception):
                self._temp_dir.cleanup()
            self._temp_dir = None

    async def close(self) -> None:
        proc = self._proc
        if proc and proc.returncode is None:
            with contextlib.suppress(Exception):
                proc.stdin.close()  # type: ignore[union-attr]
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except TimeoutError:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except TimeoutError:
                    proc.kill()
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(proc.wait(), timeout=5)
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
        self._cleanup_temp()

    def stderr(self) -> str:
        return "\n".join(self.stderr_tail)

    async def _read(self) -> None:
        assert self._proc and self._proc.stdout
        try:
            while line := await self._proc.stdout.readline():
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                if "method" not in message and "id" in message:
                    future = self._pending.pop(message["id"], None)
                    if future and not future.done():
                        if "error" in message and message["error"] is not None:
                            err = message["error"]
                            code = err.get("code") if isinstance(err, dict) else None
                            msg = (err.get("message") if isinstance(err, dict) else str(err)) or "Antigravity request failed"
                            if code == -32000 or (code == -32603 and msg.startswith(NATIVE_CLI_NO_BROWSER_PREFIX)):
                                msg = SIGN_IN_REQUIRED_MESSAGE
                            exc = AntigravityError(msg)
                            exc.rpc_code = code  # type: ignore[attr-defined]
                            future.set_exception(exc)
                        else:
                            future.set_result(message.get("result"))
                else:
                    await self._messages.put(message)
        finally:
            error = AntigravityError(f"Antigravity exited\n{self.stderr()}".strip())
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(error)
            self._pending.clear()
            await self._messages.put(None)

    async def _read_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        while line := await self._proc.stderr.readline():
            self.stderr_tail.append(line.decode(errors="replace").rstrip())

    async def _send(self, message: dict[str, Any]) -> None:
        assert self._proc and self._proc.stdin
        self._proc.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
        await self._proc.stdin.drain()

    def start_request(self, method: str, params: dict[str, Any] | None = None) -> asyncio.Future[Any]:
        self._ids += 1
        req_id = self._ids
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = future
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            payload["params"] = params

        async def _write() -> None:
            try:
                await self._send(payload)
            except BaseException as exc:
                self._pending.pop(req_id, None)
                if not future.done():
                    future.set_exception(exc)

        asyncio.create_task(_write())
        return future

    async def request(self, method: str, params: dict[str, Any] | None = None, timeout: float = REQUEST_TIMEOUT) -> Any:
        future = self.start_request(method, params)
        return await asyncio.wait_for(future, timeout)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        await self._send(payload)

    async def respond(self, request_id: Any, result: Any = None, error: str | None = None, code: int = -32601) -> None:
        if error is not None:
            await self._send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": error}})
        else:
            await self._send({"jsonrpc": "2.0", "id": request_id, "result": result})

    async def next(self, timeout: float | None = None) -> dict[str, Any] | None:
        return await asyncio.wait_for(self._messages.get(), timeout)

    def drain_nowait(self) -> list[dict[str, Any] | None]:
        items: list[dict[str, Any] | None] = []
        while True:
            try:
                items.append(self._messages.get_nowait())
            except asyncio.QueueEmpty:
                return items

    async def authenticate(self) -> None:
        method = "auth/login" if self.is_v2 else "authenticate"
        await self.request(method, {"methodId": auth_method_for(self.creds)}, timeout=STARTUP_TIMEOUT)

    async def set_config_option(self, session_id: str, config_id: str, value: str) -> dict[str, Any]:
        params: dict[str, Any] = {"sessionId": session_id, "configId": config_id, "value": value}
        if self.is_v2:
            params["type"] = "id"
        res = await self.request("session/set_config_option", params)
        return res if isinstance(res, dict) else {}


# ---------------------------------------------------------------------------
# Permission & server-request handling
# ---------------------------------------------------------------------------


def _is_plainboard_mcp_call(tool_call: dict[str, Any]) -> bool:
    raw = tool_call.get("rawInput")
    if isinstance(raw, dict) and raw.get("ServerName") == MCP_SERVER_NAME:
        return True
    title = str(tool_call.get("title") or "").strip()
    m = MCP_TITLE_RE.match(title)
    return bool(m and m.group(1) == MCP_SERVER_NAME)


def _pick_option(options: list[dict[str, Any]], *kinds: str) -> str | None:
    for want in kinds:
        for opt in options:
            if isinstance(opt, dict) and opt.get("kind") == want and str(opt.get("optionId") or "").strip():
                return str(opt["optionId"])
    return None


async def answer_server_request(server: AcpServer, message: dict[str, Any], *, toolset: str = "full", deny_all: bool = False) -> None:
    """Answer agent-initiated JSON-RPC requests (permissions, elicitations) according to ``toolset``."""
    method = message.get("method")
    req_id = message.get("id")
    params = message.get("params") or {}
    if method == "session/request_permission":
        tool_call = params.get("toolCall") or {}
        tool_id = str(tool_call.get("toolCallId") or "")
        options = params.get("options") or []
        # Native user-input questions share request_permission with interaction_* IDs: cancel headless.
        if tool_id.startswith("interaction_"):
            await server.respond(req_id, {"outcome": {"outcome": "cancelled"}})
            return
        allowed = not deny_all
        if allowed and toolset == "read":
            kind = str(tool_call.get("kind") or "")
            allowed = kind in READ_ONLY_KINDS or _is_plainboard_mcp_call(tool_call)
        if allowed:
            opt_id = _pick_option(options, "allow_always", "allow_once")
            if opt_id:
                await server.respond(req_id, {"outcome": {"outcome": "selected", "optionId": opt_id}})
                return
        reject_id = _pick_option(options, "reject_once", "reject_always")
        if reject_id:
            await server.respond(req_id, {"outcome": {"outcome": "selected", "optionId": reject_id}})
        else:
            await server.respond(req_id, {"outcome": {"outcome": "cancelled"}})
    elif method == "elicitation/create":
        await server.respond(req_id, {"action": "decline"})
    else:
        await server.respond(req_id, error=f"PlainBoard does not handle {method}")


# ---------------------------------------------------------------------------
# ACP notification -> PlainBoard RunEvent mapper
# ---------------------------------------------------------------------------


def _extract_tool_identity(update: dict[str, Any], tracked: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Map an Antigravity ACP tool call into PlainBoard's normalized ``(name, input)``."""
    kind = str(update.get("kind") or tracked.get("kind") or "")
    title = str(update.get("title") or tracked.get("title") or "").strip()
    raw = update.get("rawInput") if isinstance(update.get("rawInput"), dict) else tracked.get("rawInput")
    raw_dict = raw if isinstance(raw, dict) else {}
    meta = update.get("_meta") if isinstance(update.get("_meta"), dict) else tracked.get("_meta")
    meta_dict = meta if isinstance(meta, dict) else {}

    # 1. MCP tool calls (T3 Code PR #39474: show mcp__<server>__<tool>, never "Ran command")
    if isinstance(raw_dict.get("ServerName"), str) and isinstance(raw_dict.get("ToolName"), str):
        args = raw_dict.get("Arguments") or raw_dict.get("arguments")
        return f"mcp__{raw_dict['ServerName']}__{raw_dict['ToolName']}", args if isinstance(args, dict) else {}
    if m := MCP_TITLE_RE.match(title):
        return f"mcp__{m.group(1)}__{m.group(2)}", raw_dict
    if meta_dict.get("is_mcp_tool_call") is True and title:
        return title, raw_dict

    # 2. Shell / command execution (T3 Code PR #40702: keep command HEAD, not tail)
    cmd = (
        raw_dict.get("CommandLine")
        or raw_dict.get("command_line")
        or raw_dict.get("commandLine")
        or raw_dict.get("command")
    )
    if isinstance(cmd, list):
        cmd = " ".join(str(part) for part in cmd)
    if kind == "execute" or isinstance(cmd, str):
        command_str = bound_command_line(str(cmd).strip()) if isinstance(cmd, str) and cmd.strip() else bound_command_line(title)
        return "Bash", {"command": command_str}

    # 3. File edits / writes (populate Edit/Write shape so PlainBoard renders native diffs)
    if kind in ("edit", "delete", "move"):
        content = update.get("content") or tracked.get("content") or []
        for entry in content if isinstance(content, list) else []:
            if isinstance(entry, dict) and entry.get("type") == "diff" and isinstance(entry.get("path"), str):
                if entry.get("oldText") is None and isinstance(entry.get("newText"), str):
                    return "Write", {"file_path": entry["path"], "content": entry["newText"]}
                return "Edit", {
                    "file_path": entry["path"],
                    "old_string": str(entry.get("oldText") or ""),
                    "new_string": str(entry.get("newText") or ""),
                }
        path = raw_dict.get("TargetFile") or raw_dict.get("path") or raw_dict.get("file_path") or raw_dict.get("filePath")
        if isinstance(path, str):
            if "CodeContent" in raw_dict:
                return "Write", {"file_path": path, "content": str(raw_dict["CodeContent"])}
            if "TargetContent" in raw_dict and "ReplacementContent" in raw_dict:
                return "Edit", {
                    "file_path": path,
                    "old_string": str(raw_dict["TargetContent"]),
                    "new_string": str(raw_dict["ReplacementContent"]),
                }
            return "Edit", {"file_path": path}
        locs = update.get("locations") or tracked.get("locations") or []
        files = [loc["path"] for loc in locs if isinstance(loc, dict) and isinstance(loc.get("path"), str)]
        return "Edit", {"files": files} if files else raw_dict

    # 4. Read / Search / Fetch
    if kind == "read":
        path = raw_dict.get("AbsolutePath") or raw_dict.get("absolute_path") or raw_dict.get("path") or raw_dict.get("file_path")
        if not path:
            locs = update.get("locations") or tracked.get("locations") or []
            if locs and isinstance(locs[0], dict):
                path = locs[0].get("path")
        return "Read", {"file_path": str(path)} if path else raw_dict
    if kind == "fetch":
        return "WebFetch", raw_dict
    if kind == "search":
        return "Search", raw_dict
    return title or kind or "Tool", raw_dict


def _extract_tool_output(update: dict[str, Any]) -> str:
    parts: list[str] = []
    content = update.get("content")
    if isinstance(content, list):
        for entry in content:
            if not isinstance(entry, dict):
                continue
            if entry.get("type") == "content":
                inner = entry.get("content")
                if isinstance(inner, dict) and inner.get("type") == "text" and inner.get("text"):
                    parts.append(str(inner["text"]))
            elif entry.get("type") == "diff" and entry.get("path"):
                parts.append(f"updated {entry['path']}")
    raw = update.get("rawOutput")
    if not parts and raw is not None:
        if isinstance(raw, str):
            parts.append(raw)
        elif isinstance(raw, dict):
            text = raw.get("combinedOutput") or raw.get("combined_output") or raw.get("output") or raw.get("content") or raw.get("stdout")
            if isinstance(text, str) and text:
                parts.append(text)
            exit_code = raw.get("exitCode") if "exitCode" in raw else raw.get("exit_code")
            if exit_code not in (None, 0):
                parts.append(f"[exit code {exit_code}]")
    return bound_output_tail("\n".join(parts).strip())


class EventMapper:
    """Translates ACP ``session/update`` notifications of one session into PlainBoard ``RunEvent``s."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.context_tokens = 0
        self.last_text: str | None = None
        self.model_calls = 0
        self.open_tools: set[str] = set()
        self._emitted_tools: set[str] = set()
        self._finished_tools: set[str] = set()
        self._tracked_tools: dict[str, dict[str, Any]] = {}
        self._text_buf: list[str] = []
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read_tokens = 0
        self.cache_creation_tokens = 0

    def flush_text(self) -> list[RunEvent]:
        if not self._text_buf:
            return []
        text = "".join(self._text_buf)
        self._text_buf.clear()
        if text.strip():
            self.last_text = text
            return [AssistantTextEvent(text=text)]
        return []

    def add_prompt_usage(self, usage: dict[str, Any] | None) -> None:
        """Accumulate Antigravity 1.5+ per-prompt usage (no double-counting of thoughtTokens)."""
        if not isinstance(usage, dict):
            return
        # T3 Code PR #39686 & adapter.ts (geminiPromptUsage):
        # - inputTokens already excludes cachedReadTokens (matches PlainBoard ResultEvent.input_tokens)
        # - outputTokens already includes thoughtTokens (do NOT add thoughtTokens again)
        self.input_tokens += max(0, int(usage.get("inputTokens") or 0))
        self.cache_read_tokens += max(0, int(usage.get("cachedReadTokens") or 0))
        self.cache_creation_tokens += max(0, int(usage.get("cachedWriteTokens") or 0))
        self.output_tokens += max(0, int(usage.get("outputTokens") or 0))
        self.model_calls += 1

    def map(self, method: str, params: dict[str, Any]) -> list[RunEvent]:
        if method != "session/update":
            return []
        if params.get("sessionId") not in (None, self.session_id):
            return []
        upd = params.get("update")
        if not isinstance(upd, dict):
            return []
        kind = upd.get("sessionUpdate")
        events: list[RunEvent] = []

        if kind == "agent_message_chunk":
            content = upd.get("content")
            if isinstance(content, dict) and content.get("type") == "text":
                chunk = content.get("text") or ""
                if chunk:
                    self._text_buf.append(chunk)
                    events.append(TextDeltaEvent(text=chunk))

        elif kind in ("tool_call", "tool_call_update"):
            tool_id = str(upd.get("toolCallId") or "").strip()
            if not tool_id:
                return []
            tracked = self._tracked_tools.setdefault(tool_id, {})
            for field in ("title", "kind", "rawInput", "content", "locations", "_meta"):
                if upd.get(field) is not None:
                    tracked[field] = upd[field]
            status = str(upd.get("status") or ("pending" if kind == "tool_call" else ""))
            if tool_id not in self._emitted_tools:
                events.extend(self.flush_text())
                name, tool_input = _extract_tool_identity(upd, tracked)
                self._emitted_tools.add(tool_id)
                self.open_tools.add(tool_id)
                events.append(ToolUseEvent(id=tool_id, name=name, input=tool_input))
            if status in ("completed", "failed", "cancelled") and tool_id not in self._finished_tools:
                self._finished_tools.add(tool_id)
                self.open_tools.discard(tool_id)
                events.append(
                    ToolResultEvent(
                        tool_use_id=tool_id,
                        content=_extract_tool_output(upd),
                        is_error=status != "completed",
                    )
                )

        elif kind == "usage_update":
            used = int(upd.get("used") or 0)
            if used > 0:
                self.context_tokens = used
                events.append(UsageEvent(context_tokens=used))

        elif kind == "compaction_update" and upd.get("status") == "completed":
            events.extend(self.flush_text())
            events.append(CompactionEvent(tokens_before=self.context_tokens))

        return events

    def result(self, *, is_error: bool = False, error: str | None = None, session_id: str | None = None) -> ResultEvent:
        return ResultEvent(
            session_id=session_id if session_id is not None else self.session_id,
            is_error=is_error,
            error=error if is_error else None,
            text=None if is_error else self.last_text,
            num_turns=self.model_calls or (1 if self.last_text or self._emitted_tools else 0),
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_read_tokens=self.cache_read_tokens,
            cache_creation_tokens=self.cache_creation_tokens,
            context_tokens=self.context_tokens,
        )


# ---------------------------------------------------------------------------
# Turn runner with steering, urgent interrupt, and SQLite outcome check
# ---------------------------------------------------------------------------


class TurnRunner:
    """Runs one PlainBoard turn on an Antigravity ACP session."""

    def __init__(self, server: AcpServer, mapper: EventMapper, spec: RunSpec, home: Path, initial_prompt: str) -> None:
        self.server = server
        self.mapper = mapper
        self.spec = spec
        self.home = home
        self.initial_prompt = initial_prompt
        self.interrupted = False
        self._steers: list[asyncio.Future[Any]] = []

    async def _deliver_notices(self) -> None:
        run_id = self.spec.run_id
        if not notices.pending(run_id) or self.interrupted:
            return
        if self.mapper.open_tools:
            if notices.urgent_waiting(run_id) >= NOTICE_GRACE_SECONDS:
                self.interrupted = True
                try:
                    await self.server.notify("session/cancel", {"sessionId": self.mapper.session_id})
                except Exception:
                    self.interrupted = False
                    log.warning("Could not cancel Antigravity run %s", run_id, exc_info=True)
            return
        # agy 1.5.0+ supports steering an active turn with a follow-up session/prompt without killing subagents.
        if self.server.version is None or self.server.version >= PROMPT_STEERING_VERSION:
            pending = notices.drain(run_id)
            try:
                fut = self.server.start_request(
                    "session/prompt",
                    {"sessionId": self.mapper.session_id, "prompt": [{"type": "text", "text": "\n\n".join(pending)}]},
                )
                self._steers.append(fut)
            except Exception:
                for text in pending:
                    notices.push(run_id, text)

    def _resume_prompt(self) -> str:
        self.interrupted = False
        self.mapper.open_tools.clear()
        return "\n\n".join(
            [
                "[PlainBoard] Your running tool call was interrupted for this message, and its work may be half done.",
                *notices.drain(self.spec.run_id),
            ]
        )

    async def _run_prompt(self, prompt_text: str) -> AsyncIterator[RunEvent | tuple[str, dict[str, Any]]]:
        """Execute one ``session/prompt`` while streaming notifications and handling server requests."""
        prompt_fut = self.server.start_request(
            "session/prompt",
            {"sessionId": self.mapper.session_id, "prompt": [{"type": "text", "text": prompt_text}]},
        )
        last_activity = time.monotonic()
        next_task: asyncio.Task[dict[str, Any] | None] | None = None
        try:
            while not prompt_fut.done():
                if next_task is None:
                    next_task = asyncio.create_task(self.server.next())
                done, _ = await asyncio.wait(
                    {prompt_fut, next_task},
                    timeout=NOTICE_CHECK_SECONDS,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if next_task in done:
                    msg = next_task.result()
                    next_task = None
                    last_activity = time.monotonic()
                    if msg is None:
                        raise AntigravityError(f"Antigravity exited during the turn\n{self.server.stderr()}".strip())
                    if "id" in msg:
                        await answer_server_request(self.server, msg, toolset=self.spec.toolset)
                    else:
                        method, payload = msg.get("method", ""), msg.get("params") or {}
                        for event in self.mapper.map(method, payload):
                            yield event
                        if method == "session/update" and (payload.get("update") or {}).get("sessionUpdate") == "tool_call_update":
                            await self._deliver_notices()
                else:
                    await self._deliver_notices()
                    if not self.mapper.open_tools and time.monotonic() - last_activity > PROMPT_INACTIVITY_TIMEOUT:
                        raise AntigravityError("Antigravity turn timed out waiting for a response.")
        finally:
            if next_task is not None and not next_task.done():
                next_task.cancel()
                with contextlib.suppress(BaseException):
                    await next_task

        # Drain any trailing notifications queued right before prompt_fut resolved.
        for msg in self.server.drain_nowait():
            if msg is None:
                continue
            if "id" in msg:
                await answer_server_request(self.server, msg, toolset=self.spec.toolset)
            else:
                for event in self.mapper.map(msg.get("method", ""), msg.get("params") or {}):
                    yield event

        resp = prompt_fut.result()
        for steer_fut in self._steers:
            if not steer_fut.done():
                with contextlib.suppress(Exception):
                    steer_resp = await asyncio.wait_for(steer_fut, timeout=30)
                    if isinstance(steer_resp, dict):
                        self.mapper.add_prompt_usage(steer_resp.get("usage"))
            elif not steer_fut.cancelled() and steer_fut.exception() is None:
                steer_resp = steer_fut.result()
                if isinstance(steer_resp, dict):
                    self.mapper.add_prompt_usage(steer_resp.get("usage"))
        self._steers.clear()
        yield ("_prompt_done", resp if isinstance(resp, dict) else {})

    async def run(self) -> AsyncIterator[RunEvent]:
        prompt = self.initial_prompt
        retried_404 = False
        while True:
            start_idx = read_last_step_index(self.home, self.mapper.session_id)
            resp: dict[str, Any] = {}
            async for item in self._run_prompt(prompt):
                if isinstance(item, tuple) and item[0] == "_prompt_done":
                    resp = item[1]
                else:
                    yield item  # type: ignore[misc]

            self.mapper.add_prompt_usage(resp.get("usage"))
            stop_reason = str(resp.get("stopReason") or "end_turn")

            if stop_reason == "cancelled" and self.interrupted:
                for event in self.mapper.flush_text():
                    yield event
                prompt = self._resume_prompt()
                continue

            # Check the native SQLite session log for errors Antigravity reports as end_turn.
            last_step = read_last_step_after(self.home, self.mapper.session_id, start_idx)
            failure = analyze_turn_outcome(stop_reason, last_step, now=time.time())
            if failure is not None:
                if failure.retry_continue and not retried_404:
                    retried_404 = True
                    for event in self.mapper.flush_text():
                        yield event
                    prompt = "continue"
                    continue
                # Discard buffered error text so API error chatter isn't stored as the agent's answer.
                self.mapper._text_buf.clear()
                if failure.is_quota:
                    yield LimitsEvent(
                        windows=[
                            LimitWindow(
                                name="antigravity_quota",
                                utilization=1.0,
                                resets_at=failure.reset_at,
                                status="rejected",
                            )
                        ]
                    )
                yield self.mapper.result(
                    is_error=True,
                    error=failure.message,
                    session_id="" if failure.session_broken else self.mapper.session_id,
                )
                return

            for event in self.mapper.flush_text():
                yield event

            if stop_reason == "cancelled":
                yield self.mapper.result(is_error=True, error="Turn cancelled")
                return

            yield self.mapper.result()
            return


# ---------------------------------------------------------------------------
# Headless Google OAuth sign-in via ``agy acp --login`` (T3 Code AntigravityDriver.ts)
# ---------------------------------------------------------------------------


class CodeLogin:
    """Headless Google OAuth sign-in via ``agy acp --login``: prints URL, accepts pasted code/URL."""

    def __init__(self, home: Path) -> None:
        self.home = home
        self.proc: asyncio.subprocess.Process | None = None
        self.temp_dir: tempfile.TemporaryDirectory[str] | None = None
        self.pending: PendingAuth | None = None
        self.error: str | None = None
        self.started: float = 0.0
        self._wait_task: asyncio.Task[None] | None = None
        self._stderr_tail: collections.deque[str] = collections.deque(maxlen=20)

    @property
    def active(self) -> bool:
        return bool(self.proc and self.proc.returncode is None and self.pending is not None)

    async def start(self) -> None:
        await self.cancel()
        self.error = None
        prepare_profile(self.home, "oauth-personal")
        self.temp_dir = tempfile.TemporaryDirectory(prefix="plainboard-agy-login-")
        env = build_env(self.home, Credentials(antigravity_auth_mode="google"), dict(os.environ), Path(self.temp_dir.name))
        binary = resolve_binary()
        args = [binary, "acp", "--gemini-dir", str(self.home), "--app-data-dir", "antigravity-acp", "--login"]
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=self.temp_dir.name,
            )
        except FileNotFoundError as exc:
            self._cleanup_temp()
            raise AntigravityError("The Antigravity CLI (agy) is not installed in the runner.") from exc
        self.proc = proc
        self.started = time.time()

        async def _drain_stderr() -> None:
            assert proc.stderr
            while line := await proc.stderr.readline():
                self._stderr_tail.append(line.decode(errors="replace").rstrip())

        stderr_task = asyncio.create_task(_drain_stderr())
        try:
            assert proc.stdout
            deadline = time.monotonic() + STARTUP_TIMEOUT
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AntigravityError("Antigravity did not produce a Google sign-in link in time.")
                raw_line = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
                if not raw_line:
                    await proc.wait()
                    tail = "\n".join(self._stderr_tail)
                    raise AntigravityError(f"Antigravity exited before showing a sign-in link.{f' {tail}' if tail else ''}")
                text = raw_line.decode(errors="replace").strip()
                if text.startswith("https://accounts.google.com/"):
                    self.pending = parse_authorization_url(text)
                    break
        except BaseException:
            stderr_task.cancel()
            await self.cancel()
            raise

        self._wait_task = asyncio.create_task(self._watch_timeout(proc, stderr_task))

    async def _watch_timeout(self, proc: asyncio.subprocess.Process, stderr_task: asyncio.Task[None]) -> None:
        try:
            await asyncio.wait_for(proc.wait(), timeout=LOGIN_TIMEOUT_SECONDS)
        except TimeoutError:
            self.error = "Google sign-in expired. Start sign-in again."
            with contextlib.suppress(Exception):
                proc.terminate()
        finally:
            stderr_task.cancel()
            self.pending = None
            self._cleanup_temp()

    async def submit(self, response: str) -> None:
        if not self.active or not self.pending or not self.proc or not self.proc.stdin:
            raise ValueError("No Antigravity sign-in is waiting for a code. Start sign-in again.")
        validated = validate_login_response(self.pending, response)
        self.proc.stdin.write((validated + "\n").encode("utf-8"))
        await self.proc.stdin.drain()
        with contextlib.suppress(Exception):
            self.proc.stdin.close()
        try:
            code = await asyncio.wait_for(self.proc.wait(), timeout=60)
        except TimeoutError as exc:
            await self.cancel()
            self.error = "Antigravity timed out finishing Google sign-in."
            raise AntigravityError(self.error) from exc
        if self._wait_task and not self._wait_task.done():
            self._wait_task.cancel()
            with contextlib.suppress(BaseException):
                await self._wait_task
        self.pending = None
        self.proc = None
        self._cleanup_temp()
        if code != 0:
            tail = "\n".join(self._stderr_tail)
            self.error = f"Antigravity could not finish Google sign-in. Start sign-in again.{f' ({tail})' if tail else ''}"
            raise AntigravityError(self.error)

    def _cleanup_temp(self) -> None:
        if self.temp_dir is not None:
            with contextlib.suppress(Exception):
                self.temp_dir.cleanup()
            self.temp_dir = None

    async def cancel(self) -> None:
        if self._wait_task and not self._wait_task.done():
            self._wait_task.cancel()
            with contextlib.suppress(BaseException):
                await self._wait_task
        if self.proc and self.proc.returncode is None:
            with contextlib.suppress(Exception):
                self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            except TimeoutError:
                with contextlib.suppress(Exception):
                    self.proc.kill()
        self.proc = None
        self.pending = None
        self._wait_task = None
        self._cleanup_temp()


# ---------------------------------------------------------------------------
# Provider implementation
# ---------------------------------------------------------------------------


class AntigravityProvider(Provider):
    name = "antigravity"
    limits_complete = True

    def __init__(self) -> None:
        self._models: tuple[float, list[ModelInfo]] | None = None
        self._logins: dict[str, CodeLogin] = {}
        self._emails: dict[str, str] = {}

    def info(self) -> ProviderInfo:
        models = self._models[1] if self._models else MODELS
        return ProviderInfo(name=self.name, label="Antigravity", models=models, efforts=EFFORTS)

    def skills_folder(self, spec: RunSpec, workspace: Path) -> Path | None:
        """Antigravity loads project skills from ``.gemini/skills`` (highest priority)."""
        return workspace / ".gemini" / "skills" if spec.toolset == "full" else None

    def _code_login(self, key: str) -> CodeLogin:
        if key not in self._logins:
            self._logins[key] = CodeLogin(account_home(key))
        return self._logins[key]

    def _any_signed_in_home(self) -> Path | None:
        default = account_home(DEFAULT_ACCOUNT)
        if has_stored_oauth_login(default):
            return default
        if ACCOUNTS_HOME.is_dir():
            for entry in sorted(ACCOUNTS_HOME.iterdir()):
                if entry.is_dir() and not entry.name.endswith("-api-key") and has_stored_oauth_login(entry):
                    return entry
        return None

    async def catalog(self) -> ProviderInfo:
        """Return cached models; only spawn ``agy`` to probe live models if a Google account is signed in."""
        if not self._models or time.monotonic() - self._models[0] > MODELS_TTL_SECONDS:
            home = self._any_signed_in_home()
            if home is None:
                return self.info()
            session_id: str | None = None
            try:
                with tempfile.TemporaryDirectory(prefix="plainboard-agy-catalog-") as cwd:
                    creds = Credentials(antigravity_auth_mode="google")
                    async with AcpServer(home, creds, cwd=Path(cwd)) as server:
                        await server.authenticate()
                        opened = await server.request("session/new", {"cwd": cwd, "mcpServers": []}, timeout=60)
                        session_id = opened.get("sessionId") if isinstance(opened, dict) else None
                        models = models_from_setup(opened if isinstance(opened, dict) else {})
                self._models = (time.monotonic(), models or MODELS)
            except Exception as exc:
                log.warning("Could not list Antigravity models: %s", exc)
                self._models = (time.monotonic() - MODELS_TTL_SECONDS + 300, self._models[1] if self._models else MODELS)
            finally:
                remove_session_files(home, session_id)
        return self.info()

    def _home(self, creds: Credentials) -> Path:
        return account_home(creds.antigravity_account, api_key=creds.antigravity_auth_mode == "api_key")

    async def _apply_model_and_effort(
        self,
        server: AcpServer,
        session_id: str,
        config_options: list[dict[str, Any]],
        model: str,
        effort: str | None,
    ) -> list[dict[str, Any]]:
        model_opt = find_config_option(config_options, option_id="model", category="model")
        if model and (not model_opt or model_opt.get("currentValue") != model):
            if model_opt:
                res = await server.set_config_option(session_id, str(model_opt["id"]), model)
                if isinstance(res.get("configOptions"), list):
                    config_options = res["configOptions"]
            elif not server.is_v2:
                await server.request("session/set_model", {"sessionId": session_id, "modelId": model})
        thinking_opt = find_config_option(config_options, category="thought_level")
        level = match_thinking_level(effort, thinking_opt)
        if level and thinking_opt and thinking_opt.get("currentValue") != level:
            try:
                res = await server.set_config_option(session_id, str(thinking_opt["id"]), level)
                if isinstance(res.get("configOptions"), list):
                    config_options = res["configOptions"]
            except AntigravityError as exc:
                if getattr(exc, "rpc_code", None) != -32602:
                    raise
                log.debug("Antigravity rejected thinking level %s for %s", level, model)
        return config_options

    async def run(self, spec: RunSpec, bridge: ToolBridge) -> AsyncIterator[RunEvent]:
        home = self._home(spec.credentials)
        bridge_file = BRIDGE_DIR / f"{uuid.uuid4().hex}.json"
        write_bridge_file(bridge_file, spec.bridge, bridge.workspace)
        env = {**tool_env(spec.credentials), **spec.env}
        server = AcpServer(home, spec.credentials, env=env, cwd=bridge.workspace)
        try:
            async with server:
                await server.authenticate()
                mcp_servers = mcp_servers_config(spec, bridge_file, v2=server.is_v2)
                can_append_sys = supports_system_prompt_append(server.init_result)
                meta = {"antigravity": {"systemPrompt": {"append": spec.system_prompt}}} if can_append_sys and spec.system_prompt else None
                base_params: dict[str, Any] = {"cwd": str(bridge.workspace), "mcpServers": mcp_servers}
                if meta:
                    base_params["_meta"] = meta

                if spec.resume_session_id:
                    try:
                        opened = await server.request(
                            "session/resume",
                            {"sessionId": spec.resume_session_id, **base_params},
                            timeout=STARTUP_TIMEOUT,
                        )
                    except AntigravityError as exc:
                        text = str(exc).lower()
                        if "not found" in text or "no session" in text or "no conversation" in text or getattr(exc, "rpc_code", None) in (-32002, -32602):
                            raise AntigravityError(f"Antigravity session not found: {spec.resume_session_id}") from exc
                        raise
                    session_id = str((opened or {}).get("sessionId") or spec.resume_session_id)
                else:
                    opened = await server.request("session/new", base_params, timeout=STARTUP_TIMEOUT)
                    session_id = str((opened or {}).get("sessionId") or "")
                    if not session_id:
                        raise AntigravityError("Antigravity did not return a session ID.")

                opened_dict = opened if isinstance(opened, dict) else {}
                if models := models_from_setup(opened_dict):
                    self._models = (time.monotonic(), models)
                config_options = list(opened_dict.get("configOptions") or [])
                await self._apply_model_and_effort(server, session_id, config_options, spec.model, spec.effort)

                initial_prompt = spec.prompt
                if not can_append_sys and not spec.resume_session_id and spec.system_prompt:
                    initial_prompt = f"{spec.system_prompt}\n\n---\n\n{spec.prompt}"

                mapper = EventMapper(session_id)
                yield SessionEvent(session_id=session_id)
                result_event: ResultEvent | None = None
                async for event in TurnRunner(server, mapper, spec, home, initial_prompt).run():
                    if event.type == "result":
                        result_event = event
                    else:
                        yield event

                # While the OAuth access token is guaranteed fresh after a Google turn, report limits.
                if spec.credentials.antigravity_auth_mode == "google" and result_event and not result_event.is_error:
                    with contextlib.suppress(Exception):
                        if windows := await asyncio.wait_for(self.limits(spec.credentials), timeout=8):
                            yield LimitsEvent(windows=windows, complete=True)
                if result_event is not None:
                    yield result_event
        except (AntigravityError, TimeoutError) as exc:
            raise RuntimeError(f"{exc}\n{server.stderr()}".strip()) from exc
        finally:
            bridge_file.unlink(missing_ok=True)

    async def complete(self, spec: CompletionSpec) -> CompletionResult:
        home = self._home(spec.credentials)
        session_id: str | None = None
        with tempfile.TemporaryDirectory(prefix="plainboard-agy-complete-") as cwd:
            server = AcpServer(home, spec.credentials, cwd=Path(cwd))
            try:
                async with server:
                    await server.authenticate()
                    can_append_sys = supports_system_prompt_append(server.init_result)
                    params: dict[str, Any] = {"cwd": cwd, "mcpServers": []}
                    if can_append_sys and spec.system_prompt:
                        params["_meta"] = {"antigravity": {"systemPrompt": {"append": spec.system_prompt}}}
                    opened = await server.request("session/new", params, timeout=STARTUP_TIMEOUT)
                    opened_dict = opened if isinstance(opened, dict) else {}
                    session_id = str(opened_dict.get("sessionId") or "")
                    if not session_id:
                        raise AntigravityError("Antigravity did not return a session ID.")
                    config_options = list(opened_dict.get("configOptions") or [])
                    await self._apply_model_and_effort(server, session_id, config_options, spec.model, spec.effort)

                    prompt_text = (
                        "Use only the input below. Do not use tools, read or write files, run commands, or ask questions.\n\n"
                        + ("" if can_append_sys or not spec.system_prompt else f"{spec.system_prompt}\n\n---\n\n")
                        + spec.prompt
                    )
                    mapper = EventMapper(session_id)
                    start_idx = read_last_step_index(home, session_id)
                    prompt_fut = server.start_request(
                        "session/prompt",
                        {"sessionId": session_id, "prompt": [{"type": "text", "text": prompt_text}]},
                    )
                    next_task: asyncio.Task[dict[str, Any] | None] | None = None
                    try:
                        while not prompt_fut.done():
                            if next_task is None:
                                next_task = asyncio.create_task(server.next())
                            done, _ = await asyncio.wait({prompt_fut, next_task}, timeout=180, return_when=asyncio.FIRST_COMPLETED)
                            if not done:
                                raise AntigravityError("Antigravity completion timed out.")
                            if next_task in done:
                                msg = next_task.result()
                                next_task = None
                                if msg is None:
                                    raise AntigravityError("Antigravity exited during completion.")
                                if "id" in msg:
                                    await answer_server_request(server, msg, deny_all=True)
                                else:
                                    mapper.map(msg.get("method", ""), msg.get("params") or {})
                    finally:
                        if next_task is not None and not next_task.done():
                            next_task.cancel()
                            with contextlib.suppress(BaseException):
                                await next_task

                    for msg in server.drain_nowait():
                        if msg and "id" not in msg:
                            mapper.map(msg.get("method", ""), msg.get("params") or {})
                    resp = prompt_fut.result()
                    resp_dict = resp if isinstance(resp, dict) else {}
                    mapper.add_prompt_usage(resp_dict.get("usage"))
                    last_step = read_last_step_after(home, session_id, start_idx)
                    if failure := analyze_turn_outcome(str(resp_dict.get("stopReason") or "end_turn"), last_step, now=time.time()):
                        raise AntigravityError(failure.message)
                    mapper.flush_text()
            except (AntigravityError, TimeoutError) as exc:
                raise RuntimeError(f"{exc}\n{server.stderr()}".strip()) from exc
            finally:
                remove_session_files(home, session_id)
        return CompletionResult(
            text=(mapper.last_text or "").strip(),
            input_tokens=mapper.input_tokens + mapper.cache_read_tokens,
            output_tokens=mapper.output_tokens,
        )

    async def limits(self, credentials: Credentials) -> list[LimitWindow]:
        if credentials.antigravity_auth_mode != "google":
            return []
        home = account_home(credentials.antigravity_account)
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            resolved = await read_stored_access_token(home, client)
            if not resolved:
                return []
            access_token, project_id = resolved
            headers = {
                "Authorization": f"Bearer {access_token}",
                "User-Agent": quota_user_agent(),
            }
            body = {"project": project_id} if project_id else {}

            async def _fetch(operation: str) -> list[LimitWindow]:
                merged: dict[str, LimitWindow] = {}
                for endpoint in QUOTA_ENDPOINTS:
                    requested_at = time.time()
                    try:
                        resp = await client.post(f"{endpoint}/v1internal:{operation}", headers=headers, json=body, timeout=10)
                        if resp.status_code != 200:
                            continue
                        data = resp.json()
                    except Exception:
                        continue
                    parsed = parse_summary_windows(data, requested_at) if operation == "retrieveUserQuotaSummary" else parse_model_quota_windows(data)
                    for win in parsed:
                        merged.setdefault(win.name, win)
                return list(merged.values())

            return await _fetch("retrieveUserQuotaSummary") or await _fetch("fetchAvailableModels")

    async def _lookup_email(self, key: str, home: Path) -> str:
        if cached := self._emails.get(key):
            return cached
        async with httpx.AsyncClient(timeout=8, follow_redirects=False) as client:
            resolved = await read_stored_access_token(home, client)
            if not resolved:
                return ""
            try:
                resp = await client.get(
                    "https://www.googleapis.com/oauth2/v2/userinfo",
                    headers={"Authorization": f"Bearer {resolved[0]}"},
                    timeout=8,
                )
                if resp.status_code == 200:
                    email = str(resp.json().get("email") or "").strip()
                    if email:
                        self._emails[key] = email
                        return email
            except Exception:
                pass
        return ""

    async def account(self, key: str) -> ProviderAccount:
        login = self._code_login(key)
        if login.active and login.pending:
            return ProviderAccount(
                signed_in=False,
                login_url=login.pending.authorization_url,
                login_input=True,
                login_error=login.error,
            )
        signed_in = has_stored_oauth_login(login.home)
        detail = ""
        if signed_in:
            detail = await self._lookup_email(key, login.home) or "Google account"
        return ProviderAccount(signed_in=signed_in, detail=detail, login_error=login.error)

    async def login(self, key: str) -> ProviderAccount:
        login = self._code_login(key)
        await login.start()
        return ProviderAccount(
            signed_in=False,
            login_url=login.pending.authorization_url if login.pending else None,
            login_input=True,
        )

    async def submit_login(self, key: str, code: str) -> ProviderAccount:
        login = self._code_login(key)
        await login.submit(code)
        self._emails.pop(key, None)
        self._models = None  # refresh live model catalog on next /providers call
        return await self.account(key)

    async def logout(self, key: str) -> ProviderAccount:
        login = self._code_login(key)
        await login.cancel()
        self._emails.pop(key, None)
        acp_dir = login.home / "antigravity-acp"
        for name in ("antigravity-oauth-token", "acp_token.json"):
            (acp_dir / name).unlink(missing_ok=True)
        login.error = None
        return ProviderAccount()
