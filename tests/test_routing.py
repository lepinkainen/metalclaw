from bot import _parse_command


def test_add_tool_with_args():
    assert _parse_command("/add-tool fetch current Bitcoin price") == (
        "add-tool",
        "fetch current Bitcoin price",
    )


def test_self_edit_with_args():
    assert _parse_command("/self-edit fix the weather tool timeout") == (
        "self-edit",
        "fix the weather tool timeout",
    )


def test_command_no_args():
    assert _parse_command("/add-tool") == ("add-tool", "")


def test_help_command():
    assert _parse_command("/help") == ("help", "")


def test_unknown_command():
    assert _parse_command("/foobar") == ("foobar", "")


def test_not_a_command_returns_none():
    assert _parse_command("what's the weather?") is None


def test_plain_sentence_not_a_command():
    assert _parse_command("add a tool please") is None
