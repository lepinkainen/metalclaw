import tools
from registry import TOOLS


def test_weather_tool_is_registered_under_weather_name():
    assert "weather" in TOOLS
    assert TOOLS["weather"].func is tools.weather
    assert "_day_summary" not in TOOLS


def test_inbox_and_history_tools_registered():
    assert {"append_to_inbox", "search_conversation_history"} <= set(TOOLS)
