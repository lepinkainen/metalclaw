from chat_loop import _parse_command


def test_add_tool_with_args():
    assert _parse_command("/add-tool fetch current Bitcoin price") == (
        "add-tool",
        "fetch current Bitcoin price",
    )


def test_hyphenated_command_with_args():
    assert _parse_command("/add-tool fix the weather tool timeout") == (
        "add-tool",
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


def test_parse_command_bare_slash_is_unknown_command():
    from chat_loop import _parse_command

    assert _parse_command("/") == ("", "")
    assert _parse_command("/   ") == ("", "")
    assert _parse_command("/help") == ("help", "")
    assert _parse_command("/big  hard  q") == ("big", "hard  q")
