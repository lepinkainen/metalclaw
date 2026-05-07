from collections.abc import Iterator

import bot
import tools  # noqa: F401 — register tools
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
    out = bot._chat_with_provider(provider, messages)
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
    out = bot._chat_with_provider(provider, messages)
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
    bot._chat_with_provider(
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
            captured.append(bot._active_session_messages.get())
            return AssistantMessage(
                text="done", tool_calls=[], raw={"role": "assistant", "content": "done"}
            )

        def format_tool_results(self, results):
            return []

    messages = [{"role": "user", "content": "x"}]
    bot._chat_with_provider(PeekProvider(), messages)
    assert captured == [messages]
    # Resets on exit
    assert bot._active_session_messages.get() is None


def test_split_system_extracts_first_message_only():
    sys, hist = bot._split_system([
        {"role": "system", "content": "S"},
        {"role": "user", "content": "U"},
    ])
    assert sys == "S"
    assert hist == [{"role": "user", "content": "U"}]


def test_split_system_handles_no_system_role():
    sys, hist = bot._split_system([{"role": "user", "content": "U"}])
    assert sys == ""
    assert hist == [{"role": "user", "content": "U"}]


def test_run_tool_returns_error_for_unknown_name():
    out = bot._run_tool("nonexistent_tool", {})
    assert "unknown tool" in str(out)


def test_run_tool_executes_registered_tool():
    out = bot._run_tool("roll_die", {"sides": 6})
    assert "Rolled" in str(out)
