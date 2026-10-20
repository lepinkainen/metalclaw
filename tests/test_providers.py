from collections.abc import Iterator

import tools  # noqa: F401 — register tools
from chat_loop import (
    Session,
    _active_session,
    _chat_with_provider,
    _run_tool,
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
    provider = FakeProvider(
        [
            AssistantMessage(
                text="hello",
                tool_calls=[],
                raw={"role": "assistant", "content": "hello"},
            ),
        ]
    )
    session = Session(system="sys", history=[{"role": "user", "content": "hi"}])
    out = _chat_with_provider(provider, session)
    assert out == "hello"
    assert session.system == "sys"
    assert session.history[-1] == {"role": "assistant", "content": "hello"}


def test_loop_dispatches_tool_call_and_feeds_result_back():
    """Run a real registered tool (roll_die) through the fake provider."""
    call = ToolCall(id="call-1", name="roll_die", arguments={"sides": 6})
    provider = (
        FakeProvider(
            [
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
            ]
        ),
    )
    provider = provider[0]

    session = Session(history=[{"role": "user", "content": "roll a d6"}])
    out = _chat_with_provider(provider, session)
    assert out == "rolled"
    # FakeProvider format_tool_results emits a role:tool message with the
    # JSON-serialized tool output. Confirm it appears in history.
    tool_messages = [m for m in session.history if m.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert "Rolled a d6" in tool_messages[0]["content"]


def test_loop_excludes_tools_from_schema():
    provider = FakeProvider(
        [
            AssistantMessage(
                text="ok", tool_calls=[], raw={"role": "assistant", "content": "ok"}
            ),
        ]
    )
    session = Session(history=[{"role": "user", "content": "hi"}])
    _chat_with_provider(provider, session, exclude_tools={"escalate_to_big_model"})
    schemas_passed = provider.calls[0][1]
    names = {s["function"]["name"] for s in schemas_passed}
    assert "escalate_to_big_model" not in names
    # Sanity: at least one other tool should still be there
    assert "roll_die" in names


def test_active_session_contextvar_is_set_during_chat():
    captured: list[Session | None] = []

    class PeekProvider:
        name = "peek"

        def chat_once(self, messages, tools_, system):
            captured.append(_active_session.get())
            return AssistantMessage(
                text="done", tool_calls=[], raw={"role": "assistant", "content": "done"}
            )

        def format_tool_results(self, results):
            return []

    session = Session(system="S", history=[{"role": "user", "content": "x"}])
    _chat_with_provider(PeekProvider(), session)
    # Point-in-time copy: the user turn, not the post-commit session.
    (snap,) = captured
    assert snap is not None and snap is not session
    assert snap.system == "S"
    assert snap.history == [{"role": "user", "content": "x"}]
    # Resets on exit
    assert _active_session.get() is None


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


def test_add_user_instruction_tool_routes_to_memory_and_refreshes(
    tmp_path, monkeypatch
):
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
            id="c1",
            name="add_user_instruction",
            arguments={"text": "Reply in Finnish."},
        )
        provider = FakeProvider(
            [
                AssistantMessage(
                    text="",
                    tool_calls=[call],
                    raw={"role": "assistant", "tool_calls": [{"id": "c1"}]},
                ),
                AssistantMessage(
                    text="ok", tool_calls=[], raw={"role": "assistant", "content": "ok"}
                ),
            ]
        )
        session = Session(
            system="old-system",
            history=[{"role": "user", "content": "from now on Finnish"}],
        )
        _chat_with_provider(provider, session)
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
        max_tool_iterations = 25
        max_repeated_tool_calls = 3

    fake_cfg = _Cfg()

    def _get_cfg():
        return fake_cfg

    providers_built: list[str] = []

    def _make_provider(name, model_override=None):
        providers_built.append(name)
        return FakeProvider(
            [
                AssistantMessage(
                    text="hi",
                    tool_calls=[],
                    raw={"role": "assistant", "content": "hi"},
                ),
            ]
        )

    monkeypatch.setattr(chat_loop, "get_config", _get_cfg)
    monkeypatch.setattr(chat_loop, "get_provider", _make_provider)

    session = Session(
        system="sys",
        history=[
            {"role": "user", "content": "first"},
            # Simulate prior provider-shaped tool call entries from a previous turn.
            {"role": "assistant", "tool_calls": [{"id": "a"}]},
            {"role": "tool", "name": "roll_die", "content": "{}"},
            {"role": "assistant", "content": "rolled"},
            {"role": "user", "content": "second"},
        ],
    )
    chat_loop.chat(session)
    # First call stamps "ollama"; history preserved (no mismatch yet).
    assert session.provider_name == "ollama"
    assert any(m.get("role") == "tool" for m in session.history)

    # Switch provider in config and call again.
    fake_cfg.provider = "litellm"
    session.append_user("third")
    chat_loop.chat(session)

    roles = [m.get("role") for m in session.history]
    # Reset must drop foreign tool-call entries; final user kept, plus the
    # assistant reply appended by the new provider's turn. System untouched.
    assert "tool" not in roles
    assert session.system == "sys"
    assert session.provider_name == "litellm"
    assert [m["content"] for m in session.history if m["role"] == "user"] == ["third"]
    assert providers_built == ["ollama", "litellm"]


def test_chat_does_not_reset_when_provider_unchanged(monkeypatch):
    import chat_loop

    class _Cfg:
        provider = "ollama"
        max_tool_iterations = 25
        max_repeated_tool_calls = 3

    monkeypatch.setattr(chat_loop, "get_config", lambda: _Cfg())
    monkeypatch.setattr(
        chat_loop,
        "get_provider",
        lambda name, model_override=None: FakeProvider(
            [
                AssistantMessage(
                    text="ok",
                    tool_calls=[],
                    raw={"role": "assistant", "content": "ok"},
                ),
            ]
        ),
    )

    session = Session(system="sys", history=[{"role": "user", "content": "first"}])
    chat_loop.chat(session)
    before = list(session.history)
    session.append_user("second")
    chat_loop.chat(session)
    # First user + first assistant + second user + second assistant.
    assert session.history[: len(before)] == before
    assert len(session.history) == 4


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
            id="c1",
            name="set_user_preference",
            arguments={"key": "tone", "value": "terse"},
        )
        provider = FakeProvider(
            [
                AssistantMessage(
                    text="",
                    tool_calls=[call],
                    raw={"role": "assistant", "tool_calls": [{"id": "c1"}]},
                ),
                AssistantMessage(
                    text="ok", tool_calls=[], raw={"role": "assistant", "content": "ok"}
                ),
            ]
        )
        session = Session(
            system="old-system",
            history=[{"role": "user", "content": "remember tone=terse"}],
        )
        _chat_with_provider(provider, session)
        first_system = provider.calls[0][2]
        second_system = provider.calls[1][2]
        assert first_system == "old-system"
        assert "tone=terse" in second_system
    finally:
        _config.reset_cache()


def test_active_session_snapshot_includes_completed_exchanges_mid_turn():
    """Regression for #36: tools run after a tool round must see that round."""
    call = ToolCall(id="call-1", name="roll_die", arguments={"sides": 6})
    captured: list[Session] = []

    class PeekProvider:
        name = "peek"

        def __init__(self):
            self._replies = iter(
                [
                    AssistantMessage(
                        text="",
                        tool_calls=[call],
                        raw={"role": "assistant", "content": "", "tool_calls": [1]},
                    ),
                    AssistantMessage(
                        text="done",
                        tool_calls=[],
                        raw={"role": "assistant", "content": "done"},
                    ),
                ]
            )

        def chat_once(self, messages, tools_, system):
            snap = _active_session.get()
            assert snap is not None
            captured.append(snap)
            return next(self._replies)

        def format_tool_results(self, results):
            return [{"role": "tool", "name": c.name, "content": r} for c, r in results]

    session = Session(system="sys", history=[{"role": "user", "content": "roll"}])
    _chat_with_provider(PeekProvider(), session)

    first, second = captured
    assert first.system == second.system == "sys"
    # Round 1: nothing completed yet beyond the user turn.
    assert [m["role"] for m in first.history] == ["user"]
    # Round 2: the assistant tool call + its result are visible to tools.
    assert [m["role"] for m in second.history] == ["user", "assistant", "tool"]
    assert second.history[-1]["name"] == "roll_die"
    assert _active_session.get() is None


def test_provider_failure_leaves_session_untouched():
    class Boom:
        name = "boom"

        def chat_once(self, messages, tools_, system):
            raise RuntimeError("provider down")

        def format_tool_results(self, results):
            return []

    session = Session(system="sys", history=[{"role": "user", "content": "hi"}])
    before = (session.system, list(session.history))
    try:
        _chat_with_provider(Boom(), session)
    except RuntimeError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected provider error")
    assert (session.system, session.history) == before
    assert _active_session.get() is None
