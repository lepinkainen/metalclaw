from types import SimpleNamespace

import pytest

from messages import AssistantMessage, ToolCall, ToolResultMessage, UserMessage
from providers.ollama import OllamaProvider, fetch_model_defaults


@pytest.fixture
def patched_post(monkeypatch):
    captured: dict = {}

    def _stub_post(url, json):
        captured["url"] = url
        captured["json"] = json
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"message": {"content": "ok"}},
        )

    import providers.ollama as op

    monkeypatch.setattr(op._CLIENT, "post", _stub_post)
    return captured


def test_sampling_omitted_when_none(patched_post):
    p = OllamaProvider(url="http://x/api/chat", model="gemma4:e4b")
    p.chat_once(history=[UserMessage("hi")], tools=[], system="")
    assert "options" not in patched_post["json"]


def test_sampling_passed_in_options(patched_post):
    p = OllamaProvider(
        url="http://x/api/chat",
        model="gemma4:e4b",
        temperature=0.2,
        top_p=0.9,
        top_k=40,
    )
    p.chat_once(history=[UserMessage("hi")], tools=[], system="")
    assert patched_post["json"]["options"] == {
        "temperature": 0.2,
        "top_p": 0.9,
        "top_k": 40,
    }


def test_partial_sampling_options(patched_post):
    p = OllamaProvider(url="http://x/api/chat", model="gemma4:e4b", top_k=40)
    p.chat_once(history=[UserMessage("hi")], tools=[], system="")
    assert patched_post["json"]["options"] == {"top_k": 40}


def test_fetch_model_defaults_parses_parameters(monkeypatch):
    captured: dict = {}

    def _stub_post(url, json, timeout):
        captured["url"] = url
        captured["json"] = json
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {
                "parameters": "temperature                    1\ntop_k                          64\ntop_p                          0.95\n"
            },
        )

    import providers.ollama as op

    monkeypatch.setattr(op._CLIENT, "post", _stub_post)
    out = fetch_model_defaults("http://x/api/chat", "gemma4:e4b")
    assert out == {"temperature": 1.0, "top_k": 64, "top_p": 0.95}
    assert captured["url"] == "http://x/api/show"
    assert captured["json"] == {"name": "gemma4:e4b"}


def test_fetch_model_defaults_returns_empty_on_error(monkeypatch):
    def _stub_post(url, json, timeout):
        raise RuntimeError("boom")

    import providers.ollama as op

    monkeypatch.setattr(op._CLIENT, "post", _stub_post)
    assert fetch_model_defaults("http://x/api/chat", "gemma4:e4b") == {}


def test_to_wire_emits_ollama_shapes():
    history = [
        UserMessage("roll"),
        AssistantMessage(
            text="",
            tool_calls=[ToolCall(id="c1", name="roll_die", arguments={"sides": 6})],
        ),
        ToolResultMessage(call_id="c1", name="roll_die", content='{"value": 4}'),
        AssistantMessage(text="You rolled a 4."),
    ]
    assert OllamaProvider._to_wire(history) == [
        {"role": "user", "content": "roll"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"function": {"name": "roll_die", "arguments": {"sides": 6}}}
            ],
        },
        {"role": "tool", "name": "roll_die", "content": '{"value": 4}'},
        {"role": "assistant", "content": "You rolled a 4."},
    ]


def test_chat_once_sends_wire_history_after_system(patched_post):
    p = OllamaProvider(url="http://x/api/chat", model="gemma4:e4b")
    p.chat_once(history=[UserMessage("hi")], tools=[], system="be terse")
    assert patched_post["json"]["messages"] == [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "hi"},
    ]
