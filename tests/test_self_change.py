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


# Helpers that produce the full subprocess.run side-effect sequence.
# Call order in run_self_change:
#   1. git diff --name-only          (pre-snapshot: tracked dirty)
#   2. git ls-files --others ...     (pre-snapshot: untracked)
#   3. claude -p ...
#   4. git diff --name-only          (post-snapshot: tracked dirty)
#   5. git ls-files --others ...     (post-snapshot: untracked)
#   6. task lint
#   7. task build
#   8. task test
#   9. git diff --stat
#  10. [git checkout -- <files>]     (reject, only if new_tracked)
#  11. [git diff]                    (diff command)


def _seq(
    *,
    pre_tracked="",
    pre_untracked="",
    claude_rc=0,
    claude_out="done",
    post_tracked="",
    post_untracked="",
    lint_rc=0,
    build_rc=0,
    test_rc=0,
    diff_stat="",
    checkout_rc=0,
):
    return [
        _make_completed(0, pre_tracked),      # git diff --name-only (pre)
        _make_completed(0, pre_untracked),    # git ls-files (pre)
        _make_completed(claude_rc, claude_out),
        _make_completed(0, post_tracked),     # git diff --name-only (post)
        _make_completed(0, post_untracked),   # git ls-files (post)
        _make_completed(lint_rc),
        _make_completed(build_rc),
        _make_completed(test_rc),
        _make_completed(0, diff_stat),        # git diff --stat
        _make_completed(checkout_rc),         # git checkout (reject path)
    ]


# --- Bug 1: reject only reverts Claude's changes ---

def test_reject_only_reverts_claude_changes(tmp_path):
    """Rejecting must not touch files that were dirty before the self-change run."""
    for name in ("bot.py", "tools.py", "registry.py"):
        (tmp_path / name).write_text(f"# {name}")

    # tools.py was already dirty before claude ran; bot.py is new
    side_effects = _seq(
        pre_tracked="tools.py\n",
        post_tracked="tools.py\nbot.py\n",
        diff_stat="bot.py | 3 +++",
    )
    mock_run = MagicMock(side_effect=side_effects)
    with patch("subprocess.run", mock_run), patch("builtins.input", return_value="reject"):
        result = run_self_change("add a tool", tmp_path)

    assert result.approved is False
    assert result.files_changed == ["bot.py"]

    # Only bot.py should be checked out, not tools.py
    checkout_call = mock_run.call_args_list[-1]
    assert checkout_call == call(
        ["git", "checkout", "--", "bot.py"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_reject_with_new_untracked_file(tmp_path):
    """Untracked files created by Claude should be deleted on reject."""
    for name in ("bot.py", "tools.py", "registry.py"):
        (tmp_path / name).write_text(f"# {name}")

    new_file = tmp_path / "new_tool.py"
    new_file.write_text("# new")

    side_effects = [
        _make_completed(0, ""),           # pre tracked
        _make_completed(0, ""),           # pre untracked (clean before claude)
        _make_completed(0, "done"),       # claude
        _make_completed(0, ""),           # post tracked
        _make_completed(0, "new_tool.py\n"),  # post untracked
        _make_completed(0),               # lint
        _make_completed(0),               # build
        _make_completed(0),               # test
        _make_completed(0, ""),           # diff --stat
    ]
    with patch("subprocess.run", side_effect=side_effects), \
         patch("builtins.input", return_value="reject"):
        result = run_self_change("add a tool", tmp_path)

    assert result.approved is False
    assert result.files_changed == ["new_tool.py"]
    assert not new_file.exists()


# --- Bug 2: task build gate ---

def test_build_gate_runs(tmp_path):
    for name in ("bot.py", "tools.py", "registry.py"):
        (tmp_path / name).write_text(f"# {name}")

    side_effects = _seq()
    with patch("subprocess.run", side_effect=side_effects), \
         patch("builtins.input", return_value="reject"):
        result = run_self_change("add a tool", tmp_path)

    assert "build" in result.check_results
    assert result.checks_run == ["lint", "build", "test"]


def test_build_failure_surfaces(tmp_path):
    for name in ("bot.py", "tools.py", "registry.py"):
        (tmp_path / name).write_text(f"# {name}")

    side_effects = _seq(build_rc=1)
    with patch("subprocess.run", side_effect=side_effects), \
         patch("builtins.input", return_value="reject"):
        result = run_self_change("add a tool", tmp_path)

    assert result.check_results["build"] is False


# --- Bug 3: approve blocked on failures ---

def test_approve_blocked_on_failed_checks(tmp_path, capsys):
    for name in ("bot.py", "tools.py", "registry.py"):
        (tmp_path / name).write_text(f"# {name}")

    side_effects = _seq(lint_rc=1)
    with patch("subprocess.run", side_effect=side_effects), \
         patch("builtins.input", side_effect=["approve", "reject"]):
        result = run_self_change("add a tool", tmp_path)

    assert result.approved is False
    captured = capsys.readouterr()
    assert "approve!" in captured.out


def test_force_approve_overrides_failed_checks(tmp_path):
    for name in ("bot.py", "tools.py", "registry.py"):
        (tmp_path / name).write_text(f"# {name}")

    side_effects = _seq(lint_rc=1)
    with patch("subprocess.run", side_effect=side_effects), \
         patch("builtins.input", return_value="approve!"):
        result = run_self_change("add a tool", tmp_path)

    assert result.approved is True


def test_approve_blocked_when_claude_errored(tmp_path, capsys):
    for name in ("bot.py", "tools.py", "registry.py"):
        (tmp_path / name).write_text(f"# {name}")

    side_effects = _seq(claude_rc=1)
    with patch("subprocess.run", side_effect=side_effects), \
         patch("builtins.input", side_effect=["approve", "reject"]):
        result = run_self_change("add a tool", tmp_path)

    assert result.approved is False


# --- Existing tests updated for new call sequence ---

def test_validation_failure_surfaces_errors(tmp_path):
    for name in ("bot.py", "tools.py", "registry.py"):
        (tmp_path / name).write_text(f"# {name}")

    side_effects = _seq(lint_rc=1)
    with patch("subprocess.run", side_effect=side_effects), \
         patch("builtins.input", return_value="reject"):
        result = run_self_change("add a tool", tmp_path)

    assert result.check_results["lint"] is False
    assert result.check_results["build"] is True
    assert result.check_results["test"] is True


def test_change_log_written(tmp_path):
    for name in ("bot.py", "tools.py", "registry.py"):
        (tmp_path / name).write_text(f"# {name}")

    side_effects = _seq(post_tracked="bot.py\n", diff_stat="bot.py | 2 ++")
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
    for name in ("bot.py", "tools.py", "registry.py"):
        (tmp_path / name).write_text(f"# {name}")

    def smart_side_effect(*args, **_):
        cmd = args[0]
        if cmd[0] == "claude":
            raise subprocess.TimeoutExpired(cmd, 300)
        return _make_completed(0, "", "")

    with patch("subprocess.run", side_effect=smart_side_effect), \
         patch("builtins.input", return_value="reject"):
        result = run_self_change("add a tool", tmp_path)

    assert "timed out" in result.plan_output
