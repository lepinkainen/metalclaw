"""Shared self-modification plumbing: the process-wide busy slot and the git /
subprocess helpers used by ``live_tool`` (``/add-tool``)."""

import contextlib
import logging
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path

log = logging.getLogger(__name__)


class SelfModificationBusy(RuntimeError):
    """Another ``/add-tool`` run is in progress in this process."""


# One slot for every self-modification path (today only ``/add-tool`` in
# live_tool; ``/self-edit`` went with the CLI). A run diffs the working tree
# before and after spawning ``claude -p``; two overlapping runs would each see
# the other's new file, abort with "expected 1 new file, got 2", and revert
# each other (#35).
_BUSY = threading.Lock()


@contextlib.contextmanager
def busy_slot() -> Iterator[None]:
    """Hold the process-wide self-modification slot; non-blocking.

    Raises ``SelfModificationBusy`` immediately when another run holds it.
    """
    if not _BUSY.acquire(blocking=False):
        raise SelfModificationBusy("another self-modification is in progress")
    try:
        yield
    finally:
        _BUSY.release()


def is_busy() -> bool:
    return _BUSY.locked()


def _read_file(path: Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return f"<could not read {path.name}>"


def _run(cmd: list[str], cwd: Path, timeout: int = 60) -> tuple[int, str, str]:
    # Don't log the full argv: the claude -p prompt embeds whole source files.
    label = " ".join(cmd[:2]) if cmd and cmd[0] == "claude" else " ".join(cmd)
    log.info("subprocess start: %.200s (cwd=%s timeout=%ds)", label, cwd, timeout)
    t0 = time.monotonic()
    try:
        r = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        log.error("subprocess timed out after %ds: %.200s", timeout, label)
        return -1, "", f"timed out after {timeout}s"
    log.info(
        "subprocess exit=%d duration=%.2fs stdout=%d chars stderr=%d chars: %.200s",
        r.returncode,
        time.monotonic() - t0,
        len(r.stdout),
        len(r.stderr),
        label,
    )
    if r.returncode != 0:
        log.debug("subprocess stderr: %.500s", r.stderr)
    return r.returncode, r.stdout, r.stderr


def _dirty_tracked(repo_root: Path) -> set[str]:
    _, out, _ = _run(["git", "diff", "--name-only"], repo_root)
    return set(out.strip().splitlines())


def _untracked(repo_root: Path) -> set[str]:
    _, out, _ = _run(["git", "ls-files", "--others", "--exclude-standard"], repo_root)
    return set(out.strip().splitlines())
