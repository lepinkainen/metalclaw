"""Metalclaw entrypoint.

Wires the CLI REPL, Telegram, and Discord frontends together with the heartbeat
scheduler. Imports ``tools`` to trigger ``@tool`` registrations before any
provider is invoked.
"""

import argparse
import asyncio
import logging
import logging.handlers
import os
from pathlib import Path

import discord
from telegram.ext import Application

import heartbeat
import memory
from config import get_config
from frontends import telegram as _telegram
from frontends.cli import console
from frontends.cli import run_cli_repl as _run_cli_repl
from frontends.discord import (
    start_discord as _start_discord,
)
from frontends.discord import (
    stop_discord as _stop_discord,
)
from frontends.telegram import (
    start_telegram as _start_telegram,
)
from frontends.telegram import (
    stop_telegram as _stop_telegram,
)

log = logging.getLogger(__name__)

_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


class _NoTraceback(logging.Formatter):
    """Console formatter: one-line errors; full tracebacks go to the log file only."""

    def formatException(self, ei) -> str:
        return ""


def _log_file_path() -> Path:
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state) / "metalclaw" / "metalclaw.log"


def _setup_logging(args: argparse.Namespace) -> Path:
    """DEBUG rotating file log (always) + console handler (INFO daemon / WARNING REPL).

    Console level can be overridden with METALCLAW_LOG_LEVEL. Returns the log path.
    """
    path = _log_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter(_LOG_FORMAT)
    file_h = logging.handlers.RotatingFileHandler(
        path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_h.setLevel(logging.DEBUG)
    file_h.setFormatter(fmt)
    console_level = os.environ.get("METALCLAW_LOG_LEVEL", "").upper() or (
        "INFO" if args.daemon else "WARNING"
    )
    console_h = logging.StreamHandler()
    console_h.setLevel(getattr(logging, console_level, logging.WARNING))
    console_h.setFormatter(_NoTraceback(_LOG_FORMAT))
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(logging.DEBUG)
    root.addHandler(file_h)
    root.addHandler(console_h)
    for name in (
        "httpx",
        "httpcore",
        "telegram",
        "discord",
        "litellm",
        "LiteLLM",
        "asyncio",
        "markdown_it",
    ):
        logging.getLogger(name).setLevel(logging.WARNING)
    return path


def _log_startup(cfg, args: argparse.Namespace, log_path: Path) -> None:
    from config import _config_path

    log.info(
        "startup: pid=%d daemon=%s log_file=%s", os.getpid(), args.daemon, log_path
    )
    log.info("startup: config=%s", _config_path())
    log.info(
        "startup: provider=%s model=%s",
        cfg.provider,
        cfg.model if cfg.provider == "ollama" else cfg.litellm_model,
    )
    log.info(
        "startup: escalation enabled=%s provider=%s model=%s",
        cfg.escalation_enabled,
        cfg.escalation_provider,
        cfg.escalation_model,
    )
    log.info(
        "startup: heartbeat enabled=%s interval=%s active_hours=%s",
        cfg.heartbeat_enabled,
        cfg.heartbeat_interval_seconds,
        cfg.heartbeat_active_hours,
    )
    log.info(
        "startup: frontends telegram=%s discord=%s cli=%s",
        (not args.no_telegram) and bool(cfg.telegram_bot_token),
        (not args.no_discord) and bool(cfg.discord_bot_token),
        not args.daemon,
    )


async def _async_main(*, daemon: bool, with_telegram: bool, with_discord: bool) -> None:
    import tools  # noqa: F401 — triggers @tool registrations

    cfg = get_config()
    migrated = memory.migrate_legacy_scopes()
    if migrated:
        log.info(
            "memory: migrated %d legacy scope file(s) into memory.md (%s); originals renamed to *.bak",
            len(migrated),
            ", ".join(migrated),
        )
    tg_app: Application | None = None
    if with_telegram:
        if not cfg.telegram_bot_token:
            if daemon and not (with_discord and cfg.discord_bot_token):
                raise RuntimeError(
                    "telegram_bot_token missing — set TELEGRAM_BOT_TOKEN env or "
                    "telegram_bot_token in config.yaml"
                )
            console.print("[dim]no telegram_bot_token — telegram disabled[/dim]")
        else:
            tg_app = await _start_telegram(cfg.telegram_bot_token)
            console.print(
                f"[dim]telegram polling started ({_telegram.known_chat_count()} known chat(s))[/dim]"
            )

    discord_client: discord.Client | None = None
    discord_task: asyncio.Task | None = None
    if with_discord:
        if not cfg.discord_bot_token:
            if daemon and tg_app is None:
                raise RuntimeError(
                    "no frontend token configured — set telegram_bot_token or "
                    "discord_bot_token (or pass --no-daemon to use the CLI)"
                )
            console.print("[dim]no discord_bot_token — discord disabled[/dim]")
        else:
            discord_client, discord_task = await _start_discord(cfg.discord_bot_token)
            console.print("[dim]discord gateway connecting…[/dim]")

    stop = asyncio.Event()
    hb_task = asyncio.create_task(heartbeat.run(stop))

    try:
        if daemon:
            console.print("[dim]daemon mode — Ctrl-C to exit[/dim]")
            try:
                await asyncio.Event().wait()
            except KeyboardInterrupt, asyncio.CancelledError:
                pass
        else:
            await _run_cli_repl()
    finally:
        stop.set()
        try:
            await asyncio.wait_for(hb_task, timeout=5)
        except TimeoutError, asyncio.CancelledError:
            hb_task.cancel()
        if tg_app is not None:
            await _stop_telegram(tg_app)
        if discord_client is not None and discord_task is not None:
            await _stop_discord(discord_client, discord_task)


def main() -> None:
    parser = argparse.ArgumentParser(prog="metalclaw")
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="run without CLI REPL (Telegram + Discord + heartbeat only)",
    )
    parser.add_argument(
        "--no-telegram",
        action="store_true",
        help="skip starting the Telegram frontend",
    )
    parser.add_argument(
        "--no-discord",
        action="store_true",
        help="skip starting the Discord frontend",
    )
    args = parser.parse_args()

    log_path = _setup_logging(args)

    try:
        _log_startup(get_config(), args, log_path)
    except Exception:
        log.exception("startup summary failed")

    try:
        asyncio.run(
            _async_main(
                daemon=args.daemon,
                with_telegram=not args.no_telegram,
                with_discord=not args.no_discord,
            )
        )
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
