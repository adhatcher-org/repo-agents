"""Verify one engineer branch or existing pull request in a disposable, discarded worktree."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from subprocess import TimeoutExpired, run
from typing import Any

from repo_agent.engineering import (
    _git_output,
    _load_repository_info,
    _workspace_path,
    _write_json_atomic,
)
from repo_agent.planning import _positive_integer

_GATE_ORDER = ("bootstrap", "format", "lint", "test", "coverage", "security")
_TESTABLE_STATUSES = frozenset(
    {"existing_pull_request_ready_for_testing", "implementation_applied"}
)
_OUTPUT_LIMIT = 4_000
_WORKTREE_DIRECTORY = ".repo-agent-worktrees"
_REDACTED_ENVIRONMENT = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN")
_PULL_REQUEST_URL = re.compile(r"^https?://[^/\s]+/[^/\s]+/[^/\s]+/pull/(\d+)/?$")
_COVERAGE_PATTERNS = (
    re.compile(r"total coverage:\s*([0-9]+(?:\.[0-9]+)?)\s*%", re.IGNORECASE),
    re.compile(r"^TOTAL\b.*?([0-9]+(?:\.[0-9]+)?)%", re.MULTILINE),
    re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*%"),
)


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
    if not isinstance(preflight, dict):
        return {}
    work_item = preflight.get("work_item")
    if not isinstance(work_item, dict) or work_item.get("id") != item_id:
        return {}
    return preflight


def _pull_request_number(url: object) -> str:
    """Accept only a literal GitHub pull-request URL before it reaches a Git refspec."""
    if not isinstance(url, str):
        raise RuntimeError("engineer execution does not record a pull request URL")
    match = _PULL_REQUEST_URL.match(url.strip())
    if match is None:
        raise RuntimeError(f"engineer execution pull request URL is not supported: {url}")
    return match.group(1)


def _checkout_pull_request(workspace: Path, worktree: Path, url: object) -> dict[str, Any]:
    """Test the pull request's own head commit, fetched without changing any local branch."""
    number = _pull_request_number(url)
    _git_output(workspace, ["fetch", "--no-tags", "--force", "origin", f"pull/{number}/head"])
    head = _git_output(workspace, ["rev-parse", "FETCH_HEAD"])
    _git_output(workspace, ["worktree", "add", "--detach", str(worktree), head])
    return {"source": "existing_pull_request", "pull_request_url": url, "head_commit": head}


def _apply_pending(worktree: Path, diff: str) -> None:
    """Reproduce the engineer's changes in the disposable worktree only."""
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".patch") as patch_file:
        patch_file.write(diff + "\n")
        patch_file.flush()
        _git_output(worktree, ["apply", "--whitespace=nowarn", patch_file.name])


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
    pending = _git_output(workspace, ["diff", "HEAD"])
    if pending:
        _apply_pending(worktree, pending)
    return {
        "source": "engineer_branch",
        "branch": branch,
        "head_commit": head,
        "uncommitted_changes_applied": bool(pending),
    }


def _checkout(workspace: Path, worktree: Path, execution: dict[str, Any]) -> dict[str, Any]:
    """Route the two upstream shapes already accepted by the caller's status guard."""
    if execution["status"] == "existing_pull_request_ready_for_testing":
        return _checkout_pull_request(workspace, worktree, execution.get("pull_request_url"))
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
        completed = run(
            shlex.split(command),
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


def _parse_coverage(output: str) -> float | None:
    """Read a total coverage percentage from gate output without trusting its exact format."""
    for pattern in _COVERAGE_PATTERNS:
        matches = pattern.findall(output)
        if matches:
            return float(matches[-1])
    return None


def _record_coverage(result: dict[str, Any], output: str, minimum: object) -> None:
    """Treat an unmet or unreadable coverage minimum as a failure rather than a silent pass."""
    percent = _parse_coverage(output)
    result["coverage_percent"] = percent
    if not isinstance(minimum, int | float) or isinstance(minimum, bool):
        return
    result["minimum_coverage"] = minimum
    if percent is None:
        result["status"] = "failed"
        result["error"] = "coverage gate output did not report a total percentage"
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
            _record_coverage(result, output, minimum)
        results.append(result)
    return results


def _remove_worktree(workspace: Path, worktree: Path) -> bool:
    """Remove the disposable worktree unconditionally without masking the gate results."""
    for arguments in (["worktree", "remove", "--force", str(worktree)], ["worktree", "prune"]):
        try:
            _git_output(workspace, arguments)
        except (OSError, RuntimeError):
            pass
    if worktree.exists():
        shutil.rmtree(worktree, ignore_errors=True)
    return not worktree.exists()


def _test_markdown(report: dict[str, Any]) -> str:
    """Render the evidence a reviewer needs without repeating whole command transcripts."""
    lines = [
        "# Test execution report",
        "",
        f"- Status: **{report['status']}**",
        "- Mode: disposable worktree only; nothing committed, pushed, published, merged, "
        "or dismissed.",
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
            "",
            "## Gates",
            "",
        ]
    )
    for gate in report.get("gates", []):
        detail = gate.get("error") or f"exit code {gate.get('exit_code')}"
        if gate["status"] == "skipped":
            detail = "not configured in quality_gates"
        lines.append(f"- `{gate['gate']}`: **{gate['status']}** — {detail}")
    criteria = report.get("architect_decision", {}).get("acceptance_criteria", [])
    if criteria:
        lines.extend(["", "## Acceptance criteria", ""])
        lines.extend(f"- {criterion}" for criterion in criteria)
    lines.append("")
    return "\n".join(lines)


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
        if not isinstance(gates, dict) or not any(name in gates for name in _GATE_ORDER):
            raise RuntimeError(f"repository metadata configures no quality gates: {repository}")

        preflight = _preflight_details(state_dir, item_id)
        work_item = execution.get("work_item")
        if not isinstance(work_item, dict) or work_item.get("id") != item_id:
            work_item = preflight.get("work_item", {})
        decision = execution.get("architect_decision")
        if not isinstance(decision, dict):
            decision = preflight.get("architect_decision", {})

        run_id = timestamp.strftime("%Y%m%dT%H%M%SZ")
        worktree = _worktree_path(repository, run_id)
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
            report["checkout"] = _checkout(workspace, worktree, execution)
            results = _run_gates(gates, worktree)
        finally:
            report["worktree_removed"] = _remove_worktree(workspace, worktree)
        report["gates"] = results
        report["status"] = (
            "failed" if any(gate["status"] == "failed" for gate in results) else "passed"
        )
    except (OSError, RuntimeError, json.JSONDecodeError) as exc:
        report["error"] = str(exc)

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
    return 1 if report["status"] == "blocked" else 0
