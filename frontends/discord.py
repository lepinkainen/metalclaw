"""Discord frontend: per-channel sessions, slash commands, message routing."""

import asyncio
import logging
import re
from collections.abc import Iterable
from typing import cast

import discord

import channels
import history
from chat_loop import (
    Session,
    _parse_command,
    chat,
    run_turn,
    scoped_chat,
)
from config import get_config
from frontends import common
from registry import TOOLS

log = logging.getLogger(__name__)

_discord_session_locks: dict[int, asyncio.Lock] = {}
# In-memory mirror of the ``known_chats`` rows for frontend="discord"
# (history.db). Loaded in ``start_discord``; every handled message touches the row.
_known_discord_channels: set[int] = set()
_FRONTEND = "discord"


def _load_known_channels() -> set[int]:
    out: set[int] = set()
    for cid in history.known_chat_ids(_FRONTEND):
        try:
            out.add(int(cid))
        except ValueError:
            log.warning("discord: ignoring non-integer known channel id %r", cid)
    return out


def _channel_meta(message: discord.Message) -> dict:
    ch = message.channel
    guild = getattr(message, "guild", None)
    return {
        "type": type(ch).__name__,
        "name": getattr(ch, "name", None),
        "guild": getattr(guild, "name", None),
        "guild_id": getattr(guild, "id", None),
        "author": str(message.author) if message.author else None,
    }


def _remember_channel(channel_id: int, meta: dict | None = None) -> None:
    try:
        history.known_chat_touch(_FRONTEND, str(channel_id), meta)
    except Exception:
        log.exception("discord: could not persist known channel %d", channel_id)
    if channel_id not in _known_discord_channels:
        _known_discord_channels.add(channel_id)
        log.info(
            "discord: new channel remembered channel_id=%d (known=%d)",
            channel_id,
            len(_known_discord_channels),
        )


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
    """Split text to fit Discord's message limit (see `common.split_message`)."""
    return common.split_message(text, limit)


async def _discord_send(
    channel: discord.abc.Messageable, text: str, *, raise_on_error: bool = False
) -> None:
    """Send text to a Discord channel, splitting if it exceeds the 2000-char limit.

    Send errors are logged and swallowed for interactive replies; pass
    ``raise_on_error=True`` (heartbeat notify) so the caller can keep the
    action active instead of counting a dropped message as delivered.
    """
    if not text:
        return
    chunks = _split_for_discord(text)
    log.info("discord: sending %d chars in %d message(s)", len(text), len(chunks))
    for chunk in chunks:
        try:
            await channel.send(chunk)
        except Exception as e:
            if raise_on_error:
                raise
            log.warning("discord send failed: %s", e, exc_info=True)
            return


def _send_for(channel: discord.abc.Messageable) -> common.SendFn:
    async def _send(text: str) -> None:
        await _discord_send(channel, text)

    return _send


class _DiscordChannel:
    name = "discord"

    def __init__(
        self, client: discord.Client, heartbeat_channel_id: int | None
    ) -> None:
        self._client = client
        self._heartbeat_channel_id = heartbeat_channel_id

    async def notify(self, scope: str, text: str) -> None:
        """Deliver a heartbeat alert; raises on any failure so `run_tick`
        keeps the action active (never silently drops a reminder)."""
        if self._heartbeat_channel_id is None:
            raise RuntimeError(
                f"discord_heartbeat_channel unset; cannot deliver to scope {scope}"
            )
        ch = self._client.get_channel(self._heartbeat_channel_id)
        if ch is None:
            try:
                ch = await self._client.fetch_channel(self._heartbeat_channel_id)
            except Exception as e:
                raise RuntimeError(
                    f"cannot resolve discord channel {self._heartbeat_channel_id}: {e}"
                ) from e
        log.info(
            "discord: notify channel=%s %d chars (scope %s)",
            self._heartbeat_channel_id,
            len(text),
            scope,
        )
        # ids come from the configured heartbeat channel, which is a text channel
        await _discord_send(
            cast(discord.abc.Messageable, ch), text, raise_on_error=True
        )

    def active_scopes(self) -> Iterable[str]:
        return tuple(common.discord_scope(cid) for cid in _known_discord_channels)


def _get_discord_session(channel_id: int) -> Session:
    """Per-channel conversation, rebuilt from the ``turns`` table on every turn."""
    return Session.load(_discord_scope_for(channel_id))


# Per-channel live tool-status toggle; absent -> config.show_tool_calls.
_discord_show_tools: dict[int, bool] = {}


def _tool_status_for(channel) -> common.ToolStatusReporter | None:
    if not _discord_show_tools.get(channel.id, get_config().show_tool_calls):
        return None

    async def _send(text: str):
        return await channel.send(text)

    async def _edit(msg, text: str):
        await msg.edit(content=text)

    return common.ToolStatusReporter(
        asyncio.get_running_loop(), _send, _edit, common.DISCORD_STATUS_LIMIT
    )


async def _discord_dispatch_command(
    message: discord.Message, cmd: str, args: str
) -> None:
    channel_id = message.channel.id
    scope = _discord_scope_for(channel_id)
    send = _send_for(message.channel)
    canon = common.canonicalize(cmd) or cmd
    log.info(
        "discord: command /%s (canon=%s) channel_id=%d args=%d chars",
        cmd,
        canon,
        channel_id,
        len(args),
    )

    if canon == "help":
        await _discord_send(message.channel, _DISCORD_HELP_TEXT)
    elif canon == "tools":
        cur = _discord_show_tools.get(channel_id, get_config().show_tool_calls)
        new, reply = common.parse_tools_arg(cur, args)
        _discord_show_tools[channel_id] = new
        await _discord_send(message.channel, reply)
    elif canon == "status":
        await common.run_status(send)
    elif canon == "usage":
        await common.run_usage(send)
    elif canon == "new":
        await asyncio.to_thread(Session.reset, _discord_scope_for(channel_id))
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
            await asyncio.to_thread(_get_discord_session, channel_id),
            args.strip(),
            scope=scope,
            on_tool_call=_tool_status_for(message.channel),
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
            log.info("discord: /%s bad args: %s", cmd, e)
            await _discord_send(message.channel, str(e))
            return
        log.info(
            "discord: tool command /%s -> %s params=%.200s", cmd, tool_name, params
        )
        tool_obj = TOOLS.get(tool_name)
        if tool_obj is None:
            log.warning("discord: tool %s unavailable", tool_name)
            await _discord_send(message.channel, f"{tool_name} tool unavailable")
            return
        loop = asyncio.get_running_loop()
        try:
            async with message.channel.typing():
                result = await loop.run_in_executor(
                    None, lambda: tool_obj.func(**params)
                )
        except Exception as e:
            log.exception("discord: tool command /%s failed", cmd)
            await _discord_send(message.channel, f"Error: {e}")
            return
        await _discord_send(message.channel, formatter(result))
    else:
        log.info("discord: unknown command /%s", cmd)
        await _discord_send(message.channel, f"Unknown command: /{cmd}  (try /help)")


def _discord_should_respond(
    message: discord.Message, bot_user: discord.ClientUser | None
) -> bool:
    """Decide whether to handle a non-command message based on channel context."""
    cid = message.channel.id
    if isinstance(message.channel, discord.DMChannel):
        log.debug("discord: respond=yes channel=%d reason=dm", cid)
        return True
    cfg = get_config()
    if message.channel.id in cfg.discord_chat_channels:
        log.debug("discord: respond=yes channel=%d reason=chat_channel", cid)
        return True
    if bot_user is not None and bot_user in message.mentions:
        log.debug("discord: respond=yes channel=%d reason=mention", cid)
        return True
    ref = message.reference
    if (
        ref is not None
        and isinstance(ref.resolved, discord.Message)
        and bot_user is not None
        and ref.resolved.author.id == bot_user.id
    ):
        log.debug("discord: respond=yes channel=%d reason=reply_to_bot", cid)
        return True
    log.debug(
        "discord: respond=no channel=%d reason=guild channel without mention/reply", cid
    )
    return False


async def _discord_handle_message(
    client: discord.Client, message: discord.Message
) -> None:
    if message.author.bot:
        return
    raw = (message.content or "").strip()
    if not raw:
        return

    bot_user = client.user
    text = _strip_bot_mention(raw, bot_user.id) if bot_user is not None else raw
    parsed = _parse_command(text)
    log.info(
        "discord: message channel_id=%d user=%s len=%d command=%s",
        message.channel.id,
        message.author,
        len(text),
        parsed[0] if parsed else None,
    )
    log.debug("discord: message text: %.500s", text)

    if parsed is None and not _discord_should_respond(message, bot_user):
        return
    if not text:
        return

    channel_id = message.channel.id
    _remember_channel(channel_id, _channel_meta(message))

    if parsed is not None:
        cmd, args = parsed
        async with _session_lock(channel_id):
            await _discord_dispatch_command(message, cmd, args)
        return

    delay = get_config().message_debounce_seconds
    if delay > 0:
        _debouncer.add(channel_id, text, message, delay)
        return
    await _run_text_turn(channel_id, text, message)


async def _run_text_turn(channel_id: int, text: str, message: discord.Message) -> None:
    """One chat turn for ``text``; replies in the channel of the latest message."""
    async with _session_lock(channel_id):
        scope = _discord_scope_for(channel_id)
        messages = await asyncio.to_thread(Session.load, scope)
        on_tool = _tool_status_for(message.channel)
        try:
            async with message.channel.typing():
                _, _, clean_reply = await run_turn(
                    messages,
                    text,
                    lambda: scoped_chat(
                        scope, lambda: chat(messages, on_tool_call=on_tool)
                    ),
                )
        except Exception as e:
            log.exception("discord: turn failed channel_id=%d", channel_id)
            await _discord_send(message.channel, f"Error: {e}")
            return
        if on_tool is not None:
            await on_tool.drain()
        log.info("discord: reply channel_id=%d len=%d", channel_id, len(clean_reply))
        await _discord_send(message.channel, clean_reply)


_debouncer = common.Debouncer(_run_text_turn)


class _MetalclawDiscordClient(discord.Client):
    async def on_ready(self) -> None:
        log.info("discord ready as %s", self.user)

    async def on_message(self, message: discord.Message) -> None:
        try:
            await _discord_handle_message(self, message)
        except Exception:
            log.exception("discord on_message crashed")


async def start_discord(token: str) -> tuple[discord.Client, asyncio.Task]:
    cfg = get_config()
    intents = discord.Intents.default()
    intents.message_content = True
    _known_discord_channels.update(_load_known_channels())
    client = _MetalclawDiscordClient(intents=intents)
    channels.register(_DiscordChannel(client, cfg.discord_heartbeat_channel))
    task = asyncio.create_task(client.start(token))
    log.info(
        "discord: starting gateway (chat_channels=%d heartbeat_channel=%s)",
        len(cfg.discord_chat_channels),
        cfg.discord_heartbeat_channel,
    )
    return client, task


async def stop_discord(client: discord.Client, task: asyncio.Task) -> None:
    try:
        await client.close()
    except Exception:
        log.exception("discord client close failed")
    try:
        await asyncio.wait_for(task, timeout=5)
    except TimeoutError, asyncio.CancelledError:
        task.cancel()
