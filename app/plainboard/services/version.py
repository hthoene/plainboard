"""Which PlainBoard is running, and whether a newer one exists.

The image knows the commit it was built from (``read_commit``, run by the Dockerfile), and a release
image its version (/app/version, see RELEASING.md). A watcher without any model asks the upstream
repository now and then how many commits it is ahead of that commit, or for a release, how many
newer releases exist (orchestration/update_watcher.py); the UI shows a quiet notice. Only the
commit id is sent.

Standard library only up to ``read_commit``: the image build runs it before anything is configured.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

SHA = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?")
RELEASE = re.compile(r"\d+\.\d+\.\d+")


def read_commit(git_dir: Path) -> str:
    """The commit a checkout is on, from its .git folder; "" if that cannot be told (no git data in
    the build context, a worktree, a broken checkout). Reads files only: the image needs no git."""
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if SHA.fullmatch(head):
            return head  # detached
        if not head.startswith("ref: "):
            return ""
        ref = head.removeprefix("ref: ").strip()
        loose = git_dir / ref
        if loose.is_file():
            sha = loose.read_text(encoding="utf-8").strip()
            return sha if SHA.fullmatch(sha) else ""
        for line in (git_dir / "packed-refs").read_text(encoding="utf-8").splitlines():
            sha, _, name = line.partition(" ")
            if name.strip() == ref and SHA.fullmatch(sha):
                return sha
    except (OSError, UnicodeDecodeError):
        pass
    return ""


if __name__ == "__main__":  # python version.py <.git folder>: used by the image build
    print(read_commit(Path(sys.argv[1])))
    raise SystemExit(0)


@dataclass
class Upstream:
    behind: int | None = None  # commits the upstream branch has that this build has not; None: not known
    url: str = ""  # where to read them
    checked_at: datetime | None = None
    latest: str = ""  # for a release: the newest release, if newer


_upstream = Upstream()
_commit: str | None = None
_version: str | None = None


def running_commit() -> str:
    """The commit this app was built from; "" if the build could not tell."""
    global _commit
    if _commit is None:
        from ..config import get_config

        config = get_config()
        _commit = config.commit.strip().lower() or read_commit_file(config.commit_file)
    return _commit


def running_version() -> str:
    """The release this app is ("X.Y.Z"); "" for a build of a commit (:edge, a local build)."""
    global _version
    if _version is None:
        from ..config import get_config

        config = get_config()
        value = config.version.strip().removeprefix("v")
        if not value:
            try:
                value = config.version_file.read_text(encoding="utf-8").strip().removeprefix("v")
            except OSError:
                value = ""
        _version = value if RELEASE.fullmatch(value) else ""
    return _version


def name() -> str:
    """What the UI calls this version: "X.Y.Z" for a release, "edge-<7 characters of the commit>" otherwise."""
    return running_version() or f"edge-{running_commit()[:7]}"


def read_commit_file(path: Path) -> str:
    try:
        sha = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return sha if SHA.fullmatch(sha) else ""


def record(behind: int | None, url: str, now: datetime | None, latest: str = "") -> None:
    _upstream.behind, _upstream.url, _upstream.checked_at, _upstream.latest = behind, url, now, latest


def forget() -> None:
    record(None, "", None)


HISTORY_KEY = "version_history"
HISTORY_MAX = 400


async def note_start(session, now: datetime) -> None:
    """Write down that PlainBoard runs another version than last time. The statistics mark these
    moments, so that what an update changed can be read off the numbers before and after it."""
    from ..models import Setting  # not at the top: the image build runs this file on its own

    commit = running_commit()
    if not commit:
        return
    row = await session.get(Setting, HISTORY_KEY) or Setting(key=HISTORY_KEY, secret=False)
    known = json.loads(row.value) if row.value else []
    if known and known[-1]["commit"] == commit:
        return
    row.value = json.dumps([*known, {"commit": commit, "at": now.isoformat()}][-HISTORY_MAX:])
    session.add(row)
    await session.commit()


async def history(session) -> list[dict]:
    """The versions PlainBoard ran, oldest first: ``{"commit": short id, "at": naive UTC datetime}``."""
    from ..models import Setting

    row = await session.get(Setting, HISTORY_KEY)
    return [{"commit": v["commit"][:10], "at": datetime.fromisoformat(v["at"])} for v in (json.loads(row.value) if row and row.value else [])]


def current() -> dict | None:
    """For the UI; None if this build does not know its commit."""
    commit = running_commit()
    if not commit:
        return None
    return {"commit": commit[:10], "name": name(), "release": bool(running_version()), "behind": _upstream.behind, "latest": _upstream.latest, "url": _upstream.url}
