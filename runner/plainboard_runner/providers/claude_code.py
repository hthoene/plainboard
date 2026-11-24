"""Claude Code provider built on the Claude Agent SDK (bundled Claude Code CLI).

Auth: a Claude subscription OAuth token (``claude setup-token``), an Anthropic API key,
or an interactive ``claude login`` performed inside the runner container. A person's own browser
sign-in ("own_login") is never in the runner's home: ``claude auth login`` runs in a private
``CLAUDE_CONFIG_DIR``, and its ``.credentials.json`` comes with each run of the account into such a
folder again (signins.py), whose other entries link to the shared ``~/.claude`` (sessions resume).
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import hashlib
import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from datetime import UTC, datetime, timedelta
from collections.abc import AsyncIterator, Callable
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    create_sdk_mcp_server,
    tool,
)
import httpx
from claude_agent_sdk.types import RateLimitEvent, RateLimitInfo, StreamEvent
from plainboard_protocol import (
    AssistantTextEvent,
    ProviderAccount,
    CompactionEvent,
    DEFAULT_ACCOUNT,
    CompletionResult,
    CompletionSpec,
    Credentials,
    LimitsEvent,
    LimitWindow,
    McpServerSpec,
    ModelInfo,
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

from .. import notices, signins
from ..config import TOOL_RESULT_PREVIEW_CHARS
from ..signins import SIGN_INS
from ..tool_bridge import ToolBridge
from ..workspace import tool_env
from .base import Provider, ProviderError, merge_efforts

log = logging.getLogger(__name__)

MCP_SERVER_NAME = "plainboard"
BOARD_TOOLSET_BUILTINS = ["Read", "Glob", "Grep", "Bash", "Edit", "Write"]
READ_TOOLSET_BUILTINS = ["Read", "Glob", "Grep"]  # a question about the project (app questions.py): nothing that changes anything
# Interactive tools make no sense for headless agents; they talk back via chat or the board. Plan mode
# waits for an approval nobody gives. The CLI's own todo list would compete with the PlainBoard task
# list, which is the one the user and the manager see. Monitor waits for a process of its own: a turn
# that ends meanwhile ends the assignment, and nothing takes it up again (start_job does).
DISALLOWED_TOOLS = ["AskUserQuestion", "EnterPlanMode", "ExitPlanMode", "TodoWrite", "TaskCreate", "TaskUpdate", "TaskList", "TaskGet", "Monitor"]
BACKGROUND_REFUSED = (
    "[PlainBoard] Not in the background: when your turn ends, your assignment ends with it, and nothing "
    "brings you back when the command is done. Run it with start_job (wait_minutes for a short one)."
)
# Background work of Claude Code that does not report back to PlainBoard: a turn may not end on it.
UNAWAITED_TASKS = ("shell", "monitor")
STILL_RUNNING = (
    "[PlainBoard] Still running in the background: {what}. A turn that ends now ends your assignment, and "
    "this is stopped with it. Wait for it with start_job/wait_job (run it again there), or stop it and say what is missing."
)
# The message Claude Code writes into the session in place of an answer when a request fails
# (a used-up limit, say). It is not the agent's: it is shown as an error, and a resumed session leaves it out.
SYNTHETIC_MODEL = "<synthetic>"
SESSION_TAIL_BYTES = 256 * 1024
# One CLI message holds a whole tool result; a screenshot the agent reads easily exceeds the SDK's 1 MB default.
MAX_MESSAGE_BYTES = 64 * 1024 * 1024
NOTICE_GRACE_SECONDS = 60
NOTICE_CHECK_SECONDS = 5
# Background work of Claude Code that reports back when it is done. A shell or a monitor may never end.
AWAITED_TASKS = ("subagent", "workflow")
# Nothing at all heard from the session for this long while the agent waits for such work: it goes on without.
BACKGROUND_SILENT_SECONDS = 1200
BACKGROUND_GIVE_UP_NOTE = (
    "[PlainBoard] The work you started in the background has been silent for {minutes} min. "
    "Do not wait for it any longer: finish with what you have, and say what is missing."
)
# A tool call that takes this long gets its duration added to its result: the agent has no clock.
SLOW_TOOL_SECONDS = 120
# These PlainBoard tools already say how long they waited.
TIMED_TOOLS = ("start_job", "wait_job", "ask_agent")

MODELS = [
    ModelInfo(id="claude-opus-5-5", label="Claude Opus 5.5", tier="large"),
    ModelInfo(id="claude-sonnet-5-5", label="Claude Sonnet 5.5", tier="medium"),
    ModelInfo(id="claude-haiku-5-5", label="Claude Haiku 5.5", tier="small"),
    ModelInfo(id="claude-fable-5-2", label="Claude Fable 5.2", tier="xlarge"),
    ModelInfo(id="claude-fable-5-1", label="Claude Fable 5.1", tier="xlarge"),
]
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
TIER_HINTS = {"fable": "xlarge", "opus": "large", "sonnet": "medium", "haiku": "small"}
CLI_MODELS_TTL_SECONDS = 3600  # how long Claude Code's own list of models (cli_models) is kept
# What Claude Code says when a turn is turned away because a plan limit is used up, e.g. "You've hit
# your 5-hour limit · resets 7:10pm (UTC)", or in older versions "Claude AI usage limit reached|<unix time>".
LIMIT_HIT = re.compile(r"hit your (?P<kind>[\w -]*?)\s*limit|usage limit reached(?:\|(?P<at>\d+))?", re.IGNORECASE)
LIMIT_RESET = re.compile(r"resets (?:at )?(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<half>[ap]m)?(?:\s*\((?P<zone>[^)]+)\))?", re.IGNORECASE)


# Claude's plans by rate limit tier, with their size as a multiple of Pro's usage. A plan Claude Code
# names without a tier gets its name only (Max comes in two sizes); the user can give its size.
# Ultra (10x Pro a week, no 5-hour window) has no tier name Claude Code knows yet: a tier or plan
# type that names it counts as Ultra.
PLAN_TIERS = {"default_claude_max_5x": ("Max 5x", 5.0), "default_claude_max_20x": ("Max 20x", 20.0), "default_claude_ultra": ("Ultra", 10.0)}
PLAN_TYPES = {"pro": ("Pro", 1.0), "max": ("Max", None), "ultra": ("Ultra", 10.0)}
PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
PLAN_KEEP_SECONDS = 6 * 3600  # a plan rarely changes; asking for it costs a request, no tokens


def guess_tier(model_id: str) -> str:
    return next((tier for hint, tier in TIER_HINTS.items() if hint in model_id), "medium")


def cli_models(listed: list[Any] | None) -> tuple[list[ModelInfo], list[str]]:
    """The models Claude Code offers (``models`` of its initialize answer) beyond MODELS, and their effort levels."""
    known = {m.id for m in MODELS}
    out: dict[str, ModelInfo] = {}
    levels: list[list[str]] = []
    entries = [m for m in listed or [] if isinstance(m, dict)]
    for m in sorted(entries, key=lambda m: m.get("value") == "default"):  # "default" is another one's alias: its label last
        model_id = str(m.get("resolvedModel") or m.get("value") or "")
        if not model_id.startswith("claude-"):
            continue
        levels.append([str(e) for e in m.get("supportedEffortLevels") or []])
        if model_id in known or model_id in out:
            continue
        name = str(m.get("description") or "").split(" · ")[0].strip() or str(m.get("displayName") or "")
        label = name if name.lower().startswith("claude") else f"Claude {name}" if name else model_id
        out[model_id] = ModelInfo(id=model_id, label=label, tier=guess_tier(model_id))  # type: ignore[arg-type]
    return list(out.values()), merge_efforts([], *levels)


def plan_of(subscription: str | None, tier: str | None) -> tuple[str | None, float | None]:
    """Name and size of a plan from what Claude Code knows about the account."""
    if tier in PLAN_TIERS:
        return PLAN_TIERS[tier]
    if "ultra" in (tier or "").lower():
        return PLAN_TIERS["default_claude_ultra"]
    kind = (subscription or "").lower().removeprefix("claude_")
    if not kind:
        return None, None
    return PLAN_TYPES.get(kind, (kind.replace("_", " ").title(), None))


def shared_home() -> Path:
    return Path.home() / ".claude"


SIGN_IN_FILES = (".credentials.json",)  # what a person's own sign-in is
CLAUDE_BIN = os.environ.get("PLAINBOARD_CLAUDE_BIN", "claude")
LOGIN_START_SECONDS = 30
LOGIN_CODE_SECONDS = 30
LOGIN_TIMEOUT_SECONDS = 15 * 60
LOGIN_URL = re.compile(r"https://\S+")
HOME_SIGN_IN = "claude_code"  # the mark of the home's sign-in once the app took it over (signins.TAKEN)
REFRESH_AHEAD_SECONDS = 3 * 3600  # a kept sign-in whose token ends sooner is refreshed ahead (refresh_sign_in)


def refresh_due(files: dict[str, str], now: float | None = None) -> bool:
    """A Claude sign-in whose access token ends within ``REFRESH_AHEAD_SECONDS``."""
    try:
        oauth = json.loads(files.get(SIGN_IN_FILES[0]) or "{}").get("claudeAiOauth") or {}
    except (ValueError, AttributeError):
        return False
    expires = oauth.get("expiresAt")
    if not oauth.get("refreshToken") or not isinstance(expires, (int, float)):
        return False
    return expires / 1000 - (time.time() if now is None else now) < REFRESH_AHEAD_SECONDS


def private_config(home: Path) -> None:
    """A private ``CLAUDE_CONFIG_DIR`` that shares everything of ``~/.claude`` but the sign-in: sessions,
    plugins and settings stay where every run finds them."""
    shared = shared_home()
    (shared / "projects").mkdir(parents=True, exist_ok=True)
    for entry in shared.iterdir():
        if entry.name in SIGN_IN_FILES or entry.name == ".claude.json" or (home / entry.name).exists():
            continue
        (home / entry.name).symlink_to(entry, target_is_directory=entry.is_dir())
    if not (home / ".claude.json").exists():
        signins.write(home, {".claude.json": json.dumps({"hasCompletedOnboarding": True})})


def auth_env(creds: Credentials, home: Path | None = None) -> dict[str, str]:
    """``home``: the private folder of an own sign-in (``ClaudeCodeProvider._home``)."""
    # Turns of one agent are often more than five minutes apart (a job, a teammate, a free slot). With
    # the short cache lifetime, the default with an API key, each of them would write the whole context
    # to the cache again instead of reading it.
    env = {"DISABLE_AUTOUPDATER": "1", "CLAUDE_AGENT_SDK_CLIENT_APP": "plainboard/0.1", "ENABLE_PROMPT_CACHING_1H": "1"}
    if creds.claude_auth_mode == "oauth_token":
        if not creds.claude_oauth_token:
            raise RuntimeError("Claude OAuth token is not configured (Settings → Claude).")
        env["CLAUDE_CODE_OAUTH_TOKEN"] = creds.claude_oauth_token
    elif creds.claude_auth_mode == "api_key":
        if not creds.anthropic_api_key:
            raise RuntimeError("Anthropic API key is not configured (Settings → Claude).")
        env["ANTHROPIC_API_KEY"] = creds.anthropic_api_key
    elif creds.claude_auth_mode == "own_login":
        if home is None:  # never the runner's own sign-in in its place
            raise RuntimeError("A person's own Claude sign-in needs its private folder.")
        env["CLAUDE_CONFIG_DIR"] = str(home)
    # "runner_login": rely on credentials stored by `claude login` in the runner's home volume.
    return env


class ClaudeLogin:
    """Claude's browser sign-in for a person's own subscription, in a private folder: ``claude auth login``
    prints the address to open; the page shows a code afterwards, which is typed in here (``code``)."""

    def __init__(self, provider: str, key: str) -> None:
        self.provider, self.key = provider, key
        self.home = signins.private_dir()
        self.proc: asyncio.subprocess.Process | None = None
        self.output = ""
        self.url: str | None = None
        self.error: str | None = None
        self.succeeded = False
        self._reader: asyncio.Task[None] | None = None
        self._ended: asyncio.Task[None] | None = None

    @property
    def active(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def start(self) -> None:
        signins.write(self.home, {".claude.json": json.dumps({"hasCompletedOnboarding": True})})
        env = {**os.environ, "CLAUDE_CONFIG_DIR": str(self.home), "BROWSER": "false", "DISABLE_AUTOUPDATER": "1"}
        for name in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"):
            env.pop(name, None)
        self.proc = await asyncio.create_subprocess_exec(
            CLAUDE_BIN, "auth", "login", "--claudeai", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, cwd=str(self.home), env=env
        )
        self._reader = asyncio.create_task(self._read())
        self._ended = asyncio.create_task(self._wait())
        deadline = time.monotonic() + LOGIN_START_SECONDS
        while self.url is None and self.active and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
        if self.url is None:
            await self.cancel()
            raise RuntimeError(f"Claude Code did not offer a sign-in address: {self.last_line() or 'no output'}")

    async def _read(self) -> None:
        assert self.proc and self.proc.stdout
        while chunk := await self.proc.stdout.read(4096):
            self.output = (self.output + chunk.decode(errors="replace"))[-8000:]
            if self.url is None and (found := LOGIN_URL.search(self.output)):
                self.url = found.group(0)

    async def _wait(self) -> None:
        assert self.proc
        try:
            await asyncio.wait_for(self.proc.wait(), timeout=LOGIN_TIMEOUT_SECONDS)
        except TimeoutError:
            self.error = "The sign-in expired"
            with contextlib.suppress(ProcessLookupError):
                self.proc.kill()
            await self.proc.wait()
        if self._reader:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._reader, timeout=5)
        files = signins.read(self.home, SIGN_IN_FILES)
        if self.proc.returncode == 0 and files:
            SIGN_INS.signed_in(self.provider, self.key, files)
            self.succeeded = True
        elif not self.error:
            self.error = self.last_line() or "Sign-in failed"
        self.url = None
        signins.remove(self.home)

    def last_line(self) -> str:
        lines = [line.strip(" >") for line in re.split(r"[\r\n]+", self.output) if line.strip(" >")]
        return lines[-1][:300] if lines else ""

    async def code(self, code: str) -> None:
        if not self.active or not self.proc or not self.proc.stdin:
            raise RuntimeError("No sign-in is waiting for a code. Start it again.")
        self.error = None
        seen = len(self.output)
        self.proc.stdin.write(code.strip().encode() + b"\n")
        await self.proc.stdin.drain()
        deadline = time.monotonic() + LOGIN_CODE_SECONDS
        heard, quiet = seen, time.monotonic()
        while self.active and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
            if len(self.output) != heard:
                heard, quiet = len(self.output), time.monotonic()
            elif heard > seen and time.monotonic() - quiet > 2:
                break  # it answered and goes on waiting: the code was refused
        if self.active:
            said = [line.strip(" >") for line in re.split(r"[\r\n]+", self.output[seen:]) if line.strip(" >")]
            self.error = said[-1][:300] if said else "Claude Code did not take the code"
        elif self._ended:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(self._ended), timeout=10)

    async def cancel(self) -> None:
        if self.active and self.proc:
            with contextlib.suppress(ProcessLookupError):
                self.proc.kill()
        if self._ended:
            with contextlib.suppress(BaseException):
                await self._ended
        signins.remove(self.home)


def context_tokens_from_usage(usage: dict[str, Any] | None) -> int:
    if not usage:
        return 0
    return int(
        (usage.get("input_tokens") or 0)
        + (usage.get("cache_read_input_tokens") or 0)
        + (usage.get("cache_creation_input_tokens") or 0)
        + (usage.get("output_tokens") or 0)
    )


def _tool_result_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(item.get("text", ""))
        elif isinstance(item, dict):
            parts.append(f"[{item.get('type', 'content')}]")
    return "\n".join(parts)


def external_mcp_config(server: McpServerSpec) -> dict[str, Any]:
    if server.transport == "stdio":
        return {"type": "stdio", "command": server.command or "", "args": server.args, "env": server.env}
    return {"type": server.transport, "url": server.url or "", "headers": server.headers}


def build_mcp_server(bridge: ToolBridge):
    tools = []
    for definition in bridge.tools:

        def make_handler(name: str):
            async def handler(args: dict[str, Any]) -> dict[str, Any]:
                result = await bridge.call(name, args)
                images = [{"type": "image", "data": i.data, "mimeType": i.media_type} for i in result.images]
                return {"content": [{"type": "text", "text": result.text}, *images], "is_error": result.is_error}

            return handler

        tools.append(tool(definition.name, definition.description, definition.input_schema)(make_handler(definition.name)))
    server = create_sdk_mcp_server(name=MCP_SERVER_NAME, version="1.0.0", tools=tools)
    # PlainBoard's own tools are in the prompt with their descriptions and parameters, not found
    # on demand. Claude Code defers MCP tools by default: the agent then sees only their names, has
    # to search before it can call one (a step), and when it guesses the parameters instead, the
    # call is refused (another step). Measured on a day of real work: 84 searches and 57 refused
    # calls of 1640. The definitions are short (about 3k tokens for a member, 7k for a manager) and
    # cached like the system prompt; that is about what the extra steps cost. Tools of the MCP
    # servers the user added stay deferred: they can be many and long.
    server["alwaysLoad"] = True  # type: ignore[typeddict-unknown-key]
    return server


class ToolTimer:
    """Measures tool calls, so that a slow one can tell the agent how long it took."""

    def __init__(self) -> None:
        self.started: dict[str, float] = {}

    async def start(self, hook_input: Any, tool_use_id: str | None, _context: Any) -> dict[str, Any]:
        if hook_input.get("tool_name") == "Bash" and (hook_input.get("tool_input") or {}).get("run_in_background"):
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": BACKGROUND_REFUSED}}
        if tool_use_id and not hook_input.get("agent_id"):
            self.started[tool_use_id] = time.monotonic()
        return {}

    def note(self, hook_input: Any, tool_use_id: str | None) -> str | None:
        start = self.started.pop(tool_use_id or "", None)
        if start is None or str(hook_input.get("tool_name", "")).endswith(TIMED_TOOLS):
            return None
        seconds = time.monotonic() - start
        if seconds < SLOW_TOOL_SECONDS:
            return None
        return (
            f"[PlainBoard] That call took {round(seconds / 60)} min, far longer than writing the change. "
            "Before the next slow call, put in everything that belongs to it and check with something faster first."
        )


def notice_hook(run_id: str, timer: ToolTimer | None = None):
    """Hands messages for the running turn to the agent after its current tool call, and says how
    long a slow call took."""

    async def hook(hook_input: Any, tool_use_id: str | None, _context: Any) -> dict[str, Any]:
        if hook_input.get("agent_id"):  # a sub-agent's tool call: the message is for the agent itself
            return {}
        pending = notices.drain(run_id)
        if timer and (took := timer.note(hook_input, tool_use_id)):
            pending.append(took)
        if not pending:
            return {}
        return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "\n\n".join(pending)}}

    return hook


def resume_point(session_file: Path) -> str | None:
    """Where a session is to be taken up when its last turn ended in Claude Code's error message
    (a used-up limit): the entry before it, so that the agent does not read it as its own answer
    from then on. None: take it up at its end."""
    try:
        with session_file.open("rb") as handle:
            handle.seek(max(0, handle.seek(0, 2) - SESSION_TAIL_BYTES))
            lines = handle.read().splitlines()
    except OSError:
        return None
    entries: dict[str, dict] = {}
    leaf = None
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue  # cut off by the tail
        if not isinstance(entry, dict):
            continue
        if entry.get("type") in ("user", "assistant") and entry.get("uuid") and not entry.get("isSidechain"):
            entries[entry["uuid"]] = entry
            leaf = entry["uuid"]
        elif entry.get("type") == "last-prompt" and entry.get("leafUuid"):
            leaf = entry["leafUuid"]
    point, moved = leaf, False
    while point and (entry := entries.get(point)) and entry.get("type") == "assistant" and (entry.get("isApiErrorMessage") or (entry.get("message") or {}).get("model") == SYNTHETIC_MODEL):
        point, moved = entry.get("parentUuid"), True
    return point if moved else None


def session_file(cwd: str | Path, session_id: str) -> Path:
    """Claude Code's transcript of a session started in ``cwd``."""
    return Path.home() / ".claude" / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", str(cwd)) / f"{session_id}.jsonl"


def limit_windows(info: RateLimitInfo) -> list[LimitWindow]:
    """The plan's limit windows. Only the window that changed may be known, usually the one getting tight."""
    windows = {
        name: LimitWindow(name=name, utilization=window["utilization"], resets_at=window.get("resetsAt"), status=window.get("status"))
        for name, window in (info.raw.get("unifiedWindows") or {}).items()
    }
    kind = info.rate_limit_type
    known = info.utilization is not None or info.status == "rejected"
    if kind and kind != "overage" and kind not in windows and known:
        used = info.utilization if info.utilization is not None else 1.0
        windows[kind] = LimitWindow(name=kind, utilization=used, resets_at=info.resets_at, status=info.status)
    return list(windows.values())


def usage_windows(rate_limits: dict[str, Any] | None) -> list[LimitWindow]:
    """Limit windows from Claude Code's usage answer, which has percentages and ISO dates."""
    windows = []
    for name in ("five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet"):
        window = (rate_limits or {}).get(name) or {}
        if window.get("utilization") is not None:
            resets = window.get("resets_at")
            windows.append(LimitWindow(name=name, utilization=window["utilization"] / 100, resets_at=int(datetime.fromisoformat(resets).timestamp()) if resets else None))
    return windows


def _reset_time(text: str, now: datetime) -> int | None:
    """The next time of day named in "resets 7:10pm (UTC)", as unix seconds."""
    from zoneinfo import ZoneInfo

    if not (found := LIMIT_RESET.search(text)):
        return None
    hour, minute = int(found["hour"]), int(found["minute"] or 0)
    if found["half"]:
        hour = hour % 12 + (12 if found["half"].lower() == "pm" else 0)
    if hour > 23 or minute > 59:
        return None
    try:
        zone = ZoneInfo(found["zone"].strip()) if found["zone"] else UTC
    except Exception:
        zone = UTC
    local = now.astimezone(zone)
    at = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if at <= local:
        at += timedelta(days=1)
    return int(at.timestamp())


def limit_hit(error: str | None, now: datetime | None = None) -> LimitWindow | None:
    """The window a turn was turned away by, from Claude Code's error text; None for another error."""
    if not error or not (found := LIMIT_HIT.search(error)):
        return None
    kind = (found["kind"] or "").lower()
    name = "five_hour" if "5-hour" in kind or "five" in kind else "seven_day_opus" if "opus" in kind else "seven_day_sonnet" if "sonnet" in kind else "seven_day" if "week" in kind else "five_hour"
    resets = int(found["at"]) if found["at"] else _reset_time(error, now or datetime.now(UTC))
    return LimitWindow(name=name, utilization=1.0, resets_at=resets, status="rejected")


class UrgentNoticeWatch:
    """Notices arrive after a tool call, so a long one would hold back a stop: interrupt it after a grace period."""

    def __init__(self, client: Any, run_id: str) -> None:
        self.client = client
        self.run_id = run_id
        self.open_tools: set[str] = set()
        self.interrupted = False

    def see(self, message: Any) -> None:
        if isinstance(message, (AssistantMessage, UserMessage)) and not message.parent_tool_use_id and not isinstance(message.content, str):
            for block in message.content:
                if isinstance(block, ToolUseBlock):
                    self.open_tools.add(block.id)
                elif isinstance(block, ToolResultBlock):
                    self.open_tools.discard(block.tool_use_id)

    async def check(self) -> None:
        if self.interrupted or not self.open_tools or notices.urgent_waiting(self.run_id) < NOTICE_GRACE_SECONDS:
            return
        self.interrupted = True
        try:
            await self.client.interrupt()
        except Exception:
            self.interrupted = False
            log.warning("Could not interrupt run %s", self.run_id, exc_info=True)

    async def watch(self) -> None:
        while True:
            await asyncio.sleep(NOTICE_CHECK_SECONDS)
            await self.check()

    def resume_prompt(self) -> str:
        """The turn goes on with the waiting messages; there is no tool call left to attach them to."""
        self.interrupted = False
        self.open_tools.clear()
        return "\n\n".join(["[PlainBoard] Your running tool call was interrupted for this message, and its work may be half done.", *notices.drain(self.run_id)])


class BackgroundWork:
    """Claude Code runs sub-agents in the background: the agent may end its turn while they still
    work, and the session takes the turn up again by itself when one reports. Such an end is not
    the turn's result. Closing the session there would throw the sub-agents' work away, so the
    runner keeps reading until nothing is in flight. Waiting costs no model turn."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.open: list[str] = []  # what Claude Code said is in flight when the agent last stopped
        self.waiting = False
        self.given_up = False
        self.heard = time.monotonic()

    async def stop_hook(self, hook_input: Any, _tool_use_id: str | None, _context: Any) -> dict[str, Any]:
        tasks = hook_input.get("background_tasks") or []
        self.open = [str(task.get("description") or task.get("id")) for task in tasks if task.get("type") in AWAITED_TASKS]
        # A shell Claude Code moved to the background reports to nobody: refused once, then the turn may end.
        running = [str(task.get("command") or task.get("description") or task.get("id"))[:200] for task in tasks if task.get("type") in UNAWAITED_TASKS]
        if running and not self.open and not hook_input.get("stop_hook_active"):
            return {"decision": "block", "reason": STILL_RUNNING.format(what="; ".join(running))}
        return {}

    def see(self, message: Any) -> None:
        self.heard = time.monotonic()
        if isinstance(message, AssistantMessage) and not message.parent_tool_use_id:
            self.waiting = False
            self.open = []  # the agent works again: its next stop says what is in flight then

    def holds(self, result: ResultEvent) -> bool:
        """Whether this end of a turn only pauses it."""
        self.waiting = bool(self.open) and not result.is_error and not self.given_up
        return self.waiting

    async def check(self, client: Any) -> None:
        """A waiting agent has no tool call a message could arrive after: it gets it as a prompt."""
        if not self.waiting:
            return
        if notices.pending(self.run_id):
            prompt = "\n\n".join(notices.drain(self.run_id))
        elif time.monotonic() - self.heard >= BACKGROUND_SILENT_SECONDS:
            self.given_up = True
            log.warning("Run %s: background work silent, going on without: %s", self.run_id, "; ".join(self.open)[:300])
            prompt = BACKGROUND_GIVE_UP_NOTE.format(minutes=round(BACKGROUND_SILENT_SECONDS / 60))
        else:
            return
        self.waiting = False
        try:
            await client.query(prompt)
        except Exception:
            log.warning("Could not reach the waiting run %s", self.run_id, exc_info=True)

    async def watch(self, client: Any) -> None:
        while True:
            await asyncio.sleep(NOTICE_CHECK_SECONDS)
            await self.check(client)


class EventMapper:
    """Translates Agent SDK messages into protocol events (kept separate for unit tests)."""

    def __init__(self) -> None:
        self.context_tokens = 0
        self.last_text: str | None = None
        self.rejected = False  # Claude reported a used-up limit in this turn

    def map(self, message: Any) -> list[RunEvent]:
        events: list[RunEvent] = []
        if isinstance(message, StreamEvent):
            event = message.event or {}
            delta = event.get("delta") or {}
            if event.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
                events.append(TextDeltaEvent(text=delta.get("text", "")))
        elif isinstance(message, RateLimitEvent):
            if windows := limit_windows(message.rate_limit_info):
                self.rejected = self.rejected or any(w.status == "rejected" for w in windows)
                events.append(LimitsEvent(windows=windows))
        elif isinstance(message, SystemMessage):
            if message.subtype == "init" and message.data.get("session_id"):
                events.append(SessionEvent(session_id=message.data["session_id"]))
            elif message.subtype == "compact_boundary":
                metadata = message.data.get("compact_metadata") or {}
                events.append(CompactionEvent(tokens_before=int(metadata.get("pre_tokens") or 0)))
        elif isinstance(message, AssistantMessage):
            if message.parent_tool_use_id:  # sub-agent chatter: keep out of the transcript
                return events
            if message.error or message.model == SYNTHETIC_MODEL:  # Claude Code's own error text, see the result
                return events
            for block in message.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    self.last_text = block.text
                    events.append(AssistantTextEvent(text=block.text))
                elif isinstance(block, ToolUseBlock):
                    events.append(ToolUseEvent(id=block.id, name=block.name, input=block.input or {}))
            tokens = context_tokens_from_usage(message.usage)
            if tokens:
                self.context_tokens = tokens
                events.append(UsageEvent(context_tokens=tokens))
        elif isinstance(message, UserMessage):
            if message.parent_tool_use_id or isinstance(message.content, str):
                return events
            for block in message.content:
                if isinstance(block, ToolResultBlock):
                    events.append(
                        ToolResultEvent(
                            tool_use_id=block.tool_use_id,
                            content=_tool_result_text(block.content)[:TOOL_RESULT_PREVIEW_CHARS],
                            is_error=bool(block.is_error),
                        )
                    )
        elif isinstance(message, ResultMessage):
            usage = message.usage or {}
            error = None
            if message.is_error:
                error = "; ".join(message.errors or []) or message.result or message.subtype
                # A turn turned away by a used-up limit: the account counts as used up until the reset,
                # also when Claude Code did not report the window itself.
                if not self.rejected and (hit := limit_hit(error)):
                    events.append(LimitsEvent(windows=[hit]))
            events.append(
                ResultEvent(
                    session_id=message.session_id,
                    is_error=message.is_error,
                    error=error,
                    text=message.result if not message.is_error else self.last_text,
                    num_turns=message.num_turns,
                    input_tokens=int(usage.get("input_tokens") or 0),
                    output_tokens=int(usage.get("output_tokens") or 0),
                    cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
                    cache_creation_tokens=int(usage.get("cache_creation_input_tokens") or 0),
                    session_cost_usd=float(message.total_cost_usd or 0.0),  # keeps growing across resumed turns
                    context_tokens=self.context_tokens,
                )
            )
        return events


PROBE_MODEL = next(m.id for m in MODELS if m.tier == "small")


# How long the SDK waits for a starting CLI's answer to "initialize" (ms; the SDK's default is 60 s).
# On a busy host, a CLI with MCP servers resuming a long session took longer. The SDK reads the
# variable from this process only for that wait; Claude Code itself does not use it.
os.environ.setdefault("CLAUDE_CODE_STREAM_CLOSE_TIMEOUT", "300000")
START_ATTEMPTS = 2


def _start_timed_out(exc: BaseException) -> bool:
    return "Control request timeout: initialize" in str(exc)


@contextlib.asynccontextmanager
async def _started(options: Callable[[], ClaudeAgentOptions], run_id: str) -> AsyncIterator[ClaudeSDKClient]:
    """A connected client; a CLI that did not start in time is started once more (nothing ran yet)."""
    for attempt in range(1, START_ATTEMPTS + 1):
        client = ClaudeSDKClient(options=options())
        try:
            await client.__aenter__()
        except Exception as exc:
            with contextlib.suppress(Exception):
                await client.__aexit__(None, None, None)  # ends the CLI that hangs in its start
            if not _start_timed_out(exc):
                raise
            log.warning("Run %s: Claude Code did not start in time (attempt %d of %d)", run_id, attempt, START_ATTEMPTS)
            continue
        try:
            yield client
        finally:
            await client.__aexit__(None, None, None)
        return
    seconds = max(int(os.environ["CLAUDE_CODE_STREAM_CLOSE_TIMEOUT"]) // 1000, 60)
    raise ProviderError(f"Claude Code did not start within {seconds} s, twice; the host is probably overloaded. The turn can simply be tried again.")


class ClaudeCodeProvider(Provider):
    name = "claude_code"

    def __init__(self) -> None:
        self._plans: dict[str, tuple[float, str | None, str | None]] = {}  # sign-in -> (when, subscription, tier)
        self._logins: dict[str, ClaudeLogin] = {}  # people's own sign-ins, by account key
        self._listed: tuple[float, list[ModelInfo], list[str]] = (float("-inf"), [], [])  # Claude Code's own list (cli_models); never read yet
        self._listing: asyncio.Task[None] | None = None

    def info(self) -> ProviderInfo:
        _when, models, efforts = self._listed
        return ProviderInfo(name=self.name, label="Claude Agent", models=[*MODELS, *models], efforts=merge_efforts(EFFORTS, efforts))

    async def catalog(self) -> ProviderInfo:
        """With the models of Claude Code's own list, asked again in the background once it is an hour old."""
        if time.monotonic() - self._listed[0] > CLI_MODELS_TTL_SECONDS and not (self._listing and not self._listing.done()):
            self._listing = asyncio.create_task(self._list())
        return self.info()

    async def _list(self) -> None:
        try:
            self._listed = (time.monotonic(), *await self._cli_models())
        except Exception as exc:
            log.info("Claude Code's list of models could not be read: %s", str(exc)[:200])
            self._listed = (time.monotonic() - CLI_MODELS_TTL_SECONDS + 300, self._listed[1], self._listed[2])  # again in 5 minutes

    async def _cli_models(self) -> tuple[list[ModelInfo], list[str]]:
        """Claude Code's list, from a start without a sign-in in an empty home: no account is touched, no token used."""
        with tempfile.TemporaryDirectory(prefix="plainboard-claude-models-") as home:
            env = {"HOME": home, "CLAUDE_CONFIG_DIR": str(Path(home) / ".claude"), "ANTHROPIC_API_KEY": "", "CLAUDE_CODE_OAUTH_TOKEN": "", "ANTHROPIC_AUTH_TOKEN": ""}
            async with ClaudeSDKClient(ClaudeAgentOptions(tools=[], setting_sources=[], cwd=home, env=env)) as client:
                info = await client.get_server_info() or {}
        return cli_models(info.get("models"))

    def auth_env(self, creds: Credentials, model: str = "", home: Path | None = None) -> dict[str, str]:
        """What tells Claude Code where the model is and how to sign in. ``model``: what the request
        runs on, for a provider that has to name it more than once (providers/endpoints.py). ``home``:
        the private folder of an own sign-in (``_home``)."""
        return auth_env(creds, home)

    @contextlib.asynccontextmanager
    async def _home(self, creds: Credentials) -> AsyncIterator[Path | None]:
        """The private folder of a person's own sign-in while the block runs; None for every other sign-in."""
        if creds.claude_auth_mode == "runner_login":  # the home's own, all runner_login accounts share it
            with SIGN_INS.in_home(HOME_SIGN_IN, DEFAULT_ACCOUNT, "This Claude sign-in"):
                yield None
            return
        if creds.claude_auth_mode != "own_login":
            yield None
            return
        if not creds.login_files:
            raise RuntimeError("This subscription is not signed in to Claude yet (Settings → My subscriptions).")
        async with SIGN_INS.home(self.name, creds.login_account, creds.login_files, SIGN_IN_FILES, prepare=private_config) as home:
            yield home

    def skills_folder(self, spec: RunSpec, workspace: Path) -> Path | None:
        # Claude Code loads the skills of its working directory with the project's settings (setting_sources),
        # which only the full toolset reads.
        return workspace / ".claude" / "skills" if spec.toolset == "full" else None

    def _options(
        self, spec: RunSpec, bridge: ToolBridge, stderr_tail: collections.deque[str], background: BackgroundWork | None = None, home: Path | None = None
    ) -> ClaudeAgentOptions:
        env = {**tool_env(spec.credentials), **self.auth_env(spec.credentials, spec.model, home), **spec.env}
        mcp_servers: dict[str, Any] = {s.name: external_mcp_config(s) for s in spec.mcp_servers if s.name != MCP_SERVER_NAME}
        mcp_servers[MCP_SERVER_NAME] = build_mcp_server(bridge)
        timer = ToolTimer()
        hooks = {"PreToolUse": [HookMatcher(hooks=[timer.start])], "PostToolUse": [HookMatcher(hooks=[notice_hook(spec.run_id, timer)])]}
        if background:
            hooks["Stop"] = [HookMatcher(hooks=[background.stop_hook])]
        allowed = [f"mcp__{MCP_SERVER_NAME}__{t.name}" for t in bridge.tools] + [f"mcp__{name}" for name in mcp_servers if name != MCP_SERVER_NAME]
        common: dict[str, Any] = dict(
            model=spec.model,
            cwd=str(bridge.workspace),
            env=env,
            permission_mode="bypassPermissions",
            resume=spec.resume_session_id,
            resume_session_at=resume_point(session_file(bridge.workspace, spec.resume_session_id)) if spec.resume_session_id else None,
            max_turns=spec.max_turns,
            mcp_servers=mcp_servers,
            allowed_tools=allowed,
            disallowed_tools=DISALLOWED_TOOLS,
            include_partial_messages=True,
            max_buffer_size=MAX_MESSAGE_BYTES,
            stderr=stderr_tail.append,
            hooks=hooks,
        )
        if spec.effort:
            common["effort"] = spec.effort
        if spec.toolset == "full":
            return ClaudeAgentOptions(
                tools={"type": "preset", "preset": "claude_code"},
                system_prompt={"type": "preset", "preset": "claude_code", "append": spec.system_prompt},
                setting_sources=["project"],
                **common,
            )
        return ClaudeAgentOptions(
            tools=[] if spec.toolset == "none" else READ_TOOLSET_BUILTINS if spec.toolset == "read" else BOARD_TOOLSET_BUILTINS,
            system_prompt=spec.system_prompt,
            setting_sources=[],
            **common,
        )

    async def run(self, spec: RunSpec, bridge: ToolBridge) -> AsyncIterator[RunEvent]:
        async with self._home(spec.credentials) as home:
            async for event in self._run(spec, bridge, home):
                yield event

    async def _run(self, spec: RunSpec, bridge: ToolBridge, home: Path | None) -> AsyncIterator[RunEvent]:
        stderr_tail: collections.deque[str] = collections.deque(maxlen=30)
        mapper = EventMapper()
        background = BackgroundWork(spec.run_id)
        got_result = False
        paused: ResultEvent | None = None  # an end that only paused the turn; the result if nothing follows
        try:
            async with _started(lambda: self._options(spec, bridge, stderr_tail, background, home), spec.run_id) as client:
                watch = UrgentNoticeWatch(client, spec.run_id)
                watchers = [asyncio.create_task(watch.watch()), asyncio.create_task(background.watch(client))]
                try:
                    await client.query(spec.prompt)
                    while True:
                        ended = False
                        async for message in client.receive_response():
                            watch.see(message)
                            background.see(message)
                            for event in mapper.map(message):
                                if event.type == "result":
                                    ended = True
                                    if watch.interrupted or background.holds(event):
                                        paused = event
                                        continue  # the turn goes on below
                                    got_result = True
                                yield event
                        if not ended:
                            break  # the session is gone
                        if watch.interrupted:
                            await client.query(watch.resume_prompt())
                        elif not background.waiting:
                            break
                finally:
                    for watcher in watchers:
                        watcher.cancel()
        except ProviderError:
            raise
        except Exception as exc:
            detail = "\n".join(stderr_tail)
            raise ProviderError(f"{type(exc).__name__}: {exc}\n{detail}".strip()) from exc
        if not got_result:
            yield paused or ResultEvent(is_error=True, error="Claude Code ended without a result", context_tokens=mapper.context_tokens)

    async def limits(self, credentials: Credentials) -> list[LimitWindow]:
        if credentials.claude_auth_mode == "api_key" or (credentials.claude_auth_mode == "own_login" and not credentials.login_files):
            return []
        if credentials.claude_auth_mode == "runner_login" and signins.taken(HOME_SIGN_IN, DEFAULT_ACCOUNT):
            return []
        async with self._home(credentials) as home:
            try:
                windows = await self._usage(credentials, home)
            except Exception as exc:
                log.info("Claude's usage could not be read, asking with a minimal turn: %s", str(exc)[:200])
                windows = []
            return windows or await self._limits_of_a_turn(credentials, home)

    async def _usage(self, credentials: Credentials, home: Path | None = None) -> list[LimitWindow]:
        options = ClaudeAgentOptions(tools=[], setting_sources=[], cwd="/tmp", env=self.auth_env(credentials, home=home))
        async with ClaudeSDKClient(options) as client:
            # Claude Code's usage request has no public method in the SDK yet.
            answer = await client._query._send_control_request({"subtype": "get_usage", "skip_behaviors": True}, timeout=20)
        if subscription := answer.get("subscription_type"):
            when, _, tier = self._plans.get(self._sign_in(credentials), (0.0, None, None))
            self._plans[self._sign_in(credentials)] = (when, subscription, tier)
        return usage_windows(answer.get("rate_limits"))

    @staticmethod
    def _sign_in(credentials: Credentials) -> str:
        token = credentials.claude_oauth_token or "" if credentials.claude_auth_mode == "oauth_token" else ""
        if credentials.claude_auth_mode == "own_login":
            token = credentials.login_account
        return credentials.claude_auth_mode + ":" + hashlib.sha256(token.encode()).hexdigest()[:16]

    async def plan(self, credentials: Credentials) -> tuple[str | None, float | None]:
        """From the account's profile (a token from ``claude setup-token`` may not be allowed to read
        it), the sign-in stored by ``claude login``, or the usage answer."""
        if credentials.claude_auth_mode == "api_key":
            return None, None
        key = self._sign_in(credentials)
        when, subscription, tier = self._plans.get(key, (0.0, None, None))
        if time.time() - when > PLAN_KEEP_SECONDS:
            read_subscription, read_tier = await self._read_plan(credentials)
            subscription, tier = read_subscription or subscription, read_tier or tier
            self._plans[key] = (time.time(), subscription, tier)
        return plan_of(subscription, tier)

    async def _read_plan(self, credentials: Credentials) -> tuple[str | None, str | None]:
        if credentials.claude_auth_mode in ("runner_login", "own_login"):
            try:
                text = (credentials.login_files or {}).get(SIGN_IN_FILES[0], "") if credentials.claude_auth_mode == "own_login" else (shared_home() / ".credentials.json").read_text()
                stored = json.loads(text).get("claudeAiOauth") or {}
            except (OSError, ValueError, AttributeError):
                return None, None
            return stored.get("subscriptionType"), stored.get("rateLimitTier")
        if not credentials.claude_oauth_token:
            return None, None
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(PROFILE_URL, headers={"Authorization": f"Bearer {credentials.claude_oauth_token}", "anthropic-beta": "oauth-2025-04-20"})
            resp.raise_for_status()
            organization = (resp.json() or {}).get("organization") or {}
        except Exception as exc:
            log.info("Claude's plan could not be read from the profile: %s", str(exc)[:200])
            return None, None
        return organization.get("organization_type"), organization.get("rate_limit_tier")

    async def _limits_of_a_turn(self, credentials: Credentials, home: Path | None = None) -> list[LimitWindow]:
        """The limits as a turn reports them. A subscription that is signed in with a token
        (``claude setup-token``) answers the usage request with nothing: the token is for model
        requests only. Every model request reports the limits, though, so the smallest one there
        is shows them: one word to the small model, a few tokens. Without this, a subscription
        that no agent is running on keeps the numbers of its last turn for days."""
        options = ClaudeAgentOptions(
            model=PROBE_MODEL, tools=[], system_prompt="Reply with the single word: ok", setting_sources=[], max_turns=1, cwd="/tmp", env=self.auth_env(credentials, home=home)
        )
        windows: dict[str, LimitWindow] = {}

        async def ask() -> None:
            async with ClaudeSDKClient(options) as client:
                await client.query("ok")
                async for message in client.receive_response():
                    if isinstance(message, RateLimitEvent):
                        windows.update({w.name: w for w in limit_windows(message.rate_limit_info)})

        try:
            await asyncio.wait_for(ask(), timeout=60)
        except Exception as exc:  # a subscription that is used up refuses the request, after reporting that
            log.info("Minimal turn for Claude's limits ended early: %s", str(exc)[:200])
        return list(windows.values())

    async def complete(self, spec: CompletionSpec) -> CompletionResult:
        async with self._home(spec.credentials) as home:
            return await self._complete(spec, home)

    async def _complete(self, spec: CompletionSpec, home: Path | None) -> CompletionResult:
        stderr_tail: collections.deque[str] = collections.deque(maxlen=30)
        options = ClaudeAgentOptions(
            model=spec.model,
            tools=[],
            system_prompt=spec.system_prompt,
            setting_sources=[],
            max_turns=1,
            cwd="/tmp",
            env=self.auth_env(spec.credentials, spec.model, home),
            stderr=stderr_tail.append,
            **({"effort": spec.effort} if spec.effort else {}),
        )
        texts: list[str] = []
        result: ResultMessage | None = None
        try:
            async with ClaudeSDKClient(options=options) as client:
                await client.query(spec.prompt)
                async for message in client.receive_response():
                    if isinstance(message, AssistantMessage):
                        texts.extend(b.text for b in message.content if isinstance(b, TextBlock))
                    elif isinstance(message, ResultMessage):
                        result = message
        except Exception as exc:
            raise RuntimeError(f"{exc}\n" + "\n".join(stderr_tail)) from exc
        if result is None or result.is_error:
            reason = (result.result or "; ".join(result.errors or [])) if result else "no result"
            raise RuntimeError(f"Completion failed: {reason}")
        usage = result.usage or {}
        return CompletionResult(
            text="\n".join(texts).strip() or (result.result or ""),
            input_tokens=int(usage.get("input_tokens") or 0) + int(usage.get("cache_read_input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            cost_usd=float(result.total_cost_usd or 0.0),
        )

    # A person's own browser sign-in (Settings → My subscriptions). The installation's runner_login stays
    # ``claude`` in the runner's terminal.
    async def account(self, key: str, own: bool = False) -> ProviderAccount | None:
        if not own:
            return None
        login = self._logins.get(key)
        if login is None:
            return ProviderAccount()
        waiting = login.active
        return ProviderAccount(signed_in=login.succeeded, login_url=login.url if waiting else None, needs_code=waiting, login_error=login.error)

    async def login(self, key: str, own: bool = False) -> ProviderAccount:
        if not own:
            raise NotImplementedError("Claude's sign-in on the runner is made in its terminal: claude")
        await self._end_login(key)
        login = self._logins[key] = ClaudeLogin(self.name, key)
        try:
            await login.start()
        except BaseException:
            self._logins.pop(key, None)
            await login.cancel()
            raise
        return await self.account(key, own=True) or ProviderAccount()

    async def login_code(self, key: str, code: str) -> ProviderAccount:
        login = self._logins.get(key)
        if login is None:
            raise RuntimeError("No sign-in is waiting for a code. Start it again.")
        await login.code(code)
        return await self.account(key, own=True) or ProviderAccount()

    async def logout(self, key: str, own: bool = False) -> ProviderAccount:
        if not own:
            raise NotImplementedError("Claude's sign-in on the runner is made in its terminal: claude")
        await self._end_login(key)
        SIGN_INS.forget(key)
        return ProviderAccount()

    async def take_over(self, key: str) -> dict[str, str]:
        """The home's sign-in (``claude`` in the runner's terminal): one for all runner_login accounts."""
        return SIGN_INS.take_over(HOME_SIGN_IN, DEFAULT_ACCOUNT, shared_home(), SIGN_IN_FILES)

    async def refresh_sign_in(self, credentials: Credentials) -> None:
        """Claude Code refreshes a token when it ends, in every runner that has the same file at that moment;
        here it is made to do so earlier, on one runner: the token is marked as ended, then the usage is read."""
        if credentials.claude_auth_mode != "own_login" or not credentials.login_files:
            return
        async with self._home(credentials) as home:
            files = signins.read(home, SIGN_IN_FILES)
            if not home or not refresh_due(files):
                return
            stored = json.loads(files[SIGN_IN_FILES[0]])
            stored["claudeAiOauth"]["expiresAt"] = 0
            signins.write(home, {SIGN_IN_FILES[0]: json.dumps(stored)})
            await self._usage(credentials, home)

    async def _end_login(self, key: str) -> None:
        if (login := self._logins.pop(key, None)) is not None:
            await login.cancel()
