"""CLI-style command system with chain parsing and presentation layer.

Commands are registered via the @command decorator and exposed through
a single `run(command="...")` tool.  Supports Unix-style composition:
  |   pipe stdout to next command's stdin
  &&  run next only if previous succeeded
  ||  run next only if previous failed
  ;   run next regardless
"""

from __future__ import annotations

import math
import os
import platform
import re
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

@dataclass
class Command:
    name: str
    summary: str  # one-line description for command listing
    usage: str  # shown when called with no/wrong args
    func: Callable[..., "Result"]


@dataclass
class Result:
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0


COMMANDS: dict[str, Command] = {}


def command(*, name: str, summary: str, usage: str):
    """Decorator that registers a CLI-style command."""
    def decorator(func: Callable[..., Result]) -> Callable[..., Result]:
        COMMANDS[name] = Command(name=name, summary=summary, usage=usage, func=func)
        return func
    return decorator


def command_listing() -> str:
    """One-line-per-command listing for injection into tool description."""
    lines = []
    for cmd in COMMANDS.values():
        lines.append(f"  {cmd.name:12s} — {cmd.summary}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Chain parser  —  splits on  |  &&  ||  ;
# ---------------------------------------------------------------------------

_OPERATORS = re.compile(r"\s*(\|\||&&|[|;])\s*")


def _tokenize_chain(raw: str) -> list[tuple[str, str]]:
    """Return [(operator, command_string), ...].

    The first entry always has operator="" (it's the head of the chain).
    """
    parts = _OPERATORS.split(raw)
    # parts looks like: [cmd, op, cmd, op, cmd, ...]
    result: list[tuple[str, str]] = []
    op = ""
    for i, chunk in enumerate(parts):
        chunk = chunk.strip()
        if not chunk:
            continue
        if i % 2 == 0:  # command segment
            result.append((op, chunk))
            op = ""
        else:  # operator segment
            op = chunk
    return result


# ---------------------------------------------------------------------------
# Execution engine
# ---------------------------------------------------------------------------

def _parse_argv(raw: str) -> list[str]:
    """Shell-style split, tolerant of unbalanced quotes."""
    try:
        return shlex.split(raw)
    except ValueError:
        return raw.split()


def _dispatch(argv: list[str], stdin: str | None) -> Result:
    """Route a single command to its handler."""
    if not argv:
        return Result(stderr="empty command", exit_code=1)
    name = argv[0]
    cmd = COMMANDS.get(name)
    if cmd is None:
        available = ", ".join(COMMANDS)
        return Result(
            stderr=f"unknown command: {name}. Available: {available}",
            exit_code=127,
        )
    try:
        return cmd.func(argv[1:], stdin)
    except Exception as e:
        return Result(stderr=f"{name}: {e}", exit_code=1)


def execute_chain(raw: str) -> Result:
    """Parse and execute a full chain expression, return final Result."""
    steps = _tokenize_chain(raw)
    if not steps:
        return Result(stderr="empty command", exit_code=1)

    last = Result()
    stdin: str | None = None

    for op, cmd_str in steps:
        # Evaluate chain operators
        if op == "&&" and last.exit_code != 0:
            break
        if op == "||" and last.exit_code == 0:
            continue
        if op == "|":
            stdin = last.stdout
        else:
            stdin = None

        argv = _parse_argv(cmd_str)
        t0 = time.monotonic()
        last = _dispatch(argv, stdin)
        last._duration_ms = round((time.monotonic() - t0) * 1000)  # type: ignore[attr-defined]

    return last


# ---------------------------------------------------------------------------
# Presentation layer  (Layer 2)
# ---------------------------------------------------------------------------

_MAX_LINES = 200
_MAX_BYTES = 50_000


def _is_binary(text: str) -> bool:
    """Heuristic: null bytes or high ratio of control characters."""
    if "\x00" in text:
        return True
    if not text:
        return False
    control = sum(1 for c in text[:4096] if ord(c) < 32 and c not in "\n\r\t")
    return control / min(len(text), 4096) > 0.10


def present(result: Result) -> str:
    """Format a Result for the LLM, applying guards and truncation."""
    duration = getattr(result, "_duration_ms", 0)
    meta = f"[exit:{result.exit_code} | {duration}ms]"

    output = result.stdout
    stderr_part = ""
    if result.stderr:
        stderr_part = f"\n[stderr] {result.stderr}"

    # Binary guard
    if _is_binary(output):
        return f"[error] binary data detected ({len(output)} bytes).{stderr_part}\n{meta}"

    # Truncation
    lines = output.split("\n")
    if len(lines) > _MAX_LINES or len(output.encode()) > _MAX_BYTES:
        truncated = "\n".join(lines[:_MAX_LINES])
        total = len(lines)
        size_kb = round(len(output.encode()) / 1024, 1)
        return (
            f"{truncated}\n"
            f"--- output truncated ({total} lines, {size_kb}KB) ---\n"
            f"Explore with: run(command=\"<your command> | head 50\") or pipe through grep"
            f"{stderr_part}\n{meta}"
        )

    body = output if output else ""
    return f"{body}{stderr_part}\n{meta}" if body or stderr_part else meta


# ---------------------------------------------------------------------------
# Built-in commands
# ---------------------------------------------------------------------------

@command(
    name="help",
    summary="List available commands or show help for a specific command",
    usage="help [command]",
)
def _help(args: list[str], stdin: str | None) -> Result:
    if args:
        cmd = COMMANDS.get(args[0])
        if cmd is None:
            return Result(stderr=f"unknown command: {args[0]}. Use 'help' to list all.", exit_code=1)
        return Result(stdout=f"{cmd.name} — {cmd.summary}\nUsage: {cmd.usage}")
    listing = command_listing()
    return Result(stdout=f"Available commands:\n{listing}\n\nUse 'help <command>' for details.")


@command(
    name="calc",
    summary="Evaluate a math expression",
    usage="calc <expression>  (e.g. calc 2**10, calc sqrt(144), calc sin(pi/4))",
)
def _calc(args: list[str], stdin: str | None) -> Result:
    if not args:
        return Result(stderr="usage: calc <expression>  (e.g. calc 2**10)", exit_code=1)
    expr = " ".join(args)
    # Safe math namespace
    allowed = {k: getattr(math, k) for k in dir(math) if not k.startswith("_")}
    allowed.update({"abs": abs, "round": round, "min": min, "max": max})
    try:
        val = eval(expr, {"__builtins__": {}}, allowed)  # noqa: S307
        return Result(stdout=str(val))
    except Exception as e:
        return Result(stderr=f"calc: {e}", exit_code=1)


@command(
    name="cat",
    summary="Read a text file (use 'cat -b' for binary size info)",
    usage="cat [-b] <path>",
)
def _cat(args: list[str], stdin: str | None) -> Result:
    if not args:
        if stdin is not None:
            return Result(stdout=stdin)
        return Result(stderr="usage: cat [-b] <path>", exit_code=1)
    binary_mode = False
    path_args = []
    for a in args:
        if a == "-b":
            binary_mode = True
        else:
            path_args.append(a)
    if not path_args:
        return Result(stderr="usage: cat [-b] <path>", exit_code=1)
    path = path_args[0]
    if not os.path.isfile(path):
        return Result(stderr=f"cat: no such file: {path}", exit_code=1)
    if binary_mode:
        size = os.path.getsize(path)
        return Result(stdout=f"{path}: {size} bytes (binary)")
    try:
        with open(path, "r", errors="replace") as f:
            return Result(stdout=f.read())
    except Exception as e:
        return Result(stderr=f"cat: {e}", exit_code=1)


@command(
    name="ls",
    summary="List files in a directory",
    usage="ls [path] [-a]  (default: current directory, -a includes hidden files)",
)
def _ls(args: list[str], stdin: str | None) -> Result:
    show_hidden = "-a" in args
    path_args = [a for a in args if a != "-a"]
    path = path_args[0] if path_args else "."
    if not os.path.isdir(path):
        return Result(stderr=f"ls: no such directory: {path}", exit_code=1)
    try:
        entries = sorted(os.listdir(path))
        if not show_hidden:
            entries = [e for e in entries if not e.startswith(".")]
        # Mark directories
        lines = []
        for e in entries:
            full = os.path.join(path, e)
            suffix = "/" if os.path.isdir(full) else ""
            lines.append(f"{e}{suffix}")
        return Result(stdout="\n".join(lines))
    except Exception as e:
        return Result(stderr=f"ls: {e}", exit_code=1)


@command(
    name="grep",
    summary="Filter lines matching a pattern (supports -i, -v, -c)",
    usage="grep [-i] [-v] [-c] <pattern> [file]  (reads stdin if no file given)",
)
def _grep(args: list[str], stdin: str | None) -> Result:
    ignore_case = False
    invert = False
    count_only = False
    rest = []
    for a in args:
        if a == "-i":
            ignore_case = True
        elif a == "-v":
            invert = True
        elif a == "-c":
            count_only = True
        else:
            rest.append(a)
    if not rest:
        return Result(stderr="usage: grep [-i] [-v] [-c] <pattern> [file]", exit_code=1)
    pattern = rest[0]
    # Get input text
    if len(rest) > 1:
        path = rest[1]
        if not os.path.isfile(path):
            return Result(stderr=f"grep: no such file: {path}", exit_code=1)
        with open(path, errors="replace") as f:
            text = f.read()
    elif stdin is not None:
        text = stdin
    else:
        return Result(stderr="grep: no input (provide a file or pipe stdin)", exit_code=1)

    flags = re.IGNORECASE if ignore_case else 0
    try:
        compiled = re.compile(pattern, flags)
    except re.error as e:
        return Result(stderr=f"grep: bad pattern: {e}", exit_code=1)

    lines = text.split("\n")
    matched = [l for l in lines if bool(compiled.search(l)) != invert]
    if count_only:
        return Result(stdout=str(len(matched)))
    if not matched:
        return Result(stdout="", exit_code=1)
    return Result(stdout="\n".join(matched))


@command(
    name="head",
    summary="Show first N lines (default 10)",
    usage="head [N] [file]  (reads stdin if no file given)",
)
def _head(args: list[str], stdin: str | None) -> Result:
    n = 10
    path = None
    for a in args:
        if a.isdigit():
            n = int(a)
        else:
            path = a
    if path:
        if not os.path.isfile(path):
            return Result(stderr=f"head: no such file: {path}", exit_code=1)
        with open(path, errors="replace") as f:
            text = f.read()
    elif stdin is not None:
        text = stdin
    else:
        return Result(stderr="usage: head [N] [file]", exit_code=1)
    lines = text.split("\n")
    return Result(stdout="\n".join(lines[:n]))


@command(
    name="tail",
    summary="Show last N lines (default 10)",
    usage="tail [N] [file]  (reads stdin if no file given)",
)
def _tail(args: list[str], stdin: str | None) -> Result:
    n = 10
    path = None
    for a in args:
        if a.isdigit():
            n = int(a)
        else:
            path = a
    if path:
        if not os.path.isfile(path):
            return Result(stderr=f"tail: no such file: {path}", exit_code=1)
        with open(path, errors="replace") as f:
            text = f.read()
    elif stdin is not None:
        text = stdin
    else:
        return Result(stderr="usage: tail [N] [file]", exit_code=1)
    lines = text.split("\n")
    return Result(stdout="\n".join(lines[-n:]))


@command(
    name="wc",
    summary="Count lines, words, or characters",
    usage="wc [-l] [-w] [-c] [file]  (reads stdin if no file given, default: -l)",
)
def _wc(args: list[str], stdin: str | None) -> Result:
    count_lines = False
    count_words = False
    count_chars = False
    path = None
    for a in args:
        if a == "-l":
            count_lines = True
        elif a == "-w":
            count_words = True
        elif a == "-c":
            count_chars = True
        else:
            path = a
    if not (count_lines or count_words or count_chars):
        count_lines = True
    if path:
        if not os.path.isfile(path):
            return Result(stderr=f"wc: no such file: {path}", exit_code=1)
        with open(path, errors="replace") as f:
            text = f.read()
    elif stdin is not None:
        text = stdin
    else:
        return Result(stderr="usage: wc [-l] [-w] [-c] [file]", exit_code=1)
    parts = []
    if count_lines:
        parts.append(str(text.count("\n")))
    if count_words:
        parts.append(str(len(text.split())))
    if count_chars:
        parts.append(str(len(text)))
    return Result(stdout=" ".join(parts))


@command(
    name="sort",
    summary="Sort lines alphabetically (supports -r for reverse, -n for numeric)",
    usage="sort [-r] [-n] [file]  (reads stdin if no file given)",
)
def _sort(args: list[str], stdin: str | None) -> Result:
    reverse = "-r" in args
    numeric = "-n" in args
    path_args = [a for a in args if not a.startswith("-")]
    if path_args:
        path = path_args[0]
        if not os.path.isfile(path):
            return Result(stderr=f"sort: no such file: {path}", exit_code=1)
        with open(path, errors="replace") as f:
            text = f.read()
    elif stdin is not None:
        text = stdin
    else:
        return Result(stderr="usage: sort [-r] [-n] [file]", exit_code=1)
    lines = text.split("\n")
    if numeric:
        def key(l: str):
            m = re.match(r"-?\d+\.?\d*", l.strip())
            return float(m.group()) if m else 0.0
        lines.sort(key=key, reverse=reverse)
    else:
        lines.sort(reverse=reverse)
    return Result(stdout="\n".join(lines))


@command(
    name="uniq",
    summary="Remove adjacent duplicate lines (supports -c for counts)",
    usage="uniq [-c] [file]  (reads stdin if no file given)",
)
def _uniq(args: list[str], stdin: str | None) -> Result:
    show_counts = "-c" in args
    path_args = [a for a in args if a != "-c"]
    if path_args:
        path = path_args[0]
        if not os.path.isfile(path):
            return Result(stderr=f"uniq: no such file: {path}", exit_code=1)
        with open(path, errors="replace") as f:
            text = f.read()
    elif stdin is not None:
        text = stdin
    else:
        return Result(stderr="usage: uniq [-c] [file]", exit_code=1)
    lines = text.split("\n")
    result: list[str] = []
    counts: list[int] = []
    for line in lines:
        if result and line == result[-1]:
            counts[-1] += 1
        else:
            result.append(line)
            counts.append(1)
    if show_counts:
        out = "\n".join(f"{c:>4} {l}" for c, l in zip(counts, result))
    else:
        out = "\n".join(result)
    return Result(stdout=out)


@command(
    name="sysinfo",
    summary="Show system information (OS, CPU, memory, uptime)",
    usage="sysinfo",
)
def _sysinfo(args: list[str], stdin: str | None) -> Result:
    info = [
        f"OS: {platform.system()} {platform.release()}",
        f"Architecture: {platform.machine()}",
        f"Python: {platform.python_version()}",
        f"Hostname: {platform.node()}",
    ]
    # CPU count
    cpu_count = os.cpu_count()
    if cpu_count:
        info.append(f"CPUs: {cpu_count}")
    # Memory via /proc/meminfo (Linux)
    try:
        with open("/proc/meminfo") as f:
            meminfo = f.read()
        for line in meminfo.split("\n"):
            if line.startswith("MemTotal:"):
                kb = int(line.split()[1])
                info.append(f"Memory: {round(kb / 1024 / 1024, 1)} GB")
                break
    except OSError:
        pass
    # Uptime
    try:
        with open("/proc/uptime") as f:
            secs = float(f.read().split()[0])
        days, rem = divmod(int(secs), 86400)
        hours, rem = divmod(rem, 3600)
        mins, _ = divmod(rem, 60)
        info.append(f"Uptime: {days}d {hours}h {mins}m")
    except OSError:
        pass
    return Result(stdout="\n".join(info))


@command(
    name="echo",
    summary="Print text (useful in chains)",
    usage="echo <text...>",
)
def _echo(args: list[str], stdin: str | None) -> Result:
    return Result(stdout=" ".join(args))


@command(
    name="date",
    summary="Show current date and time",
    usage="date",
)
def _date(args: list[str], stdin: str | None) -> Result:
    from datetime import datetime
    return Result(stdout=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))


@command(
    name="env",
    summary="Show selected environment variables (PATH, HOME, USER, SHELL)",
    usage="env [name]  (show specific var, or list common ones)",
)
def _env(args: list[str], stdin: str | None) -> Result:
    if args:
        val = os.environ.get(args[0])
        if val is None:
            return Result(stderr=f"env: {args[0]} not set", exit_code=1)
        return Result(stdout=val)
    safe_keys = ["PATH", "HOME", "USER", "SHELL", "LANG", "TERM", "PWD", "HOSTNAME"]
    lines = []
    for k in safe_keys:
        v = os.environ.get(k)
        if v:
            lines.append(f"{k}={v}")
    return Result(stdout="\n".join(lines))
