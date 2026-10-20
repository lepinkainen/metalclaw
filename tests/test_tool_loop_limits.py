"""Guards against runaway tool loops (issue #30)."""

from chat_loop import _chat_with_provider
from providers.base import AssistantMessage, ToolCall


class _LoopingProvider:
    """Always asks for the same tool call until tools are withheld."""

    name = "stub"

    def __init__(self):
        self.calls: list[list[dict]] = []

    def chat_once(self, messages, tools_, system):
        self.calls.append(list(tools_))
        if not tools_:
            return AssistantMessage(
                text="final answer",
                tool_calls=[],
                raw={"role": "assistant", "content": "final answer"},
            )
        return AssistantMessage(
            text="",
            tool_calls=[ToolCall(id="1", name="roll_die", arguments={"sides": 6})],
            raw={"role": "assistant", "content": "", "tool_calls": [{"n": 1}]},
        )

    def format_tool_results(self, results):
        return [{"role": "tool", "content": r} for _, r in results]


def test_iteration_cap_forces_tool_less_final_call(cfg_file, write_config):
    write_config(cfg_file, max_tool_iterations=4, max_repeated_tool_calls=100)
    import tools  # noqa: F401  (registers roll_die)

    provider = _LoopingProvider()
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}]
    out = _chat_with_provider(provider, messages)

    assert out == "final answer"
    # 4 tool-bearing calls, then exactly one call with an empty tool list.
    assert len(provider.calls) == 5
    assert all(provider.calls[i] for i in range(4))
    assert provider.calls[4] == []
    assert messages[-1] == {"role": "assistant", "content": "final answer"}


def test_repeated_identical_call_gets_error_result_instead_of_rerun(
    cfg_file, write_config, monkeypatch
):
    write_config(cfg_file, max_tool_iterations=10, max_repeated_tool_calls=3)
    import tools  # noqa: F401

    ran = []
    monkeypatch.setattr(
        "chat_loop._run_tool", lambda name, args: ran.append(name) or {"n": 4}
    )

    provider = _LoopingProvider()
    messages = [{"role": "user", "content": "go"}]
    _chat_with_provider(provider, messages)

    # Rounds 1-2 execute; round 3 onwards are identical -> refused, not run.
    assert ran == ["roll_die", "roll_die"]
    refused = [
        m for m in messages if m.get("role") == "tool" and "Error:" in m["content"]
    ]
    assert refused, "stuck call should be answered with an Error: tool result"
    assert "Do not call it again" in refused[0]["content"]


def test_cap_with_model_ignoring_empty_tools_returns_error_without_dangling_calls(
    cfg_file, write_config, monkeypatch
):
    write_config(cfg_file, max_tool_iterations=2, max_repeated_tool_calls=100)
    import tools  # noqa: F401

    monkeypatch.setattr("chat_loop._run_tool", lambda name, args: {"n": 4})

    class Stubborn(_LoopingProvider):
        def chat_once(self, messages, tools_, system):
            self.calls.append(list(tools_))
            return AssistantMessage(
                text="",
                tool_calls=[ToolCall(id="1", name="roll_die", arguments={})],
                raw={"role": "assistant", "content": "", "tool_calls": [{"n": 1}]},
            )

    provider = Stubborn()
    messages = [{"role": "user", "content": "go"}]
    out = _chat_with_provider(provider, messages)

    assert out.startswith("Error: stopped after 2 tool-call rounds")
    assert len(provider.calls) == 3
    # Last recorded message is a tool result, not an assistant tool_call
    # without results.
    assert messages[-1]["role"] == "tool"
