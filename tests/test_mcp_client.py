import asyncio
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from mcp import types
from mcp.server import MCPServer

import config
import mcp_client
import registry
from chat_loop import Session, _chat_with_provider
from config import McpServer
from providers.base import AssistantMessage, ToolCall

srv = MCPServer("fake")


@srv.tool()
def add(a: int, b: int) -> int:
    """Add two ints."""
    return a + b


@srv.tool()
def boom() -> str:
    raise RuntimeError("kaboom")


@srv.tool()
async def slow() -> str:
    await asyncio.sleep(5)
    return "late"


@srv.tool()
def big() -> str:
    return "x" * 50_000


@pytest.fixture
def mcp(monkeypatch):
    monkeypatch.setattr(mcp_client, "_transport", lambda s, stack: srv)

    def _start(*servers):
        mcp_client.start(servers)

    yield _start
    mcp_client.stop()


def _srv(**kw) -> McpServer:
    return McpServer(**{"name": "t", "transport": "http", "url": "http://x/mcp"} | kw)


def test_register_and_call(mcp):
    mcp(_srv(include=("add", "boom", "slow", "big", "nope"), call_timeout_seconds=0.3))
    assert mcp_client.status()[0]["status"] == "up"
    assert {"t_add", "t_boom", "t_slow", "t_big"} <= set(registry.TOOLS)
    fn = registry.TOOLS["t_add"]
    assert fn.schema["function"]["description"] == "[t] Add two ints."
    assert fn.func(a=1, b=2) == {"source": "mcp:t", "untrusted": True, "result": 3}
    assert registry.TOOLS["t_boom"].func().startswith("Error:")
    t0 = time.monotonic()
    assert "timed out" in registry.TOOLS["t_slow"].func()
    assert time.monotonic() - t0 < 1.5
    res = registry.TOOLS["t_big"].func()["result"]
    assert res.endswith("…[truncated 30000 chars]") and len(res) < 20_100
    with ThreadPoolExecutor(4) as ex:
        outs = list(ex.map(lambda i: fn.func(a=i, b=1)["result"], range(8)))
    assert outs == [i + 1 for i in range(8)]
    assert "mcp:*" in mcp_client.system_prompt_note()
    mcp_client.stop()
    assert "t_add" not in registry.TOOLS
    assert mcp_client.system_prompt_note() == ""


def test_exclude_and_native_collision(mcp):
    native = registry.TOOLS["weather"].func
    mcp(_srv(name="w", exclude=("boom",)))
    names = mcp_client.status()[0]["tools"]
    assert "w_boom" not in names and "w_add" in names
    registry.TOOLS["w_x"] = registry.Tool(func=native, schema={})
    try:
        st = mcp_client._STATE["w"]
        mcp_client._register(
            st, [types.Tool(name="x", input_schema={"type": "object"})]
        )
        assert registry.TOOLS["w_x"].func is native
    finally:
        registry.TOOLS.pop("w_x")


def test_failed_server_isolated(mcp, monkeypatch):
    def transport(s, stack):
        if s.name == "bad":
            raise RuntimeError("no route")
        return srv

    monkeypatch.setattr(mcp_client, "_transport", transport)
    mcp(
        _srv(name="bad"),
        _srv(name="good", include=("add",)),
        _srv(name="off", enabled=False),
    )
    st = {m["name"]: m for m in mcp_client.status()}
    assert st["bad"]["status"] == "down" and "no route" in st["bad"]["error"]
    assert st["good"]["status"] == "up" and st["off"]["status"] == "disabled"
    assert not any(k.startswith("bad_") for k in registry.TOOLS)


def test_missing_env_var_marks_server_down(monkeypatch):
    monkeypatch.delenv("NOPE_X", raising=False)
    try:
        mcp_client.start([_srv(headers={"A": "Bearer ${NOPE_X}"})])  # real _transport
        st = mcp_client.status()[0]
        assert st["status"] == "down" and "NOPE_X" in st["error"]
    finally:
        mcp_client.stop()


def test_startup_timeout(mcp, monkeypatch):
    class Hang:
        async def __aenter__(self):
            await asyncio.sleep(60)

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr("mcp.Client", lambda target: Hang())
    t0 = time.monotonic()
    mcp(_srv(startup_timeout_seconds=0.3))
    assert time.monotonic() - t0 < 1.5
    assert mcp_client.status()[0]["status"] == "down"


def test_call_from_running_loop(mcp):
    mcp(_srv(include=("add",)))

    async def inner():
        return registry.TOOLS["t_add"].func(a=2, b=2)  # blocks loop thread, no deadlock

    assert asyncio.run(inner())["result"] == 4


def test_chat_loop_end_to_end(mcp):
    mcp(_srv(include=("add",)))

    class Stub:
        name = "stub"
        replies = iter(
            [
                AssistantMessage(
                    text="",
                    tool_calls=[
                        ToolCall(id="1", name="t_add", arguments={"a": 3, "b": 4})
                    ],
                ),
                AssistantMessage(text="7"),
            ]
        )

        def chat_once(self, messages, tools, system):
            assert any(t["function"]["name"] == "t_add" for t in tools)
            return next(self.replies)

        def format_tool_results(self, results):
            return [
                {"role": "tool", "tool_call_id": c.id, "content": j} for c, j in results
            ]

    session = Session(history=[{"role": "user", "content": "3+4"}])
    assert _chat_with_provider(Stub(), session) == "7"
    assert '"result": 7' in session.history[-1]["content"]


def test_schema_mapping():
    s = _srv(name="gh")
    t = types.Tool(
        name="a.b" + "c" * 80,
        description="  d  ",
        input_schema={"$schema": "x", "title": "T", "type": "object"},
    )
    fn = mcp_client._to_schema(s, t)["function"]
    assert fn["name"].startswith("gh_a_b") and len(fn["name"]) == 64
    assert fn["description"] == "[gh] d"
    assert fn["parameters"] == {"type": "object", "properties": {}}


def test_convert_result():
    s = _srv()
    r = types.CallToolResult(
        content=[
            types.TextContent(type="text", text="hi"),
            types.ImageContent(type="image", data="AAAA", mime_type="image/png"),
        ]
    )
    assert (
        mcp_client._convert_result(s, r)["result"]
        == "hi\n[image image/png, 4 bytes omitted]"
    )
    r = types.CallToolResult(content=[], structured_content={"result": "plain"})
    assert mcp_client._convert_result(s, r)["result"] == "plain"
    r = types.CallToolResult(content=[], structured_content={"k": "v" * 30_000})
    assert mcp_client._convert_result(s, r)["result"].startswith('{"k": "vvv')


def test_config_validation(cfg_file, write_config):
    bad = [
        ({"name": "gh", "transport": "http"}, "needs url"),
        ({"name": "gh", "transport": "stdio", "url": "u"}, "needs command"),
        (
            {
                "name": "gh",
                "transport": "http",
                "url": "u",
                "include": ["a"],
                "exclude": ["b"],
            },
            "mutually",
        ),
        ({"name": "gh", "transport": "http", "url": "u", "inclde": ["a"]}, "inclde"),
        ({"name": "Bad-Name", "transport": "http", "url": "u"}, "must match"),
    ]
    for entry, msg in bad:
        write_config(cfg_file, mcp_servers=[entry])
        config.reset_cache()
        with pytest.raises(ValueError, match=msg):
            config.get_config()
    write_config(
        cfg_file,
        mcp_servers=[{"name": "a", "transport": "stdio", "command": "x"}] * 2,
    )
    config.reset_cache()
    with pytest.raises(ValueError, match="duplicate"):
        config.get_config()
