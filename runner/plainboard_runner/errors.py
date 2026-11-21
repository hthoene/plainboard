"""What went wrong in a turn: the text the app shows, and a file that keeps the last errors.

The runner's logs go to the container's output, which can't be read afterwards. The tracebacks of the
last errors are also written to a file in the permanent home, capped in size.
"""

from __future__ import annotations

import logging
import os
import traceback
from datetime import UTC, datetime
from pathlib import Path

ERROR_LOG = Path(os.environ.get("PLAINBOARD_RUNNER_ERRORS", Path.home() / ".plainboard" / "runner-errors.log"))
ERROR_LOG_MAX_BYTES = 256 * 1024

log = logging.getLogger("plainboard.runner")


def describe(exc: BaseException) -> str:
    """``ExceptionType: text`` - never empty, also for an exception without a message."""
    from .providers.base import ProviderError

    if isinstance(exc, ProviderError) and str(exc).strip():  # its text names the original error already
        return str(exc).strip()
    text = str(exc).strip()
    name = type(exc).__name__
    return f"{name}: {text}" if text else name


def record(what: str, exc: BaseException) -> None:
    """Log the traceback and append it to ERROR_LOG, which keeps the last ERROR_LOG_MAX_BYTES."""
    trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    log.error("%s: %s\n%s", what, describe(exc), trace)
    try:
        ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
        if ERROR_LOG.exists() and ERROR_LOG.stat().st_size > ERROR_LOG_MAX_BYTES:
            tail = ERROR_LOG.read_bytes()[-ERROR_LOG_MAX_BYTES // 2 :]
            ERROR_LOG.write_bytes(tail[tail.find(b"\n=== ") + 1 :] if b"\n=== " in tail else b"")
        with ERROR_LOG.open("a", encoding="utf-8") as handle:
            handle.write(f"=== {datetime.now(UTC).isoformat(timespec='seconds')} {what}: {describe(exc)}\n{trace}\n")
    except OSError:
        log.warning("Could not write %s", ERROR_LOG, exc_info=True)
