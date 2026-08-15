"""Verify one engineer branch or existing pull request in a disposable, discarded worktree."""

from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from subprocess import CalledProcessError, TimeoutExpired, run
from typing import Any

from repo_agent.engineering import (
    _git_output,
    _load_repository_info,
    _workspace_path,
    _write_json_atomic,
)
from repo_agent.planning import _positive_integer

# `check` must run the repository's CI-equivalent aggregate command (normally `make check`).
# The component gates remain useful evidence, but they are not a substitute for the exact command
# CI will execute before a publisher is allowed to create a pull request.
_GATE_ORDER = ("bootstrap", "format", "lint", "test", "coverage", "security", "check")
_TESTABLE_STATUSES = frozenset(
    {"existing_pull_request_ready_for_testing", "implementation_applied"}
)
_OUTPUT_LIMIT = 4_000
_WORKTREE_DIRECTORY = ".repo-agent-worktrees"
_REDACTED_ENVIRONMENT = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN")
_PULL_REQUEST_URL = re.compile(r"^https?://[^/\s]+/([^/\s]+)/([^/\s]+)/pull/(\d+)/?$")
_COVERAGE_ARTIFACTS = ("coverage.json", "coverage.xml")
_COVERAGE_FILE_LIMIT = 8_000_000
_COVERAGE_TOTAL_LINE = re.compile(r"^TOTAL\s+.*?([0-9]+(?:\.[0-9]+)?)%", re.MULTILINE)
_COVERAGE_LINE_RATE = re.compile(r"<coverage\b[^>]*\bline-rate=\"([0-9]*\.?[0-9]+)\"")


def _git_capture(directory: Path, arguments: list[str]) -> bytes:
    """Run Git for byte-significant output; unlike `_git_output` nothing here is stripped."""
    try:
        completed = run(["git", "-C", str(directory), *arguments], check=True, capture_output=True)
    except CalledProcessError as exc:
        message = (exc.stderr or b"").decode("utf-8", "replace").strip() or "unknown git failure"
        raise RuntimeError(f"git {' '.join(arguments[:2])} failed: {message}") from exc
    return completed.stdout


def _gate_timeout() -> int:
    """Bound every operator-configured command so one gate cannot hang the stage."""
    return _positive_integer(
        os.environ.get("TEST_GATE_TIMEOUT_SECONDS", "1800"), "TEST_GATE_TIMEOUT_SECONDS"
    )


def _truncated(text: str) -> str:
    """Keep the informative tail of gate output and drop the rest so reports stay readable."""
    if len(text) <= _OUTPUT_LIMIT:
        return text
    return "...truncated...\n" + text[-_OUTPUT_LIMIT:]


def _worktree_path(repository: str, run_id: str) -> Path:
    """Contain the disposable worktree in the repository mount by flattening the slug itself."""
    root = Path(os.environ.get("ENGINEER_REPOSITORY_ROOT", "/projects"))
    if not root.is_absolute():
        raise RuntimeError("ENGINEER_REPOSITORY_ROOT must be an absolute path")
    directory = re.sub(r"[^a-z0-9]+", "-", repository.lower()).strip("-")
    if not directory:
        raise RuntimeError(f"repository slug cannot name a worktree directory: {repository}")
    return root / _WORKTREE_DIRECTORY / directory / run_id


def _preflight_details(state_dir: Path, item_id: str) -> dict[str, Any]:
    """Recover work item and acceptance criteria the branch execution report does not copy."""
    try:
        preflight = json.loads(
            (state_dir / "latest-engineer-preflight.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(preflight, dict) or preflight.get("status") != "ready_for_coding":
        return {}
    work_item = preflight.get("work_item")
    if not isinstance(work_item, dict) or work_item.get("id") != item_id:
        return {}
    return preflight


def _pull_request_number(url: object, repository: str) -> str:
    """Bind the pull request to the configured slug before its number reaches a Git refspec."""
    if not isinstance(url, str):
        raise RuntimeError("engineer execution does not record a pull request URL")
    match = _PULL_REQUEST_URL.match(url.strip())
    if match is None:
        raise RuntimeError(f"engineer execution pull request URL is not supported: {url}")
    owner, name, number = match.groups()
    if f"{owner}/{name}".lower() != repository.lower():
        raise RuntimeError(
            f"pull request URL does not belong to the configured repository {repository}: {url}"
        )
    return number


def _checkout_pull_request(
    workspace: Path, worktree: Path, url: object, repository: str
) -> dict[str, Any]:
    """Test the pull request's own head commit, fetched without changing any local branch."""
    number = _pull_request_number(url, repository)
    _git_output(workspace, ["fetch", "--no-tags", "--force", "origin", f"pull/{number}/head"])
    head = _git_output(workspace, ["rev-parse", "FETCH_HEAD"])
    _git_output(workspace, ["worktree", "add", "--detach", str(worktree), head])
    return {"source": "existing_pull_request", "pull_request_url": url, "head_commit": head}


def _apply_pending(worktree: Path, diff: bytes) -> None:
    """Apply the engineer's diff byte-for-byte; trailing whitespace and base85 blocks are data."""
    with tempfile.NamedTemporaryFile(mode="wb", suffix=".patch") as patch_file:
        patch_file.write(diff)
        patch_file.flush()
        _git_output(worktree, ["apply", "--whitespace=nowarn", patch_file.name])


def _worktree_destination(worktree: Path, relative: str) -> Path:
    """Keep every copied path inside the disposable worktree, symlinked parents included."""
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or ".git" in path.parts:
        raise RuntimeError(f"untracked path is unsafe to copy: {relative}")
    destination = worktree / path
    if not destination.resolve().is_relative_to(worktree.resolve()):
        raise RuntimeError(f"untracked path resolves outside the worktree: {relative}")
    return destination


def _copy_untracked(workspace: Path, worktree: Path) -> tuple[list[str], list[str]]:
    """Carry new files into the worktree; `git diff HEAD` reports only tracked paths."""
    listed = _git_capture(workspace, ["ls-files", "--others", "--exclude-standard", "-z"])
    entries = listed.split(b"\0")
    if entries and entries[-1] == b"":
        entries.pop()
    copied: list[str] = []
    skipped: list[str] = []
    for raw in entries:
        # NUL-delimited output is byte-exact, so leading and trailing spaces are part of the name.
        entry = os.fsdecode(raw)
        source = workspace / entry
        # Symlinks are not copied: their target is resolved by the reader, not by this stage.
        if source.is_symlink() or not source.is_file():
            skipped.append(entry)
            continue
        destination = _worktree_destination(worktree, entry)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        copied.append(entry)
    return copied, skipped


def _checkout_branch(workspace: Path, worktree: Path, branch: object) -> dict[str, Any]:
    """Test the engineer branch including the patch set that stage deliberately left uncommitted."""
    if not isinstance(branch, str) or not branch:
        raise RuntimeError("engineer execution does not record a branch")
    current = _git_output(workspace, ["branch", "--show-current"])
    if current != branch:
        raise RuntimeError(
            f"configured checkout must still be on engineer branch {branch}, "
            f"found {current or 'DETACHED'}"
        )
    head = _git_output(workspace, ["rev-parse", "HEAD"])
    _git_output(workspace, ["worktree", "add", "--detach", str(worktree), head])
    pending = _git_capture(workspace, ["diff", "--binary", "HEAD"])
    if pending:
        _apply_pending(worktree, pending)
    copied, skipped = _copy_untracked(workspace, worktree)
    return {
        "source": "engineer_branch",
        "branch": branch,
        "head_commit": head,
        "uncommitted_changes_applied": bool(pending),
        "untracked_files_copied": copied,
        "untracked_paths_skipped": skipped,
    }


def _checkout(
    workspace: Path, worktree: Path, execution: dict[str, Any], repository: str
) -> dict[str, Any]:
    """Route the two upstream shapes already accepted by the caller's status guard."""
    if execution["status"] == "existing_pull_request_ready_for_testing":
        return _checkout_pull_request(
            workspace, worktree, execution.get("pull_request_url"), repository
        )
    return _checkout_branch(workspace, worktree, execution.get("branch"))


def _gate_environment() -> dict[str, str]:
    """Never expose repository write tokens to operator-configured repository commands."""
    environment = os.environ.copy()
    for name in _REDACTED_ENVIRONMENT:
        environment.pop(name, None)
    return environment


def _run_gate(
    name: str, command: object, worktree: Path, timeout: int
) -> tuple[dict[str, Any], str]:
    """Run one configured gate with no shell, returning its record and untruncated output."""
    if not isinstance(command, str) or not command.strip():
        return {"gate": name, "status": "failed", "error": f"{name} gate is not a command"}, ""
    result: dict[str, Any] = {"gate": name, "command": command}
    try:
        arguments = shlex.split(command)
    except ValueError as exc:
        result.update(
            {"status": "failed", "error": f"{name} gate is not a parsable command: {exc}"}
        )
        return result, ""
    try:
        completed = run(
            arguments,
            cwd=str(worktree),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=_gate_environment(),
        )
    except TimeoutExpired:
        result.update({"status": "failed", "error": f"{name} gate exceeded {timeout} seconds"})
        return result, ""
    except OSError as exc:
        result.update({"status": "failed", "error": str(exc)})
        return result, ""
    result.update(
        {
            "exit_code": completed.returncode,
            "status": "passed" if completed.returncode == 0 else "failed",
            "stdout": _truncated(completed.stdout),
            "stderr": _truncated(completed.stderr),
        }
    )
    return result, completed.stdout + completed.stderr


def _coverage_from_file(path: Path) -> float | None:
    """Read a total from a coverage report file; an absent or unreadable file is not a total."""
    try:
        if path.stat().st_size > _COVERAGE_FILE_LIMIT:
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if path.suffix == ".json":
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return None
        totals = payload.get("totals") if isinstance(payload, dict) else None
        percent = totals.get("percent_covered") if isinstance(totals, dict) else None
        if isinstance(percent, bool) or not isinstance(percent, int | float):
            return None
        return float(percent)
    # Cobertura's line-rate is read with a narrow pattern rather than an XML parser, so a
    # repository-authored report cannot reach entity expansion or external-entity handling.
    match = _COVERAGE_LINE_RATE.search(text)
    return round(float(match.group(1)) * 100, 2) if match else None


def _parse_coverage(output: str) -> list[float]:
    """Collect distinct anchored TOTAL percentages; the caller refuses to choose between them."""
    return sorted({float(value) for value in _COVERAGE_TOTAL_LINE.findall(output)})


def _record_coverage(result: dict[str, Any], output: str, worktree: Path, minimum: object) -> None:
    """Prefer a machine-readable coverage report over the gate's own stdout, and never guess.

    The report file is still produced under repository-controlled configuration, so this narrows
    the surface a pull request can use to fake a total; it does not eliminate it.
    """
    percent: float | None = None
    source = "unavailable"
    for name in _COVERAGE_ARTIFACTS:
        percent = _coverage_from_file(worktree / name)
        if percent is not None:
            source = name
            break
    conflicting: list[float] = []
    if percent is None:
        conflicting = _parse_coverage(output)
        if len(conflicting) == 1:
            percent, source = conflicting[0], "gate_output"
    result["coverage_percent"] = percent
    result["coverage_source"] = source
    if len(conflicting) > 1:
        result["status"] = "failed"
        result["error"] = f"coverage gate reported conflicting totals: {conflicting}"
        return
    if isinstance(minimum, bool) or not isinstance(minimum, int | float):
        return
    result["minimum_coverage"] = minimum
    if percent is None:
        result["status"] = "failed"
        result["error"] = "coverage gate did not report a readable total percentage"
    elif percent < float(minimum):
        result["status"] = "failed"
        result["error"] = f"coverage {percent}% is below the configured minimum {minimum}%"


def _run_gates(gates: dict[str, Any], worktree: Path) -> list[dict[str, Any]]:
    """Run only configured gates and record every unconfigured gate as an explicit skip."""
    timeout = _gate_timeout()
    minimum = gates.get("minimum_coverage")
    results: list[dict[str, Any]] = []
    for name in _GATE_ORDER:
        if name not in gates:
            results.append({"gate": name, "status": "skipped", "reason": "not configured"})
            continue
        result, output = _run_gate(name, gates[name], worktree, timeout)
        if name == "coverage":
            _record_coverage(result, output, worktree, minimum)
        results.append(result)
    return results


def _overall_status(results: list[dict[str, Any]]) -> str:
    """Reserve `passed` for a successful CI-equivalent aggregate check."""
    if any(gate["status"] == "failed" for gate in results):
        return "failed"
    check = next((gate for gate in results if gate["gate"] == "check"), None)
    return "passed" if check and check["status"] == "passed" else "passed_partial"


def _remove_worktree(workspace: Path, worktree: Path) -> bool:
    """Remove the disposable worktree unconditionally without masking the gate results."""
    for arguments in (["worktree", "remove", "--force", str(worktree)], ["worktree", "prune"]):
        # Git's exit status is not the authority here: the filesystem check below decides, and the
        # rmtree fallback plus the returned bool report the real outcome to the operator.
        with contextlib.suppress(OSError, RuntimeError):
            _git_output(workspace, arguments)
    if worktree.exists():
        shutil.rmtree(worktree, ignore_errors=True)
    return not worktree.exists()


def _test_markdown(report: dict[str, Any]) -> str:
    """Render the evidence a reviewer needs without repeating whole command transcripts."""
    lines = [
        "# Test execution report",
        "",
        f"- Status: **{report['status']}**",
        (
            "- Mode: disposable worktree only; nothing committed, pushed, published, merged, "
            "or dismissed."
        ),
        f"- Engineer execution: `{report['execution_path']}`",
        f"- Requested item: `{report['requested_item_id']}`",
    ]
    if report.get("error"):
        lines.extend(["", "## Blocker", "", str(report["error"])])
        return "\n".join(lines) + "\n"

    checkout = report.get("checkout", {})
    lines.extend(
        [
            f"- Source: `{checkout.get('source', 'unknown')}`",
            f"- Head commit: `{checkout.get('head_commit', 'unknown')}`",
            f"- Worktree removed: {report.get('worktree_removed')}",
        ]
    )
    if report.get("worktree_removed") is False:
        lines.append(
            "- **The disposable worktree could not be removed.** It still holds the code under "
            f"test at `{report.get('worktree_path')}` and needs operator cleanup."
        )
    if checkout.get("untracked_files_copied"):
        lines.append(
            "- New files tested: "
            + ", ".join(f"`{path}`" for path in checkout["untracked_files_copied"])
        )
    if checkout.get("untracked_paths_skipped"):
        lines.append(
            "- Untracked paths not copied: "
            + ", ".join(f"`{path}`" for path in checkout["untracked_paths_skipped"])
        )
    lines.extend(["", "## Gates", ""])
    for gate in report.get("gates", []):
        detail = gate.get("error") or f"exit code {gate.get('exit_code')}"
        if gate["status"] == "skipped":
            detail = "not configured in quality_gates"
        if gate.get("coverage_source"):
            detail += f" (total read from {gate['coverage_source']})"
        lines.append(f"- `{gate['gate']}`: **{gate['status']}** — {detail}")
    criteria = report.get("architect_decision", {}).get("acceptance_criteria", [])
    if criteria:
        lines.extend(["", "## Acceptance criteria", ""])
        lines.extend(f"- {criterion}" for criterion in criteria)
    lines.append("")
    return "\n".join(lines)


def _persist_report(report: dict[str, Any], state_dir: Path, timestamp: datetime) -> None:
    """Write the artifact on every exit path; a stale `latest-*` pointer would be read as fresh."""
    if report["status"] == "blocked" and "error" not in report:
        report["error"] = "the test stage was interrupted before it completed"
    report["completed_at"] = datetime.now(UTC).isoformat()
    run_dir = state_dir / "runs" / timestamp.strftime("%Y%m%dT%H%M%SZ")
    run_dir.mkdir(parents=True, exist_ok=True)
    report_path = run_dir / "test-report.json"
    markdown_path = run_dir / "test-report.md"
    report["report_path"] = str(report_path)
    report["markdown_path"] = str(markdown_path)
    rendered = _test_markdown(report)
    _write_json_atomic(report_path, report)
    markdown_path.write_text(rendered, encoding="utf-8")
    _write_json_atomic(state_dir / "latest-test-report.json", report)
    (state_dir / "latest-test-report.md").write_text(rendered, encoding="utf-8")
    print(
        json.dumps(
            {
                "event": "test_execution_completed",
                "report": str(report_path),
                "status": report["status"],
            }
        )
    )


def run_test_execute(item_id: str) -> int:
    """Run configured quality gates in a disposable worktree; never publish or change the repo."""
    timestamp = datetime.now(UTC)
    state_dir = Path(os.environ["AGENT_STATE_DIR"])
    execution_path = state_dir / "latest-engineer-execution.json"
    report: dict[str, Any] = {
        "kind": "test_agent_execution",
        "started_at": timestamp.isoformat(),
        "mode": "disposable_worktree_no_publish",
        "requested_item_id": item_id,
        "execution_path": str(execution_path),
        "status": "blocked",
    }
    try:
        _execute_gates(report, item_id, state_dir, execution_path, timestamp)
    # json.JSONDecodeError is a ValueError, as is an unbalanced quote in a configured gate.
    except (OSError, RuntimeError, ValueError) as exc:
        report["error"] = str(exc)
    finally:
        # Unconditional: an interrupt must not leave the previous run's pointer standing.
        _persist_report(report, state_dir, timestamp)
    return 1 if report["status"] == "blocked" else 0


def _execute_gates(
    report: dict[str, Any],
    item_id: str,
    state_dir: Path,
    execution_path: Path,
    timestamp: datetime,
) -> None:
    """Fill in the verdict; the caller owns error capture and writing the artifact."""
    execution = json.loads(execution_path.read_text(encoding="utf-8"))
    if not isinstance(execution, dict):
        raise RuntimeError("latest engineer execution must be a JSON object")
    if execution.get("requested_item_id") != item_id:
        raise RuntimeError("latest engineer execution does not match the requested work item")
    if execution.get("status") not in _TESTABLE_STATUSES:
        raise RuntimeError(
            f"latest engineer execution is not ready for testing: {execution.get('status')}"
        )
    repository = execution.get("repository")
    if not isinstance(repository, str):
        raise RuntimeError("latest engineer execution does not name a repository")
    metadata = _load_repository_info().get(repository)
    if metadata is None:
        raise RuntimeError(f"repository is not configured for execution: {repository}")
    workspace = _workspace_path(repository, metadata)
    if str(workspace) != execution.get("workspace_path"):
        raise RuntimeError("engineer execution workspace does not match repository metadata")
    gates = metadata.get("quality_gates")
    if not isinstance(gates, dict) or "test" not in gates:
        raise RuntimeError(f"repository metadata must configure a test quality gate: {repository}")

    preflight = _preflight_details(state_dir, item_id)
    work_item = execution.get("work_item")
    if not isinstance(work_item, dict) or work_item.get("id") != item_id:
        work_item = preflight.get("work_item", {})
    decision = execution.get("architect_decision")
    if not isinstance(decision, dict):
        decision = preflight.get("architect_decision", {})

    worktree = _worktree_path(repository, timestamp.strftime("%Y%m%dT%H%M%SZ"))
    report.update(
        {
            "repository": repository,
            "workspace_path": str(workspace),
            "worktree_path": str(worktree),
            "work_item": work_item,
            "architect_decision": decision,
        }
    )
    worktree.parent.mkdir(parents=True, exist_ok=True)
    try:
        report["checkout"] = _checkout(workspace, worktree, execution, repository)
        results = _run_gates(gates, worktree)
    finally:
        report["worktree_removed"] = _remove_worktree(workspace, worktree)
    report["gates"] = results
    report["status"] = _overall_status(results)
