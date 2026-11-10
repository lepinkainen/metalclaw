"""Parallel execution of parallel_safe tool calls from one model round (issue #56)."""

import threading
import time

import pytest

import chat_loop
import registry
from chat_loop import Session, _chat_with_provider, scoped_chat
from messages import AssistantMessage, ToolCall, ToolResultMessage, UserMessage
from registry import Tool


class _OneRoundProvider:
    """Returns ``calls`` in the first response, then a plain text answer."""

    name = "stub"

    def __init__(self, calls: list[ToolCall]):
        self._calls = calls
        self.rounds = 0

    def chat_once(self, messages, tools_, system):
        self.rounds += 1
        if self.rounds == 1:
            return AssistantMessage(text="", tool_calls=tuple(self._calls))
        return AssistantMessage(text="done")


def _register(monkeypatch, name, func, *, parallel_safe):
    schema = {
        "type": "function",
        "function": {"name": name, "description": name, "parameters": {}},
    }
    monkeypatch.setitem(
        registry.TOOLS,
        name,
        Tool(func=func, schema=schema, parallel_safe=parallel_safe),
    )


def _calls(*names: str) -> list[ToolCall]:
    return [ToolCall(id=f"c{i}", name=n, arguments={}) for i, n in enumerate(names)]


def _results(session: Session) -> list[ToolResultMessage]:
    return [m for m in session.history if isinstance(m, ToolResultMessage)]


@pytest.fixture
def cfg(cfg_file, write_config):
    write_config(cfg_file, max_tool_iterations=5, max_repeated_tool_calls=3)


def test_parallel_safe_calls_overlap_and_keep_call_order(cfg, monkeypatch):
    def slow(tag):
        def f():
            time.sleep(0.3)
            return {"tag": tag}

        return f

    _register(monkeypatch, "slow_a", slow("a"), parallel_safe=True)
    _register(monkeypatch, "slow_b", slow("b"), parallel_safe=True)

    session = Session(history=[UserMessage("go")])
    t0 = time.monotonic()
    out = _chat_with_provider(_OneRoundProvider(_calls("slow_a", "slow_b")), session)
    elapsed = time.monotonic() - t0

    assert out == "done"
    assert elapsed < 0.5
    res = _results(session)
    assert [(r.call_id, r.name) for r in res] == [("c0", "slow_a"), ("c1", "slow_b")]
    assert '"a"' in res[0].content and '"b"' in res[1].content


def test_round_with_unsafe_tool_runs_sequentially(cfg, monkeypatch):
    events: list[str] = []
    lock = threading.Lock()

    def recorder(tag):
        def f():
            with lock:
                events.append(f"start-{tag}")
            time.sleep(0.05)
            with lock:
                events.append(f"end-{tag}")
            return "ok"

        return f

    _register(monkeypatch, "safe_t", recorder("a"), parallel_safe=True)
    _register(monkeypatch, "unsafe_t", recorder("b"), parallel_safe=False)

    session = Session(history=[UserMessage("go")])
    _chat_with_provider(_OneRoundProvider(_calls("safe_t", "unsafe_t")), session)

    assert events == ["start-a", "end-a", "start-b", "end-b"]


def test_parallel_tool_sees_current_scope(cfg, monkeypatch):
    seen: list[str | None] = []
    lock = threading.Lock()

    def read_scope():
        with lock:
            seen.append(chat_loop.current_scope.get())
        return "ok"

    _register(monkeypatch, "scope_a", read_scope, parallel_safe=True)
    _register(monkeypatch, "scope_b", read_scope, parallel_safe=True)

    session = Session(history=[UserMessage("go")])
    provider = _OneRoundProvider(_calls("scope_a", "scope_b"))
    scoped_chat("telegram-42", lambda: _chat_with_provider(provider, session))

    assert seen == ["telegram-42", "telegram-42"]


def test_one_raising_parallel_tool_does_not_drop_the_other(cfg, monkeypatch):
    def boom():
        raise RuntimeError("kaput")

    _register(monkeypatch, "boom_t", boom, parallel_safe=True)
    _register(monkeypatch, "fine_t", lambda: {"ok": 1}, parallel_safe=True)

    session = Session(history=[UserMessage("go")])
    _chat_with_provider(_OneRoundProvider(_calls("boom_t", "fine_t")), session)

    res = _results(session)
    assert [r.call_id for r in res] == ["c0", "c1"]
    assert "Error: kaput" in res[0].content
    assert '"ok"' in res[1].content


def test_on_tool_call_fires_in_call_order(cfg, monkeypatch):
    def sleeper(delay):
        def f():
            time.sleep(delay)
            return "ok"

        return f

    # The first call finishes last; callbacks must still follow call order.
    _register(monkeypatch, "late_t", sleeper(0.2), parallel_safe=True)
    _register(monkeypatch, "early_t", sleeper(0.0), parallel_safe=True)

    order: list[str] = []
    session = Session(history=[UserMessage("go")])
    _chat_with_provider(
        _OneRoundProvider(_calls("late_t", "early_t")),
        session,
        on_tool_call=lambda name, args, result: order.append(name),
    )

    assert order == ["late_t", "early_t"]
