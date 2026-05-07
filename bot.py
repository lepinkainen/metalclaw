"""Metalclaw entrypoint.

Wires the CLI REPL, Telegram, and Discord frontends together with the heartbeat
scheduler. Imports ``tools`` to trigger ``@tool`` registrations before any
provider is invoked. Frontend-pinned helpers are re-exported for the test suite.
"""

import argparse
import asyncio
import logging

import discord
from telegram.ext import Application

import heartbeat
from chat_loop import (
    _active_session_messages,
    _chat_with_provider,
    _parse_command,
    _run_tool,
    _split_system,
    build_system_prompt,
    chat,
)
from config import get_config
from frontends import telegram as _telegram
from frontends.cli import console, run_cli_repl as _run_cli_repl
from frontends.discord import (
    _DISCORD_MAX_MESSAGE,
    _DiscordChannel,
    _discord_scope_for,
    _split_for_discord,
    _strip_bot_mention,
    start_discord as _start_discord,
    stop_discord as _stop_discord,
)
from frontends.telegram import (
    start_telegram as _start_telegram,
    stop_telegram as _stop_telegram,
)

# Re-exports retained so existing tests can keep importing these as ``bot._foo``,
# plus ``bot.chat`` / ``bot.build_system_prompt`` consumed by heartbeat.run_tick.
__all__ = [
    "_DISCORD_MAX_MESSAGE",
    "_DiscordChannel",
    "_active_session_messages",
    "_chat_with_provider",
    "_discord_scope_for",
    "_parse_command",
    "_run_tool",
    "_split_for_discord",
    "_split_system",
    "_strip_bot_mention",
    "build_system_prompt",
    "chat",
]


async def _async_main(
    *, daemon: bool, with_telegram: bool, with_discord: bool
) -> None:
    import tools  # noqa: F401 — triggers @tool registrations

    cfg = get_config()
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
            except (KeyboardInterrupt, asyncio.CancelledError):
                pass
        else:
            await _run_cli_repl()
    finally:
        stop.set()
        try:
            await asyncio.wait_for(hb_task, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            hb_task.cancel()
        if tg_app is not None:
            await _stop_telegram(tg_app)
        if discord_client is not None and discord_task is not None:
            await _stop_discord(discord_client, discord_task)


def main() -> None:
    parser = argparse.ArgumentParser(prog="metalclaw")
    parser.add_argument(
        "--daemon", action="store_true",
        help="run without CLI REPL (Telegram + Discord + heartbeat only)",
    )
    parser.add_argument(
        "--no-telegram", action="store_true",
        help="skip starting the Telegram frontend",
    )
    parser.add_argument(
        "--no-discord", action="store_true",
        help="skip starting the Discord frontend",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO if args.daemon else logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    logging.getLogger("telegram.ext").setLevel(logging.WARNING)
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("discord.client").setLevel(logging.WARNING)
    logging.getLogger("discord.gateway").setLevel(logging.WARNING)
    logging.getLogger("discord.http").setLevel(logging.WARNING)

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
