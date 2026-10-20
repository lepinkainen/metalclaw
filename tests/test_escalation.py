import tools
from chat_loop import _active_session_messages
from providers.base import AssistantMessage


def test_escalate_returns_disabled_when_off(cfg_file, write_config):
    write_config(cfg_file, escalation_enabled=False)
    out = tools.escalate_to_big_model(query="x", reason="y")
    assert out["status"] == "disabled"


def test_escalate_routes_through_provider(cfg_file, write_config, monkeypatch):
    write_config(
        cfg_file,
        escalation_enabled=True,
        escalation_provider="litellm",
        litellm_model="bedrock/anthropic.claude-opus-4-7",
    )

    captured = {}

    class StubProvider:
        name = "stub"

        def chat_once(self, messages, tools_, system):
            captured["tools"] = list(tools_)
            captured["messages"] = list(messages)
            return AssistantMessage(
                text="cloud says hi",
                tool_calls=[],
                raw={"role": "assistant", "content": "cloud says hi"},
            )

        def format_tool_results(self, results):
            return []

    def fake_get_provider(name, *, model_override=None):
        captured["provider_name"] = name
        captured["model_override"] = model_override
        return StubProvider()

    monkeypatch.setattr("providers.get_provider", fake_get_provider)

    # Simulate an active session so the snapshot is exercised.
    session = [
        {"role": "system", "content": "you are local"},
        {"role": "user", "content": "earlier turn"},
    ]
    token = _active_session_messages.set(session)
    try:
        out = tools.escalate_to_big_model(query="hard one", reason="too hard")
    finally:
        _active_session_messages.reset(token)

    assert out["status"] == "ok"
    assert out["reply"] == "cloud says hi"
    assert captured["provider_name"] == "litellm"
    assert captured["model_override"] == "bedrock/anthropic.claude-opus-4-7"

    tool_names = {t["function"]["name"] for t in captured["tools"]}
    assert "escalate_to_big_model" not in tool_names, "recursion guard failed"
    assert "roll_die" in tool_names

    user_contents = [
        m["content"] for m in captured["messages"] if m.get("role") == "user"
    ]
    assert any("earlier turn" in c for c in user_contents), "session snapshot missing"
    assert any("hard one" in c and "too hard" in c for c in user_contents)


def test_escalate_with_no_active_session(cfg_file, write_config, monkeypatch):
    write_config(
        cfg_file,
        escalation_enabled=True,
        escalation_provider="litellm",
        litellm_model="bedrock/anthropic.claude-haiku-4-5",
    )

    class StubProvider:
        name = "stub"

        def chat_once(self, messages, tools_, system):
            return AssistantMessage(
                text="ok",
                tool_calls=[],
                raw={"role": "assistant", "content": "ok"},
            )

        def format_tool_results(self, results):
            return []

    monkeypatch.setattr("providers.get_provider", lambda *a, **k: StubProvider())

    out = tools.escalate_to_big_model(query="q", reason="r")
    assert out["status"] == "ok"


def test_escalate_strips_foreign_tool_history(cfg_file, write_config, monkeypatch):
    write_config(
        cfg_file,
        provider="ollama",
        escalation_enabled=True,
        escalation_provider="litellm",
        litellm_model="bedrock/anthropic.claude-haiku-4-5",
    )
    captured = {}

    class StubProvider:
        name = "stub"

        def chat_once(self, messages, tools_, system):
            captured["messages"] = list(messages)
            return AssistantMessage(text="ok", tool_calls=[], raw=None)

        def format_tool_results(self, results):
            return []

    monkeypatch.setattr("providers.get_provider", lambda *a, **k: StubProvider())

    # Ollama-shaped history: tool_calls without ids, tool result without tool_call_id
    session = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "roll a die"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {"name": "roll_die", "arguments": {}}}],
        },
        {"role": "tool", "name": "roll_die", "content": "4"},
        {"role": "assistant", "content": "You rolled a 4."},
    ]
    token = _active_session_messages.set(session)
    try:
        out = tools.escalate_to_big_model(query="q", reason="r")
    finally:
        _active_session_messages.reset(token)
    assert out["status"] == "ok"
    roles = [m["role"] for m in captured["messages"]]
    assert "tool" not in roles
    assert all("tool_calls" not in m for m in captured["messages"])
    assert {"role": "assistant", "content": "You rolled a 4."} in captured["messages"]
    # original session untouched
    assert len(session) == 5


def test_chat_via_escalation_cross_provider_appends_text_only(
    cfg_file, write_config, monkeypatch
):
    from chat_loop import chat_via_escalation

    write_config(
        cfg_file,
        provider="ollama",
        escalation_enabled=True,
        escalation_provider="litellm",
        litellm_model="bedrock/anthropic.claude-haiku-4-5",
    )

    class StubProvider:
        name = "stub"

        def chat_once(self, messages, tools_, system):
            assert all(m["role"] != "tool" for m in messages)
            return AssistantMessage(
                text="big answer",
                tool_calls=[],
                raw={"role": "assistant", "content": "big answer", "tool_calls": []},
            )

        def format_tool_results(self, results):
            return []

    monkeypatch.setattr("chat_loop.get_provider", lambda *a, **k: StubProvider())

    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "earlier"},
        {"role": "tool", "name": "roll_die", "content": "4"},
        {"role": "user", "content": "now"},
    ]
    out = chat_via_escalation(messages)
    assert out == "big answer"
    assert messages[-1] == {"role": "assistant", "content": "big answer"}
    assert len(messages) == 5  # original history intact, only text appended
