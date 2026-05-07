import bot
import tools
from providers.base import AssistantMessage


def test_escalate_returns_disabled_when_off(cfg_file, write_config):
    write_config(cfg_file, escalation_enabled=False)
    out = tools.escalate_to_big_model(query="x", reason="y")
    assert out["status"] == "disabled"


def test_escalate_routes_through_provider(cfg_file, write_config, monkeypatch):
    write_config(
        cfg_file,
        escalation_enabled=True,
        escalation_provider="anthropic",
        anthropic_api_key="sk-ant-test",
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
    token = bot._active_session_messages.set(session)
    try:
        out = tools.escalate_to_big_model(query="hard one", reason="too hard")
    finally:
        bot._active_session_messages.reset(token)

    assert out["status"] == "ok"
    assert out["reply"] == "cloud says hi"
    assert captured["provider_name"] == "anthropic"

    tool_names = {t["function"]["name"] for t in captured["tools"]}
    assert "escalate_to_big_model" not in tool_names, "recursion guard failed"
    assert "roll_die" in tool_names

    user_contents = [m["content"] for m in captured["messages"] if m.get("role") == "user"]
    assert any("earlier turn" in c for c in user_contents), "session snapshot missing"
    assert any("hard one" in c and "too hard" in c for c in user_contents)


def test_escalate_with_no_active_session(cfg_file, write_config, monkeypatch):
    write_config(
        cfg_file,
        escalation_enabled=True,
        escalation_provider="openai",
        openai_api_key="sk-test",
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
