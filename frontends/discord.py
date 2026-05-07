"""Discord frontend: per-channel sessions, slash commands, message routing."""

import asyncio
import logging
import re
from collections.abc import Iterable
from datetime import datetime

import discord

import channels
from chat_loop import (
    _parse_command,
    build_system_prompt,
    chat,
    forget_session_provider,
    run_turn,
)
from config import get_config
from frontends import common
from registry import TOOLS

_discord_sessions: dict[int, list[dict]] = {}
_discord_session_locks: dict[int, asyncio.Lock] = {}
_known_discord_channels: set[int] = set()


def _session_lock(channel_id: int) -> asyncio.Lock:
    lock = _discord_session_locks.get(channel_id)
    if lock is None:
        lock = asyncio.Lock()
        _discord_session_locks[channel_id] = lock
    return lock

_DISCORD_MAX_MESSAGE = 2000

_DISCORD_HELP_TEXT = "\n".join(common.HELP_LINES)


def _discord_scope_for(channel_id: int) -> str:
    return common.discord_scope(channel_id)


def _strip_bot_mention(text: str, bot_user_id: int) -> str:
    """Remove @mentions of the bot (including <@!id> nickname form) from text."""
    pattern = re.compile(rf"<@!?{bot_user_id}>")
    return pattern.sub("", text).strip()


def _split_for_discord(text: str, limit: int = _DISCORD_MAX_MESSAGE) -> list[str]:
    """Split text into ≤limit-char chunks on paragraph/line/word boundaries.

    If a fenced code block is split across chunks, close it with ``` at the end
    of one chunk and reopen with the same language fence at the start of the next.
    """
    if len(text) <= limit:
        return [text]

    raw_chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n\n", 0, limit)
        if cut <= 0:
            cut = remaining.rfind("\n", 0, limit)
        if cut <= 0:
            cut = remaining.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        raw_chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        raw_chunks.append(remaining)

    fixed: list[str] = []
    open_fence: str | None = None
    for chunk in raw_chunks:
        body = (f"```{open_fence}\n" if open_fence is not None else "") + chunk
        fences = re.findall(r"^```(\w*)\s*$", body, flags=re.MULTILINE)
        if len(fences) % 2 == 1:
            new_fence = fences[-1]
            body = body + "\n```"
            open_fence = new_fence
        else:
            open_fence = None
        if len(body) > limit:
            body = body[:limit]
        fixed.append(body)
    return fixed


async def _discord_send(channel: "discord.abc.Messageable", text: str) -> None:
    """Send text to a Discord channel, splitting if it exceeds the 2000-char limit."""
    if not text:
        return
    for chunk in _split_for_discord(text):
        try:
            await channel.send(chunk)
        except Exception as e:  # noqa: BLE001
            logging.warning("discord send failed: %s", e)
            return


def _send_for(channel: "discord.abc.Messageable") -> common.SendFn:
    async def _send(text: str) -> None:
        await _discord_send(channel, text)
    return _send


class _DiscordChannel:
    name = "discord"

    def __init__(self, client: discord.Client, heartbeat_channel_id: int | None) -> None:
        self._client = client
        self._heartbeat_channel_id = heartbeat_channel_id

    async def notify(self, scope: str, text: str) -> None:
        if self._heartbeat_channel_id is None:
            logging.warning(
                "discord heartbeat: discord_heartbeat_channel unset, dropping reply for scope %s",
                scope,
            )
            return
        ch = self._client.get_channel(self._heartbeat_channel_id)
        if ch is None:
            try:
                ch = await self._client.fetch_channel(self._heartbeat_channel_id)
            except Exception as e:  # noqa: BLE001
                logging.warning(
                    "discord heartbeat: cannot resolve channel %s: %s",
                    self._heartbeat_channel_id,
                    e,
                )
                return
        await _discord_send(ch, text)

    def active_scopes(self) -> Iterable[str]:
        return tuple(common.discord_scope(cid) for cid in _known_discord_channels)


def _get_discord_session(channel_id: int) -> list[dict]:
    if channel_id not in _discord_sessions:
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        _discord_sessions[channel_id] = [
            {"role": "system", "content": build_system_prompt(now)}
        ]
    return _discord_sessions[channel_id]


async def _discord_dispatch_command(
    message: "discord.Message", cmd: str, args: str
) -> None:
    channel_id = message.channel.id
    scope = _discord_scope_for(channel_id)
    send = _send_for(message.channel)
    canon = common.canonicalize(cmd) or cmd

    if canon == "help":
        await _discord_send(message.channel, _DISCORD_HELP_TEXT)
    elif canon == "new":
        old = _discord_sessions.pop(channel_id, None)
        if old is not None:
            forget_session_provider(old)
        await _discord_send(message.channel, "Conversation reset.")
    elif canon == "remember":
        await common.run_remember(send, args)
    elif canon == "forget":
        await common.run_forget(send, args)
    elif canon == "memory":
        await common.run_memory(send)
    elif canon == "manual":
        await common.run_manual(send, args)
    elif canon == "heartbeat":
        await common.run_heartbeat(
            send, scope, args.strip(), warn_no_discord_channel=True
        )
    elif canon == "big":
        await common.run_big(
            send,
            message.channel.typing(),
            _get_discord_session(channel_id),
            args.strip(),
        )
    elif canon == "add-tool":
        await common.run_add_tool(send, args, scope)
    elif canon == "approve":
        await common.run_approve(send, scope)
    elif canon == "approve-force":
        await common.run_approve(send, scope, force=True)
    elif canon == "reject":
        await common.run_reject(send, scope)
    elif canon == "diff":
        await common.run_diff(send, scope)
    elif cmd in common.TOOL_COMMANDS:
        tool_name, parser, formatter = common.TOOL_COMMANDS[cmd]
        try:
            params = parser(args)
        except ValueError as e:
            await _discord_send(message.channel, str(e))
            return
        tool_obj = TOOLS.get(tool_name)
        if tool_obj is None:
            await _discord_send(message.channel, f"{tool_name} tool unavailable")
            return
        loop = asyncio.get_running_loop()
        try:
            async with message.channel.typing():
                result = await loop.run_in_executor(
                    None, lambda: tool_obj.func(**params)
                )
        except Exception as e:  # noqa: BLE001
            await _discord_send(message.channel, f"Error: {e}")
            return
        await _discord_send(message.channel, formatter(result))
    else:
        await _discord_send(message.channel, f"Unknown command: /{cmd}  (try /help)")


def _discord_should_respond(
    message: "discord.Message", bot_user: "discord.ClientUser | None"
) -> bool:
    """Decide whether to handle a non-command message based on channel context."""
    if isinstance(message.channel, discord.DMChannel):
        return True
    cfg = get_config()
    if message.channel.id in cfg.discord_chat_channels:
        return True
    if bot_user is not None and bot_user in message.mentions:
        return True
    ref = message.reference
    if (
        ref is not None
        and isinstance(ref.resolved, discord.Message)
        and bot_user is not None
        and ref.resolved.author.id == bot_user.id
    ):
        return True
    return False


async def _discord_handle_message(
    client: discord.Client, message: "discord.Message"
) -> None:
    if message.author.bot:
        return
    raw = (message.content or "").strip()
    if not raw:
        return

    bot_user = client.user
    text = _strip_bot_mention(raw, bot_user.id) if bot_user is not None else raw
    parsed = _parse_command(text)

    if parsed is None and not _discord_should_respond(message, bot_user):
        return
    if not text:
        return

    channel_id = message.channel.id
    _known_discord_channels.add(channel_id)

    async with _session_lock(channel_id):
        messages = _get_discord_session(channel_id)

        if parsed is not None:
            cmd, args = parsed
            await _discord_dispatch_command(message, cmd, args)
            return

        try:
            async with message.channel.typing():
                _, _, clean_reply = await run_turn(
                    messages, text, lambda: chat(messages)
                )
        except Exception as e:  # noqa: BLE001
            await _discord_send(message.channel, f"Error: {e}")
            return
        await _discord_send(message.channel, clean_reply)


class _MetalclawDiscordClient(discord.Client):
    async def on_ready(self) -> None:
        logging.info("discord ready as %s", self.user)

    async def on_message(self, message: "discord.Message") -> None:
        try:
            await _discord_handle_message(self, message)
        except Exception:
            logging.exception("discord on_message crashed")


async def start_discord(token: str) -> tuple[discord.Client, asyncio.Task]:
    cfg = get_config()
    intents = discord.Intents.default()
    intents.message_content = True
    client = _MetalclawDiscordClient(intents=intents)
    channels.register(_DiscordChannel(client, cfg.discord_heartbeat_channel))
    task = asyncio.create_task(client.start(token))
    return client, task


async def stop_discord(client: discord.Client, task: asyncio.Task) -> None:
    try:
        await client.close()
    except Exception:
        logging.exception("discord client close failed")
    try:
        await asyncio.wait_for(task, timeout=5)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        task.cancel()
