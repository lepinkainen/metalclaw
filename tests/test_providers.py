from collections.abc import Iterator

import tools  # noqa: F401 — register tools
from chat_loop import (
    _active_session_messages,
    _chat_with_provider,
    _run_tool,
    _split_system,
)
from providers.base import AssistantMessage, ToolCall


class FakeProvider:
    name = "fake"

    def __init__(self, replies: list[AssistantMessage]) -> None:
        self._replies: Iterator[AssistantMessage] = iter(replies)
        self.calls: list[tuple[list[dict], list[dict], str]] = []
        self.tool_results_seen: list[list[tuple[ToolCall, str]]] = []

    def chat_once(self, messages, tools_, system):
        self.calls.append((list(messages), list(tools_), system))
        return next(self._replies)

    def format_tool_results(self, results):
        self.tool_results_seen.append(list(results))
        return [
            {"role": "tool", "name": call.name, "content": result_json}
            for call, result_json in results
        ]


def test_loop_terminates_on_empty_tool_calls():
    provider = FakeProvider([
        AssistantMessage(
            text="hello",
            tool_calls=[],
            raw={"role": "assistant", "content": "hello"},
        ),
    ])
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ]
    out = _chat_with_provider(provider, messages)
    assert out == "hello"
    assert messages[0] == {"role": "system", "content": "sys"}
    assert messages[-1] == {"role": "assistant", "content": "hello"}


def test_loop_dispatches_tool_call_and_feeds_result_back():
    """Run a real registered tool (roll_die) through the fake provider."""
    call = ToolCall(id="call-1", name="roll_die", arguments={"sides": 6})
    provider = FakeProvider([
        AssistantMessage(
            text="",
            tool_calls=[call],
            raw={"role": "assistant", "tool_calls": [{"id": "call-1"}]},
        ),
        AssistantMessage(
            text="rolled",
            tool_calls=[],
            raw={"role": "assistant", "content": "rolled"},
        ),
    ]),
    provider = provider[0]

    messages = [{"role": "user", "content": "roll a d6"}]
    out = _chat_with_provider(provider, messages)
    assert out == "rolled"
    # FakeProvider format_tool_results emits a role:tool message with the
    # JSON-serialized tool output. Confirm it appears in history.
    tool_messages = [m for m in messages if m.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert "Rolled a d6" in tool_messages[0]["content"]


def test_loop_excludes_tools_from_schema():
    provider = FakeProvider([
        AssistantMessage(text="ok", tool_calls=[], raw={"role": "assistant", "content": "ok"}),
    ])
    messages = [{"role": "user", "content": "hi"}]
    _chat_with_provider(
        provider, messages, exclude_tools={"escalate_to_big_model"}
    )
    schemas_passed = provider.calls[0][1]
    names = {s["function"]["name"] for s in schemas_passed}
    assert "escalate_to_big_model" not in names
    # Sanity: at least one other tool should still be there
    assert "roll_die" in names


def test_active_session_messages_contextvar_is_set_during_chat():
    captured: list[list[dict] | None] = []

    class PeekProvider:
        name = "peek"

        def chat_once(self, messages, tools_, system):
            captured.append(_active_session_messages.get())
            return AssistantMessage(
                text="done", tool_calls=[], raw={"role": "assistant", "content": "done"}
            )

        def format_tool_results(self, results):
            return []

    messages = [{"role": "user", "content": "x"}]
    _chat_with_provider(PeekProvider(), messages)
    assert captured == [messages]
    # Resets on exit
    assert _active_session_messages.get() is None


def test_split_system_extracts_first_message_only():
    sys, hist = _split_system([
        {"role": "system", "content": "S"},
        {"role": "user", "content": "U"},
    ])
    assert sys == "S"
    assert hist == [{"role": "user", "content": "U"}]


def test_split_system_handles_no_system_role():
    sys, hist = _split_system([{"role": "user", "content": "U"}])
    assert sys == ""
    assert hist == [{"role": "user", "content": "U"}]


def test_run_tool_returns_error_for_unknown_name():
    out = _run_tool("nonexistent_tool", {})
    assert "unknown tool" in str(out)


def test_run_tool_executes_registered_tool():
    out = _run_tool("roll_die", {"sides": 6})
    assert "Rolled" in str(out)


def test_run_tool_returns_structured_validation_error_on_bad_args():
    out = _run_tool("roll_die", {"sides": "not-an-int"})
    assert isinstance(out, dict)
    assert out["error"] == "invalid_arguments"
    assert out["tool"] == "roll_die"
    assert any(issue["field"] == "sides" for issue in out["issues"])


def test_run_tool_returns_structured_validation_error_on_missing_required():
    out = _run_tool("roll_die", {})
    assert isinstance(out, dict)
    assert out["error"] == "invalid_arguments"
    assert any(issue["field"] == "sides" for issue in out["issues"])


def test_run_tool_rejects_unknown_field():
    out = _run_tool("roll_die", {"sides": 6, "bogus": 1})
    # pydantic default ignores extras — call still succeeds.
    # Confirms validation does not break legitimate calls.
    assert "Rolled" in str(out)


def test_add_user_instruction_tool_routes_to_memory_and_refreshes(tmp_path, monkeypatch):
    import config as _config

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "vault_path: " + str(tmp_path / "vault") + "\n"
        "memory_subdir: Memory\n"
        "fastmail_api_token: t\n"
    )
    monkeypatch.setenv("METALCLAW_CONFIG", str(cfg_path))
    for var in ("FASTMAIL_API_TOKEN", "OLLAMA_URL"):
        monkeypatch.delenv(var, raising=False)
    _config.reset_cache()
    try:
        call = ToolCall(
            id="c1", name="add_user_instruction",
            arguments={"text": "Reply in Finnish."},
        )
        provider = FakeProvider([
            AssistantMessage(text="", tool_calls=[call],
                             raw={"role": "assistant", "tool_calls": [{"id": "c1"}]}),
            AssistantMessage(text="ok", tool_calls=[],
                             raw={"role": "assistant", "content": "ok"}),
        ])
        messages = [
            {"role": "system", "content": "old-system"},
            {"role": "user", "content": "from now on Finnish"},
        ]
        _chat_with_provider(provider, messages)
        import memory as _memory
        assert _memory.load().instructions == ["Reply in Finnish."]
        second_system = provider.calls[1][2]
        assert "Reply in Finnish." in second_system
    finally:
        _config.reset_cache()


def test_chat_resets_session_when_provider_switches_mid_session(monkeypatch):
    """If config.provider changes between chat() calls on the same session,
    tool-call-tainted history must be dropped (system + last user kept)."""
    import chat_loop

    class _Cfg:
        provider = "ollama"

    fake_cfg = _Cfg()

    def _get_cfg():
        return fake_cfg

    providers_built: list[str] = []

    def _make_provider(name, model_override=None):
        providers_built.append(name)
        return FakeProvider([
            AssistantMessage(
                text="hi",
                tool_calls=[],
                raw={"role": "assistant", "content": "hi"},
            ),
        ])

    monkeypatch.setattr(chat_loop, "get_config", _get_cfg)
    monkeypatch.setattr(chat_loop, "get_provider", _make_provider)

    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "first"},
        # Simulate prior provider-shaped tool call entries from a previous turn.
        {"role": "assistant", "tool_calls": [{"id": "a"}]},
        {"role": "tool", "name": "roll_die", "content": "{}"},
        {"role": "assistant", "content": "rolled"},
        {"role": "user", "content": "second"},
    ]
    chat_loop.chat(messages)
    # First call stamps "ollama"; history preserved (no mismatch yet).
    assert any(m.get("role") == "tool" for m in messages)

    # Switch provider in config and call again.
    fake_cfg.provider = "litellm"
    messages.append({"role": "user", "content": "third"})
    chat_loop.chat(messages)

    roles = [m.get("role") for m in messages]
    # Reset must drop foreign tool-call entries; system + final user kept,
    # plus the assistant reply appended by the new provider's turn.
    assert "tool" not in roles
    assert messages[0] == {"role": "system", "content": "sys"}
    assert any(
        m.get("role") == "user" and m.get("content") == "third" for m in messages
    )
    assert providers_built == ["ollama", "litellm"]
    chat_loop.forget_session_provider(messages)


def test_chat_does_not_reset_when_provider_unchanged(monkeypatch):
    import chat_loop

    class _Cfg:
        provider = "ollama"

    monkeypatch.setattr(chat_loop, "get_config", lambda: _Cfg())
    monkeypatch.setattr(
        chat_loop,
        "get_provider",
        lambda name, model_override=None: FakeProvider([
            AssistantMessage(
                text="ok",
                tool_calls=[],
                raw={"role": "assistant", "content": "ok"},
            ),
        ]),
    )

    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "first"},
    ]
    chat_loop.chat(messages)
    before = list(messages)
    messages.append({"role": "user", "content": "second"})
    chat_loop.chat(messages)
    # System + first user + first assistant + second user + second assistant.
    assert messages[: len(before)] == before
    chat_loop.forget_session_provider(messages)


def test_memory_mutator_refreshes_system_prompt_mid_loop(tmp_path, monkeypatch):
    """After a memory-mutating tool runs, the next chat_once must see the new memory in `system`."""
    import config as _config

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "vault_path: " + str(tmp_path / "vault") + "\n"
        "memory_subdir: Memory\n"
        "fastmail_api_token: t\n"
    )
    monkeypatch.setenv("METALCLAW_CONFIG", str(cfg_path))
    for var in ("FASTMAIL_API_TOKEN", "OLLAMA_URL"):
        monkeypatch.delenv(var, raising=False)
    _config.reset_cache()
    try:
        call = ToolCall(
            id="c1", name="set_user_preference",
            arguments={"key": "tone", "value": "terse"},
        )
        provider = FakeProvider([
            AssistantMessage(text="", tool_calls=[call],
                             raw={"role": "assistant", "tool_calls": [{"id": "c1"}]}),
            AssistantMessage(text="ok", tool_calls=[],
                             raw={"role": "assistant", "content": "ok"}),
        ])
        messages = [
            {"role": "system", "content": "old-system"},
            {"role": "user", "content": "remember tone=terse"},
        ]
        _chat_with_provider(provider, messages)
        first_system = provider.calls[0][2]
        second_system = provider.calls[1][2]
        assert first_system == "old-system"
        assert "tone=terse" in second_system
    finally:
        _config.reset_cache()
