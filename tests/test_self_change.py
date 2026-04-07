import json
import subprocess
from unittest.mock import MagicMock, call, patch

from self_change import run_self_change


def _make_completed(rc=0, stdout="", stderr=""):
    r = MagicMock()
    r.returncode = rc
    r.stdout = stdout
    r.stderr = stderr
    return r


def test_reject_discards_changes(tmp_path):
    (tmp_path / "bot.py").write_text("# bot")
    (tmp_path / "tools.py").write_text("# tools")
    (tmp_path / "registry.py").write_text("# registry")

    claude_ok = _make_completed(0, "done", "")
    lint_ok = _make_completed(0, "", "")
    test_ok = _make_completed(0, "", "")
    diff_stat = _make_completed(0, "1 file changed", "")
    diff_names = _make_completed(0, "bot.py\n", "")
    checkout = _make_completed(0, "", "")

    side_effects = [claude_ok, lint_ok, test_ok, diff_stat, diff_names, checkout]

    mock_run = MagicMock(side_effect=side_effects)
    with patch("subprocess.run", mock_run), \
         patch("builtins.input", return_value="reject"):
        result = run_self_change("add a tool", tmp_path)

    assert result.approved is False
    assert mock_run.call_args_list[-1] == call(
        ["git", "checkout", "--", "."],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_validation_failure_surfaces_errors(tmp_path):
    (tmp_path / "bot.py").write_text("# bot")
    (tmp_path / "tools.py").write_text("# tools")
    (tmp_path / "registry.py").write_text("# registry")

    claude_ok = _make_completed(0, "done", "")
    lint_fail = _make_completed(1, "", "E501 line too long")
    test_ok = _make_completed(0, "", "")
    diff_stat = _make_completed(0, "", "")
    diff_names = _make_completed(0, "", "")
    checkout = _make_completed(0, "", "")

    side_effects = [claude_ok, lint_fail, test_ok, diff_stat, diff_names, checkout]

    with patch("subprocess.run", side_effect=side_effects), \
         patch("builtins.input", return_value="reject"):
        result = run_self_change("add a tool", tmp_path)

    assert result.check_results["lint"] is False
    assert result.check_results["test"] is True


def test_change_log_written(tmp_path):
    (tmp_path / "bot.py").write_text("# bot")
    (tmp_path / "tools.py").write_text("# tools")
    (tmp_path / "registry.py").write_text("# registry")

    claude_ok = _make_completed(0, "done", "")
    lint_ok = _make_completed(0, "", "")
    test_ok = _make_completed(0, "", "")
    diff_stat = _make_completed(0, "bot.py | 2 ++", "")
    diff_names = _make_completed(0, "bot.py\n", "")

    side_effects = [claude_ok, lint_ok, test_ok, diff_stat, diff_names]

    with patch("subprocess.run", side_effect=side_effects), \
         patch("builtins.input", return_value="approve"):
        result = run_self_change("add a tool", tmp_path)

    assert result.approved is True
    log_path = tmp_path / "changes.jsonl"
    assert log_path.exists()
    entry = json.loads(log_path.read_text().strip())
    assert entry["request"] == "add a tool"
    assert entry["approved"] is True
    assert "timestamp" in entry
    assert "files_changed" in entry
    assert "check_results" in entry


def test_claude_timeout_surfaces_as_failure(tmp_path):
    (tmp_path / "bot.py").write_text("# bot")
    (tmp_path / "tools.py").write_text("# tools")
    (tmp_path / "registry.py").write_text("# registry")

    def smart_side_effect(*args, **_):
        cmd = args[0]
        if cmd[0] == "claude":
            raise subprocess.TimeoutExpired(cmd, 300)
        return _make_completed(0, "", "")

    with patch("subprocess.run", side_effect=smart_side_effect), \
         patch("builtins.input", return_value="reject"):
        result = run_self_change("add a tool", tmp_path)

    assert "timed out" in result.plan_output
