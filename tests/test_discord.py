import asyncio

import bot
import channels as channels_mod


# --- scope helper ---


def test_discord_scope_dm():
    assert bot._discord_scope_for(123456789) == "discord-123456789"


def test_discord_scope_guild_channel():
    assert bot._discord_scope_for(987654321) == "discord-987654321"


# --- mention strip ---


def test_strip_bot_mention_plain():
    assert bot._strip_bot_mention("<@42> hello there", 42) == "hello there"


def test_strip_bot_mention_nickname_form():
    assert bot._strip_bot_mention("<@!42> hello there", 42) == "hello there"


def test_strip_bot_mention_in_middle():
    assert bot._strip_bot_mention("hi <@42> there", 42) == "hi  there"


def test_strip_bot_mention_other_user_left_alone():
    assert bot._strip_bot_mention("<@99> ping", 42) == "<@99> ping"


def test_strip_bot_mention_no_mention():
    assert bot._strip_bot_mention("just talking", 42) == "just talking"


# --- chunker ---


def test_split_short_returns_single_chunk():
    assert bot._split_for_discord("hello world") == ["hello world"]


def test_split_at_limit_returns_single_chunk():
    text = "a" * bot._DISCORD_MAX_MESSAGE
    assert bot._split_for_discord(text) == [text]


def test_split_long_paragraphs_chunks_under_limit():
    para = "x" * 500
    text = "\n\n".join([para] * 10)
    chunks = bot._split_for_discord(text)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= bot._DISCORD_MAX_MESSAGE


def test_split_prefers_paragraph_breaks():
    text = ("a" * 1500) + "\n\n" + ("b" * 1500)
    chunks = bot._split_for_discord(text)
    assert chunks[0].rstrip().endswith("a")
    assert chunks[1].lstrip().startswith("b")


def test_split_reopens_fenced_code_block():
    code = "```python\n" + ("print('x')\n" * 300) + "```"
    chunks = bot._split_for_discord(code)
    assert len(chunks) >= 2
    for chunk in chunks:
        fences = chunk.count("```")
        assert fences % 2 == 0, f"chunk has unbalanced fences: {chunk[:80]}…"
    assert chunks[1].lstrip().startswith("```")


def test_split_handles_no_breakable_chars():
    text = "x" * (bot._DISCORD_MAX_MESSAGE * 2 + 5)
    chunks = bot._split_for_discord(text)
    for chunk in chunks:
        assert len(chunk) <= bot._DISCORD_MAX_MESSAGE


# --- DiscordChannel.notify guards ---


class _FakeMsgChannel:
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


class _FakeClient:
    def __init__(self, channel=None):
        self._channel = channel
        self.fetched_id: int | None = None

    def get_channel(self, channel_id: int):
        return self._channel

    async def fetch_channel(self, channel_id: int):
        self.fetched_id = channel_id
        if self._channel is None:
            raise RuntimeError("not found")
        return self._channel


def test_discord_channel_drops_when_unconfigured():
    fake_client = _FakeClient()
    channel = bot._DiscordChannel(fake_client, heartbeat_channel_id=None)
    asyncio.run(channel.notify("discord-1", "ping"))
    assert fake_client.fetched_id is None


def test_discord_channel_sends_to_configured_channel():
    msg_channel = _FakeMsgChannel()
    fake_client = _FakeClient(channel=msg_channel)
    channel = bot._DiscordChannel(fake_client, heartbeat_channel_id=4242)
    asyncio.run(channel.notify("discord-anything", "alert"))
    assert msg_channel.sent == ["alert"]


def test_discord_channel_falls_back_to_fetch():
    msg_channel = _FakeMsgChannel()

    class _ClientWithFetch(_FakeClient):
        def get_channel(self, channel_id):
            return None

        async def fetch_channel(self, channel_id):
            self.fetched_id = channel_id
            return msg_channel

    fake_client = _ClientWithFetch(channel=msg_channel)
    channel = bot._DiscordChannel(fake_client, heartbeat_channel_id=4242)
    asyncio.run(channel.notify("discord-anything", "alert"))
    assert fake_client.fetched_id == 4242
    assert msg_channel.sent == ["alert"]


# --- channels.for_scope routing ---


def test_for_scope_routes_discord_prefix():
    class _Stub:
        name = "discord"

        async def notify(self, scope, text):
            pass

        def active_scopes(self):
            return ()

    channels_mod.register(_Stub())
    try:
        assert channels_mod.for_scope("discord-123") is not None
        assert channels_mod.for_scope("discord-abc") is not None
    finally:
        channels_mod.CHANNELS.clear()
