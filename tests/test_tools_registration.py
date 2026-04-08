from registry import TOOLS
import tools  # noqa: F401


def test_weather_tool_is_registered_under_weather_name():
    assert "weather" in TOOLS
    assert TOOLS["weather"].func is tools.weather
    assert "_day_summary" not in TOOLS
