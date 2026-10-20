from types import SimpleNamespace

import pytest

from messages import AssistantMessage, ToolCall, ToolResultMessage, UserMessage


def _stub_response(*, content: str = "", tool_calls: list | None = None):
    msg = SimpleNamespace(content=content or None, tool_calls=tool_calls or None)
    choice = SimpleNamespace(message=msg)
    return SimpleNamespace(choices=[choice])


def _stub_response_content(content):
    msg = SimpleNamespace(content=content, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def _stub_tool_call(*, id: str, name: str, arguments: str):
    return SimpleNamespace(
        id=id, function=SimpleNamespace(name=name, arguments=arguments)
    )


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
        history=[UserMessage("hi")],
        tools=[],
        system="be terse",
    )
    kwargs = patched_completion["kwargs"]
    assert kwargs["model"] == "bedrock/anthropic.claude-haiku-4-5"
    # Claude model + caching on by default: system goes out as a cached block.
    assert kwargs["messages"][0] == {
        "role": "system",
        "content": [
            {"type": "text", "text": "be terse", "cache_control": {"type": "ephemeral"}}
        ],
    }
    assert kwargs["messages"][1] == {"role": "user", "content": "hi"}
    assert kwargs["num_retries"] == 2
    assert "tools" not in kwargs
    assert out.text == "ok"
    assert out == AssistantMessage(text="ok")
    assert out.tool_calls == ()


def test_chat_once_threads_tools_when_present(patched_completion):
    from providers.litellm_provider import LiteLLMProvider

    p = LiteLLMProvider(model="openai/gpt-4o")
    schema = [{"type": "function", "function": {"name": "roll", "parameters": {}}}]
    p.chat_once(history=[UserMessage("x")], tools=schema, system="")
    assert patched_completion["kwargs"]["tools"] == schema


_EPHEMERAL = {"type": "ephemeral"}
_TWO_TOOLS = [
    {"type": "function", "function": {"name": "roll", "parameters": {}}},
    {"type": "function", "function": {"name": "weather", "parameters": {}}},
]


@pytest.mark.parametrize(
    "model",
    [
        "anthropic/claude-haiku-4-5",
        "bedrock/anthropic.claude-haiku-4-5",
        "bedrock/us.anthropic.claude-opus-4-7-v1:0",
        "bedrock/eu.claude-sonnet-4-5",
    ],
)
def test_prompt_caching_marks_system_and_last_tool_for_claude(
    patched_completion, model
):
    from providers.litellm_provider import LiteLLMProvider

    p = LiteLLMProvider(model=model)
    p.chat_once(history=[UserMessage("x")], tools=_TWO_TOOLS, system="sys")
    kwargs = patched_completion["kwargs"]
    assert kwargs["messages"][0] == {
        "role": "system",
        "content": [{"type": "text", "text": "sys", "cache_control": _EPHEMERAL}],
    }
    tools = kwargs["tools"]
    assert "cache_control" not in tools[0]
    assert tools[1] == {**_TWO_TOOLS[1], "cache_control": _EPHEMERAL}
    assert tools[1]["function"] == _TWO_TOOLS[1]["function"]
    # The registry's shared schema dicts must not be mutated in place.
    assert "cache_control" not in _TWO_TOOLS[1]


@pytest.mark.parametrize(
    "model",
    [
        "openai/gpt-4o",
        "gemini/gemini-1.5-pro",
        "bedrock/amazon.nova-pro-v1:0",
        "bedrock/meta.llama3-70b-instruct-v1:0",
    ],
)
def test_prompt_caching_skipped_for_non_anthropic_models(patched_completion, model):
    from providers.litellm_provider import LiteLLMProvider

    p = LiteLLMProvider(model=model)
    assert p.prompt_caching is False
    p.chat_once(history=[UserMessage("x")], tools=_TWO_TOOLS, system="sys")
    kwargs = patched_completion["kwargs"]
    assert kwargs["messages"][0] == {"role": "system", "content": "sys"}
    assert kwargs["tools"] == _TWO_TOOLS
    assert all("cache_control" not in t for t in kwargs["tools"])


def test_prompt_caching_flag_off_disables_for_claude(patched_completion):
    from providers.litellm_provider import LiteLLMProvider

    p = LiteLLMProvider(model="anthropic/claude-haiku-4-5", prompt_caching=False)
    p.chat_once(history=[UserMessage("x")], tools=_TWO_TOOLS, system="sys")
    kwargs = patched_completion["kwargs"]
    assert kwargs["messages"][0] == {"role": "system", "content": "sys"}
    assert kwargs["tools"] == _TWO_TOOLS


def test_get_provider_threads_prompt_caching_from_config(cfg_file, write_config):
    import config
    from providers import get_provider

    write_config(cfg_file, provider="litellm", litellm_prompt_caching=False)
    config.reset_cache()
    assert get_provider("litellm").prompt_caching is False  # type: ignore[attr-defined]
    write_config(cfg_file, provider="litellm")
    config.reset_cache()
    assert get_provider("litellm").prompt_caching is True  # type: ignore[attr-defined]


def test_prompt_caching_leaves_volatile_system_tail_uncached(patched_completion):
    from messages import SYSTEM_CACHE_BOUNDARY
    from providers.litellm_provider import LiteLLMProvider

    p = LiteLLMProvider(model="anthropic/claude-sonnet-4-5")
    system = "stable rules" + SYSTEM_CACHE_BOUNDARY + "The current date and time is X."
    p.chat_once(history=[UserMessage("x")], tools=[], system=system)
    assert patched_completion["kwargs"]["messages"][0] == {
        "role": "system",
        "content": [
            {"type": "text", "text": "stable rules", "cache_control": _EPHEMERAL},
            {"type": "text", "text": "The current date and time is X."},
        ],
    }


def test_caching_off_flattens_system_boundary(patched_completion):
    from messages import SYSTEM_CACHE_BOUNDARY
    from providers.litellm_provider import LiteLLMProvider

    p = LiteLLMProvider(model="anthropic/claude-sonnet-4-5", prompt_caching=False)
    p.chat_once(history=[], tools=[], system="a" + SYSTEM_CACHE_BOUNDARY + "b")
    assert patched_completion["kwargs"]["messages"][0] == {
        "role": "system",
        "content": "a\n\nb",
    }


def test_usage_carries_cache_token_counts(patched_completion):
    from providers.litellm_provider import LiteLLMProvider

    resp = _stub_response(content="ok")
    resp.usage = SimpleNamespace(
        prompt_tokens=1200,
        completion_tokens=5,
        prompt_tokens_details=SimpleNamespace(
            cached_tokens=1000, cache_write_tokens=150
        ),
    )
    patched_completion["response"] = resp
    p = LiteLLMProvider(model="anthropic/claude-haiku-4-5")
    out = p.chat_once(history=[UserMessage("x")], tools=[], system="sys")
    assert out.usage is not None
    assert out.usage.prompt_tokens == 1200
    assert out.usage.cache_read_tokens == 1000
    assert out.usage.cache_write_tokens == 150


def test_usage_cache_fields_none_without_details(patched_completion):
    from providers.litellm_provider import LiteLLMProvider

    resp = _stub_response(content="ok")
    resp.usage = SimpleNamespace(prompt_tokens=10, completion_tokens=2)
    patched_completion["response"] = resp
    p = LiteLLMProvider(model="openai/gpt-4o")
    out = p.chat_once(history=[], tools=[], system="")
    assert out.usage is not None
    assert out.usage.cache_read_tokens is None
    assert out.usage.cache_write_tokens is None


def test_chat_once_omits_system_when_empty(patched_completion):
    from providers.litellm_provider import LiteLLMProvider

    p = LiteLLMProvider(model="bedrock/anthropic.claude-haiku-4-5")
    p.chat_once(history=[UserMessage("x")], tools=[], system="")
    msgs = patched_completion["kwargs"]["messages"]
    assert all(m["role"] != "system" for m in msgs)


def test_chat_once_extracts_tool_calls(patched_completion):
    from providers.litellm_provider import LiteLLMProvider

    patched_completion["response"] = _stub_response(
        content="",
        tool_calls=[
            _stub_tool_call(id="c1", name="roll_die", arguments='{"sides":6}'),
        ],
    )
    p = LiteLLMProvider(model="bedrock/anthropic.claude-haiku-4-5")
    out = p.chat_once(history=[UserMessage("roll")], tools=[], system="")
    assert len(out.tool_calls) == 1
    tc = out.tool_calls[0]
    assert tc.id == "c1"
    assert tc.name == "roll_die"
    assert tc.arguments == {"sides": 6}
    assert out.text == ""


def test_chat_once_handles_unparseable_arguments(patched_completion):
    from providers.litellm_provider import LiteLLMProvider

    patched_completion["response"] = _stub_response(
        tool_calls=[_stub_tool_call(id="c1", name="x", arguments="not-json")],
    )
    p = LiteLLMProvider(model="bedrock/anthropic.claude-haiku-4-5")
    out = p.chat_once(history=[], tools=[], system="")
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
    p.chat_once(history=[UserMessage("x")], tools=[], system="")
    assert captured["aws_region_name"] == "eu-west-1"
    assert captured["aws_profile_name"] == "dev"


def test_to_wire_uses_openai_envelope():
    from providers.litellm_provider import LiteLLMProvider

    history = [
        UserMessage("roll"),
        AssistantMessage(
            text="", tool_calls=[ToolCall(id="c1", name="roll", arguments={"sides": 6})]
        ),
        ToolResultMessage(call_id="c1", name="roll", content='{"value": 4}'),
    ]
    assert LiteLLMProvider._to_wire(history) == [
        {"role": "user", "content": "roll"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "roll", "arguments": '{"sides": 6}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": '{"value": 4}'},
    ]


def test_chat_once_joins_text_content_blocks(patched_completion):
    from providers.litellm_provider import LiteLLMProvider

    patched_completion["response"] = _stub_response_content(
        [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
    )
    p = LiteLLMProvider(model="bedrock/x")
    assert p.chat_once(history=[], tools=[], system="").text == "ab"


def test_chat_once_rejects_unknown_content_block(patched_completion):
    from providers.litellm_provider import LiteLLMProvider

    patched_completion["response"] = _stub_response_content(
        [{"type": "image", "data": "..."}]
    )
    p = LiteLLMProvider(model="bedrock/x")
    with pytest.raises(ValueError, match="unsupported content block"):
        p.chat_once(history=[], tools=[], system="")
