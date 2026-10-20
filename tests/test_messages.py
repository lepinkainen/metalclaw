import pytest

from chat_loop import Session
from messages import (
    AssistantMessage,
    ToolCall,
    ToolResultMessage,
    UserMessage,
    validate_exchange,
)


def _call(cid: str) -> ToolCall:
    return ToolCall(id=cid, name="roll_die", arguments={})


def _result(cid: str) -> ToolResultMessage:
    return ToolResultMessage(call_id=cid, name="roll_die", content="{}")


def test_validate_exchange_accepts_matching_ids_any_order():
    validate_exchange(
        AssistantMessage(text="", tool_calls=[_call("a"), _call("b")]),
        [_result("b"), _result("a")],
    )


def test_validate_exchange_rejects_missing_result():
    with pytest.raises(ValueError, match="missing=\\['b'\\]"):
        validate_exchange(
            AssistantMessage(text="", tool_calls=[_call("a"), _call("b")]),
            [_result("a")],
        )


def test_validate_exchange_rejects_unexpected_result():
    with pytest.raises(ValueError, match="unexpected=\\['zz'\\]"):
        validate_exchange(
            AssistantMessage(text="", tool_calls=[_call("a")]),
            [_result("a"), _result("zz")],
        )


def test_validate_exchange_rejects_duplicate_result_ids():
    with pytest.raises(ValueError, match="duplicate tool result ids"):
        validate_exchange(
            AssistantMessage(text="", tool_calls=[_call("a")]),
            [_result("a"), _result("a")],
        )


def test_validate_exchange_rejects_duplicate_call_ids():
    with pytest.raises(ValueError, match="duplicate tool_call ids"):
        validate_exchange(
            AssistantMessage(text="", tool_calls=[_call("a"), _call("a")]),
            [_result("a")],
        )


def test_session_append_exchange_commits_in_order_or_not_at_all():
    s = Session(history=[UserMessage("go")])
    am = AssistantMessage(text="", tool_calls=[_call("a")])
    with pytest.raises(ValueError):
        s.append_exchange(am, [])
    assert s.history == [UserMessage("go")]
    s.append_exchange(am, [_result("a")])
    assert s.history == [UserMessage("go"), am, _result("a")]


def test_assistant_message_is_frozen_and_normalises_tool_calls():
    am = AssistantMessage(text="x", tool_calls=[_call("a")])
    assert am.tool_calls == (_call("a"),)
    with pytest.raises(AttributeError):
        am.text = "y"  # type: ignore[misc]
    # usage never affects equality
    from messages import Usage

    assert AssistantMessage(text="x", usage=Usage(1, 2)) == AssistantMessage(text="x")
