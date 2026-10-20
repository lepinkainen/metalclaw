from collections.abc import Iterator

import tools  # noqa: F401 — register tools
from chat_loop import (
    Session,
    _active_session,
    _chat_with_provider,
    _run_tool,
)
from messages import ToolResultMessage, UserMessage
from providers.base import AssistantMessage, ToolCall


class FakeProvider:
    name = "fake"

    def __init__(self, replies: list[AssistantMessage]) -> None:
        self._replies: Iterator[AssistantMessage] = iter(replies)
        self.calls: list[tuple[list, list[dict], str]] = []

    def chat_once(self, history, tools_, system):
        self.calls.append((list(history), list(tools_), system))
        return next(self._replies)


def test_loop_terminates_on_empty_tool_calls():
    provider = FakeProvider(
        [
            AssistantMessage(
                text="hello",
                tool_calls=[],
            ),
        ]
    )
    session = Session(system="sys", history=[UserMessage("hi")])
    out = _chat_with_provider(provider, session)
    assert out == "hello"
    assert session.system == "sys"
    assert session.history[-1] == AssistantMessage(text="hello")


def test_loop_dispatches_tool_call_and_feeds_result_back():
    """Run a real registered tool (roll_die) through the fake provider."""
    call = ToolCall(id="call-1", name="roll_die", arguments={"sides": 6})
    provider = (
        FakeProvider(
            [
                AssistantMessage(
                    text="",
                    tool_calls=[call],
                ),
                AssistantMessage(
                    text="rolled",
                    tool_calls=[],
                ),
            ]
        ),
    )
    provider = provider[0]

    session = Session(history=[UserMessage("roll a d6")])
    out = _chat_with_provider(provider, session)
    assert out == "rolled"
    # The loop records the JSON-serialized tool output as a ToolResultMessage
    # paired with the call id.
    tool_messages = [m for m in session.history if isinstance(m, ToolResultMessage)]
    assert len(tool_messages) == 1
    assert tool_messages[0].call_id == "call-1"
    assert "Rolled a d6" in tool_messages[0].content


def test_loop_excludes_tools_from_schema():
    provider = FakeProvider(
        [
            AssistantMessage(text="ok", tool_calls=[]),
        ]
    )
    session = Session(history=[UserMessage("hi")])
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
            return AssistantMessage(text="done")

    session = Session(system="S", history=[UserMessage("x")])
    _chat_with_provider(PeekProvider(), session)
    # Point-in-time copy: the user turn, not the post-commit session.
    (snap,) = captured
    assert snap is not None and snap is not session
    assert snap.system == "S"
    assert snap.history == [UserMessage("x")]
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
                ),
                AssistantMessage(text="ok", tool_calls=[]),
            ]
        )
        session = Session(
            system="old-system", history=[UserMessage("from now on Finnish")]
        )
        _chat_with_provider(provider, session)
        import memory as _memory

        assert _memory.load().instructions == ["Reply in Finnish."]
        second_system = provider.calls[1][2]
        assert "Reply in Finnish." in second_system
    finally:
        _config.reset_cache()


def test_chat_keeps_history_when_provider_switches_both_ways(monkeypatch):
    """Canonical history survives ollama -> litellm -> ollama after a tool
    round: no reset, every provider receives the same typed messages."""
    import chat_loop

    class _Cfg:
        provider = "ollama"
        max_tool_iterations = 25
        max_repeated_tool_calls = 3

    fake_cfg = _Cfg()
    providers_built: list[str] = []
    seen: dict[str, list] = {}

    def _make_provider(name, model_override=None):
        providers_built.append(name)
        p = FakeProvider([AssistantMessage(text=f"hi from {name}")])
        seen[name] = p.calls
        return p

    monkeypatch.setattr(chat_loop, "get_config", lambda: fake_cfg)
    monkeypatch.setattr(chat_loop, "get_provider", _make_provider)

    call = ToolCall(id="a", name="roll_die", arguments={"sides": 6})
    session = Session(system="sys")
    session.append_user("first")
    session.append_exchange(
        AssistantMessage(text="", tool_calls=[call]),
        [ToolResultMessage(call_id="a", name="roll_die", content="{}")],
    )
    session.history.append(AssistantMessage(text="rolled"))
    session.append_user("second")
    before = list(session.history)

    chat_loop.chat(session)
    fake_cfg.provider = "litellm"
    session.append_user("third")
    chat_loop.chat(session)
    fake_cfg.provider = "ollama"
    session.append_user("fourth")
    chat_loop.chat(session)

    assert providers_built == ["ollama", "litellm", "ollama"]
    # Nothing was dropped: the original prefix (tool round included) is intact.
    assert session.history[: len(before)] == before
    assert any(isinstance(m, ToolResultMessage) for m in session.history)
    # litellm received the ollama-era tool round untouched.
    litellm_history = seen["litellm"][0][0]
    assert (
        ToolResultMessage(call_id="a", name="roll_die", content="{}") in litellm_history
    )
    assert session.history[-1] == AssistantMessage(text="hi from ollama")


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
                ),
            ]
        ),
    )

    session = Session(system="sys", history=[UserMessage("first")])
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
                ),
                AssistantMessage(text="ok", tool_calls=[]),
            ]
        )
        session = Session(
            system="old-system", history=[UserMessage("remember tone=terse")]
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
                    ),
                    AssistantMessage(
                        text="done",
                        tool_calls=[],
                    ),
                ]
            )

        def chat_once(self, messages, tools_, system):
            snap = _active_session.get()
            assert snap is not None
            captured.append(snap)
            return next(self._replies)

    session = Session(system="sys", history=[UserMessage("roll")])
    _chat_with_provider(PeekProvider(), session)

    first, second = captured
    assert first.system == second.system == "sys"
    # Round 1: nothing completed yet beyond the user turn.
    assert [type(m) for m in first.history] == [UserMessage]
    # Round 2: the assistant tool call + its result are visible to tools.
    assert [type(m) for m in second.history] == [
        UserMessage,
        AssistantMessage,
        ToolResultMessage,
    ]
    assert second.history[-1].name == "roll_die"
    assert _active_session.get() is None


def test_provider_failure_leaves_session_untouched():
    class Boom:
        name = "boom"

        def chat_once(self, messages, tools_, system):
            raise RuntimeError("provider down")

    session = Session(system="sys", history=[UserMessage("hi")])
    before = (session.system, list(session.history))
    try:
        _chat_with_provider(Boom(), session)
    except RuntimeError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected provider error")
    assert (session.system, session.history) == before
    assert _active_session.get() is None


def test_two_calls_to_same_tool_in_one_round_get_paired_results():
    c1 = ToolCall(id="r1", name="roll_die", arguments={"sides": 6})
    c2 = ToolCall(id="r2", name="roll_die", arguments={"sides": 20})
    provider = FakeProvider(
        [
            AssistantMessage(text="", tool_calls=[c1, c2]),
            AssistantMessage(text="both rolled"),
        ]
    )
    session = Session(history=[UserMessage("roll two")])
    assert _chat_with_provider(provider, session) == "both rolled"
    results = [m for m in session.history if isinstance(m, ToolResultMessage)]
    assert [r.call_id for r in results] == ["r1", "r2"]
    assert all(r.name == "roll_die" for r in results)
    # Round two saw the exchange in order: user, assistant, result, result.
    assert [type(m) for m in provider.calls[1][0]] == [
        UserMessage,
        AssistantMessage,
        ToolResultMessage,
        ToolResultMessage,
    ]
