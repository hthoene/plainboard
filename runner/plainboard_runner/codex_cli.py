"""The Codex CLI keeps itself current: new models often need a newer CLI than the image has, and a new image
needs ``docker compose pull``. Now and then the runner asks npm for the newest version, installs it next to the
others in its home (``ROOT/<version>``) and tries it: an app server must start and list models. Only then do new
app servers use it (``binary``); those already running keep theirs. A version that fails its try is not tried
again; one that can't be installed changes nothing. At most ``KEEP`` versions stay in the home.

``PLAINBOARD_CODEX_BIN`` names a CLI of one's own and turns this off, as does ``PLAINBOARD_CODEX_UPDATE=0``."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path

log = logging.getLogger(__name__)

PACKAGE = "@openai/codex"
IMAGE_BIN = "codex"
ROOT = Path.home() / ".plainboard" / "codex-cli"
KEEP = 2  # the version in use and the one before it
CHECK_SECONDS = 6 * 3600
FIRST_CHECK_SECONDS = 120
COMMAND_TIMEOUT = 600
VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def own_bin() -> str | None:
    return os.environ.get("PLAINBOARD_CODEX_BIN") or None


def enabled() -> bool:
    return own_bin() is None and os.environ.get("PLAINBOARD_CODEX_UPDATE", "1") != "0"


def version_of(text: str) -> tuple[int, int, int] | None:
    """A plain release (``0.171.0``) in ``text``; None for anything else, e.g. an alpha."""
    match = VERSION.search(text or "")
    if not match or re.search(r"\d+\.\d+\.\d+-", text):
        return None
    return int(match[1]), int(match[2]), int(match[3])


def _bin(root: Path, version: str) -> Path:
    return root / version / "node_modules" / ".bin" / "codex"


def _current(root: Path) -> str | None:
    try:
        version = (root / "current").read_text().strip()
    except OSError:
        return None
    return version if version_of(version) and _bin(root, version).exists() else None


def binary(root: Path = ROOT) -> str:
    """The CLI a new app server starts with."""
    if mine := own_bin():
        return mine
    version = _current(root)
    return str(_bin(root, version)) if version else IMAGE_BIN


async def _run(*args: str, timeout: float = COMMAND_TIMEOUT, env: dict[str, str] | None = None) -> str:
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env={**os.environ, **(env or {})})
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        raise RuntimeError(f"{args[0]} took longer than {timeout:.0f} s") from None
    text = out.decode(errors="replace").strip()
    if proc.returncode:
        raise RuntimeError(f"{' '.join(args[:3])} failed: {text[-500:]}")
    return text


async def latest() -> str:
    return (await _run("npm", "view", PACKAGE, "version", timeout=60)).strip()


async def installed(bin_: str) -> str:
    return await _run(bin_, "--version", timeout=60)


async def install(version: str, root: Path = ROOT) -> Path:
    """``version`` into ``root/<version>``, through a folder of its own so a broken install leaves nothing behind."""
    root.mkdir(parents=True, exist_ok=True)
    target = root / version
    if _bin(root, version).exists():
        return _bin(root, version)
    work = Path(tempfile.mkdtemp(prefix=f".{version}-", dir=root))
    try:
        await _run(
            "npm", "install", "--prefix", str(work / "pkg"), "--no-audit", "--no-fund", "--no-save", "--cache", str(work / "cache"), f"{PACKAGE}@{version}"
        )  # fmt: skip
        shutil.rmtree(target, ignore_errors=True)
        (work / "pkg").rename(target)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return _bin(root, version)


async def try_out(bin_: Path) -> None:
    """An app server of the new CLI starts and lists models, in a home of its own (no sign-in): what PlainBoard needs first."""
    from .providers.codex import AppServer, list_models

    with tempfile.TemporaryDirectory(prefix="plainboard-codex-try-") as home:
        async with AppServer(Path(home), binary=str(bin_)) as server:
            if not (await list_models(server))[0]:
                raise RuntimeError("it lists no models")


def prune(root: Path = ROOT, keep: int = KEEP) -> None:
    """Leave the version in use and the newest others, ``keep`` in all; leftovers of broken installs go too."""
    if not root.is_dir():
        return
    current = _current(root)
    versions = sorted((p for p in root.iterdir() if p.is_dir() and version_of(p.name) and p.name != current), key=lambda p: version_of(p.name), reverse=True)
    for path in versions[keep - 1 if current else keep :]:
        shutil.rmtree(path, ignore_errors=True)
    for path in root.glob(".*-*"):
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)


async def update(root: Path = ROOT, check: Callable[[Path], Awaitable[None]] = try_out) -> str | None:
    """Install and switch to a newer CLI if there is one. Returns the version switched to, None if none."""
    if not enabled():
        return None
    newest = await latest()
    have = version_of(await installed(binary(root)))
    if version_of(newest) is None or (have and version_of(newest) <= have):
        return None
    if (root / "failed").is_file() and (root / "failed").read_text().strip() == newest:
        return None  # tried already
    bin_ = await install(newest, root)
    try:
        await check(bin_)
    except Exception as exc:
        log.warning("Codex CLI %s failed its try, staying with the one in use: %s", newest, str(exc)[:300])
        (root / "failed").write_text(newest)
        shutil.rmtree(root / newest, ignore_errors=True)
        return None
    (root / "current").write_text(newest)
    prune(root)
    log.info("Codex CLI %s is used for new runs from now on", newest)
    return newest


async def keep_current() -> None:
    """Look for a newer CLI shortly after the start and every few hours."""
    await asyncio.sleep(FIRST_CHECK_SECONDS)
    while enabled():
        try:
            await update()
        except Exception as exc:  # npm away, no network, a full disk: the CLI in use stays
            log.info("No newer Codex CLI now: %s", str(exc)[:300])
        await asyncio.sleep(CHECK_SECONDS)
