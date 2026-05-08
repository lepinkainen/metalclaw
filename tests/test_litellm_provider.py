from types import SimpleNamespace

import pytest

from providers.base import ToolCall


def _stub_response(*, content: str = "", tool_calls: list | None = None):
    msg = SimpleNamespace(content=content or None, tool_calls=tool_calls or None)
    choice = SimpleNamespace(message=msg)
    return SimpleNamespace(choices=[choice])


def _stub_tool_call(*, id: str, name: str, arguments: str):
    return SimpleNamespace(id=id, function=SimpleNamespace(name=name, arguments=arguments))


@pytest.fixture
def patched_completion(monkeypatch):
    """Capture all kwargs handed to litellm.completion."""
    captured: dict = {}

    def _stub(**kwargs):
        captured["kwargs"] = kwargs
        return captured.setdefault("response", _stub_response(content="ok"))

    import providers.litellm_provider as lp
    monkeypatch.setattr(lp.litellm, "completion", _stub)
    return captured


def test_chat_once_passes_model_and_messages(patched_completion):
    from providers.litellm_provider import LiteLLMProvider

    p = LiteLLMProvider(model="bedrock/anthropic.claude-haiku-4-5")
    out = p.chat_once(
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        system="be terse",
    )
    kwargs = patched_completion["kwargs"]
    assert kwargs["model"] == "bedrock/anthropic.claude-haiku-4-5"
    assert kwargs["messages"][0] == {"role": "system", "content": "be terse"}
    assert kwargs["messages"][1] == {"role": "user", "content": "hi"}
    assert kwargs["num_retries"] == 2
    assert "tools" not in kwargs
    assert out.text == "ok"
    assert out.raw == {"role": "assistant", "content": "ok"}
    assert out.tool_calls == []


def test_chat_once_threads_tools_when_present(patched_completion):
    from providers.litellm_provider import LiteLLMProvider

    p = LiteLLMProvider(model="bedrock/anthropic.claude-haiku-4-5")
    schema = [{"type": "function", "function": {"name": "roll", "parameters": {}}}]
    p.chat_once(messages=[{"role": "user", "content": "x"}], tools=schema, system="")
    assert patched_completion["kwargs"]["tools"] == schema


def test_chat_once_omits_system_when_empty(patched_completion):
    from providers.litellm_provider import LiteLLMProvider

    p = LiteLLMProvider(model="bedrock/anthropic.claude-haiku-4-5")
    p.chat_once(messages=[{"role": "user", "content": "x"}], tools=[], system="")
    msgs = patched_completion["kwargs"]["messages"]
    assert all(m["role"] != "system" for m in msgs)


def test_chat_once_extracts_tool_calls_and_round_trip_raw(patched_completion):
    from providers.litellm_provider import LiteLLMProvider

    patched_completion["response"] = _stub_response(
        content="",
        tool_calls=[
            _stub_tool_call(id="c1", name="roll_die", arguments='{"sides":6}'),
        ],
    )
    p = LiteLLMProvider(model="bedrock/anthropic.claude-haiku-4-5")
    out = p.chat_once(messages=[{"role": "user", "content": "roll"}], tools=[], system="")
    assert len(out.tool_calls) == 1
    tc = out.tool_calls[0]
    assert tc.id == "c1"
    assert tc.name == "roll_die"
    assert tc.arguments == {"sides": 6}
    assert out.raw == {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": "c1",
            "type": "function",
            "function": {"name": "roll_die", "arguments": '{"sides":6}'},
        }],
    }


def test_chat_once_handles_unparseable_arguments(patched_completion):
    from providers.litellm_provider import LiteLLMProvider

    patched_completion["response"] = _stub_response(
        tool_calls=[_stub_tool_call(id="c1", name="x", arguments="not-json")],
    )
    p = LiteLLMProvider(model="bedrock/anthropic.claude-haiku-4-5")
    out = p.chat_once(messages=[], tools=[], system="")
    assert out.tool_calls[0].arguments == {}


def test_aws_region_and_profile_threaded_to_completion(monkeypatch):
    captured = {}

    def _stub(**kwargs):
        captured.update(kwargs)
        return _stub_response(content="ok")

    import providers.litellm_provider as lp
    monkeypatch.setattr(lp.litellm, "completion", _stub)
    p = lp.LiteLLMProvider(
        model="bedrock/x",
        aws_region="eu-west-1",
        aws_profile="dev",
    )
    p.chat_once(messages=[{"role": "user", "content": "x"}], tools=[], system="")
    assert captured["aws_region_name"] == "eu-west-1"
    assert captured["aws_profile_name"] == "dev"


def test_format_tool_results_uses_openai_envelope():
    from providers.litellm_provider import LiteLLMProvider

    p = LiteLLMProvider(model="bedrock/x")
    call = ToolCall(id="c1", name="roll", arguments={})
    out = p.format_tool_results([(call, '{"value": 4}')])
    assert out == [{"role": "tool", "tool_call_id": "c1", "content": '{"value": 4}'}]
