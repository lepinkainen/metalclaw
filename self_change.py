import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class SelfChangeResult:
    request: str
    plan_output: str
    files_changed: list[str]
    checks_run: list[str]
    check_results: dict[str, bool]
    diff_summary: str
    approved: bool | None = None


def _read_file(path: Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return f"<could not read {path.name}>"


def _run(cmd: list[str], cwd: Path, timeout: int = 60) -> tuple[int, str, str]:
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"timed out after {timeout}s"


def _dirty_tracked(repo_root: Path) -> set[str]:
    _, out, _ = _run(["git", "diff", "--name-only"], repo_root)
    return set(out.strip().splitlines())


def _untracked(repo_root: Path) -> set[str]:
    _, out, _ = _run(["git", "ls-files", "--others", "--exclude-standard"], repo_root)
    return set(out.strip().splitlines())


def run_self_change(request: str, repo_root: Path) -> SelfChangeResult:
    bot_src = _read_file(repo_root / "bot.py")
    tools_src = _read_file(repo_root / "tools.py")
    registry_src = _read_file(repo_root / "registry.py")

    # Snapshot pre-existing dirty state so reject only reverts Claude's changes
    pre_tracked = _dirty_tracked(repo_root)
    pre_untracked = _untracked(repo_root)

    prompt = f"""You are acting as a coding assistant for the Metalclaw bot project.
Repo root: {repo_root}

User request: {request}

Your task: implement the user's request by editing files inside the repo at {repo_root}.
Only edit files within that directory. Do not create files outside the repo.

Current file contents:

=== bot.py ===
{bot_src}

=== tools.py ===
{tools_src}

=== registry.py ===
{registry_src}

Make the minimal changes needed to fulfil the request. Edit existing files or create new ones as needed.
"""

    rc, stdout, stderr = _run(
        ["claude", "-p", prompt, "--allowedTools", "Edit,Write,Read"],
        cwd=repo_root,
        timeout=300,
    )

    plan_output = stdout
    if rc != 0:
        plan_output += f"\n[claude exited with code {rc}]\n{stderr}"

    # Compute delta — only files Claude touched
    new_tracked = _dirty_tracked(repo_root) - pre_tracked
    new_untracked = _untracked(repo_root) - pre_untracked
    files_changed = sorted(new_tracked | new_untracked)

    checks_run: list[str] = ["lint", "build", "test"]
    check_results: dict[str, bool] = {}

    lint_rc, lint_out, lint_err = _run(["task", "lint"], repo_root)
    check_results["lint"] = lint_rc == 0
    if not check_results["lint"]:
        print(f"lint failed:\n{lint_out}{lint_err}")

    build_rc, build_out, build_err = _run(["task", "build"], repo_root)
    check_results["build"] = build_rc == 0
    if not check_results["build"]:
        print(f"build failed:\n{build_out}{build_err}")

    test_rc, test_out, test_err = _run(["task", "test"], repo_root)
    check_results["test"] = test_rc == 0
    if not check_results["test"]:
        print(f"test failed:\n{test_out}{test_err}")

    _, diff_stat, _ = _run(["git", "diff", "--stat"], repo_root)
    diff_summary = diff_stat.strip() or "(no changes)"

    result = SelfChangeResult(
        request=request,
        plan_output=plan_output,
        files_changed=files_changed,
        checks_run=checks_run,
        check_results=check_results,
        diff_summary=diff_summary,
    )

    all_checks_passed = rc == 0 and all(check_results.values())

    print("\n--- self-change summary ---")
    print(f"files changed: {', '.join(files_changed) or 'none'}")
    print(f"checks: {check_results}")
    print(f"diff summary:\n{diff_summary}")

    while True:
        try:
            choice = input("\n[approve / approve! / reject / diff]> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            choice = "reject"

        if choice == "approve":
            if not all_checks_passed:
                print("validation failed — use 'approve!' to override")
            else:
                result.approved = True
                _log_change(repo_root, result)
                break
        elif choice == "approve!":
            result.approved = True
            _log_change(repo_root, result)
            break
        elif choice == "reject":
            result.approved = False
            if new_tracked:
                _run(["git", "checkout", "--"] + sorted(new_tracked), repo_root)
            for f in sorted(new_untracked):
                (repo_root / f).unlink(missing_ok=True)
            print("Changes discarded.")
            break
        elif choice == "diff":
            _, full_diff, _ = _run(["git", "diff"], repo_root)
            print(full_diff or "(no diff)")
        else:
            print("Please type approve, approve!, reject, or diff.")

    return result


def _log_change(repo_root: Path, result: SelfChangeResult) -> None:
    log_path = repo_root / "changes.jsonl"
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "request": result.request,
        "files_changed": result.files_changed,
        "checks_run": result.checks_run,
        "check_results": result.check_results,
        "diff_summary": result.diff_summary,
        "approved": result.approved,
    }
    with log_path.open("a") as f:
        f.write(json.dumps(entry) + "\n")
