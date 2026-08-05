"""Prepare one approved maintenance item for a later write-enabled engineer stage."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from subprocess import CalledProcessError, run
from typing import Any

import yaml


def _repository_info_path() -> Path:
    return Path(os.environ.get("AGENT_REPOSITORY_INFO", "/config/repo-info.yml"))


def _workspace_path(repository: str, metadata: dict[str, Any]) -> Path:
    """Validate the configured checkout path is contained in the repository mount."""
    configured_path = metadata.get("path")
    if not isinstance(configured_path, str):
        raise RuntimeError(f"repository metadata requires a path: {repository}")
    root = Path(os.environ.get("ENGINEER_REPOSITORY_ROOT", "/projects"))
    if not root.is_absolute():
        raise RuntimeError("ENGINEER_REPOSITORY_ROOT must be an absolute path")
    workspace = Path(configured_path)
    try:
        workspace.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"repository checkout must be inside {root}: {configured_path}") from exc
    return workspace


def _load_repository_info() -> dict[str, dict[str, Any]]:
    """Load and minimally validate editable, per-repository execution metadata."""
    path = _repository_info_path()
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"repository metadata is missing: {path}") from exc
    except yaml.YAMLError as exc:
        raise RuntimeError(f"repository metadata is not valid YAML: {path}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("repositories"), list):
        raise RuntimeError("repository metadata requires a repositories list")

    repositories: dict[str, dict[str, Any]] = {}
    for entry in payload["repositories"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("slug"), str):
            raise RuntimeError("every repository metadata entry requires a slug")
        slug = entry["slug"]
        if slug in repositories:
            raise RuntimeError(f"repository metadata has duplicate slug: {slug}")
        repositories[slug] = entry
    return repositories


def _approved_item(item_id: str, plan: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    if plan.get("status") != "approved":
        raise RuntimeError("latest architect plan must have status approved")
    critic = plan.get("critic")
    if not isinstance(critic, dict) or critic.get("verdict") != "approved":
        raise RuntimeError("latest architect plan requires an approved critic verdict")
    architect_plan = plan.get("architect_plan")
    if not isinstance(architect_plan, dict) or not isinstance(architect_plan.get("items"), list):
        raise RuntimeError("latest architect plan does not contain architect items")
    work_items = plan.get("work_items")
    if not isinstance(work_items, list):
        raise RuntimeError("latest architect plan does not contain source work items")

    source = next(
        (item for item in work_items if isinstance(item, dict) and item.get("id") == item_id), None
    )
    decision = next(
        (
            item
            for item in architect_plan["items"]
            if isinstance(item, dict) and item.get("id") == item_id
        ),
        None,
    )
    if source is None or decision is None:
        raise RuntimeError(f"work item is not present in the latest approved plan: {item_id}")
    return source, decision


def _markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# Senior software engineer handoff",
        "",
        f"- Status: **{report['status']}**",
        "- Mode: read-only preparation (no repository, branch, PR, or alert was changed)",
        f"- Approved plan: `{report['plan_path']}`",
    ]
    if report.get("error"):
        lines.extend(["", "## Blocker", "", str(report["error"])])
        return "\n".join(lines) + "\n"

    source = report["work_item"]
    decision = report["architect_decision"]
    metadata = report["repository"]
    assert isinstance(source, dict) and isinstance(decision, dict) and isinstance(metadata, dict)
    lines.extend(
        [
            "",
            "## Assigned work item",
            "",
            f"- ID: `{source['id']}`",
            f"- Repository: `{source['repository']}`",
            f"- Type: `{source['kind']}`",
            f"- Title: {source['title']}",
            f"- Architect disposition: **{decision.get('disposition')}**",
            f"- Rationale: {decision.get('rationale', 'Not provided')}",
            "",
            "## Required acceptance criteria",
            "",
        ]
    )
    for criterion in decision.get("acceptance_criteria", []):
        lines.append(f"- {criterion}")
    lines.extend(["", "## Repository execution contract", ""])
    lines.extend(
        [
            f"- Workspace path: `{report['workspace_path']}`",
            f"- Default branch: `{metadata.get('default_branch', 'not configured')}`",
            "- Architecture documents: "
            + ", ".join(f"`{path}`" for path in metadata.get("architecture_docs", [])),
            "- Required gates are recorded in the JSON handoff artifact.",
            "",
            "## Next controlled action",
            "",
            (
                "Provide an explicitly write-enabled, isolated workspace for this repository. "
                "Then run the future implementation command for this exact handoff. The engineer "
                "must update listed architecture documents when the approved plan requires it and "
                "hand the result to the test agent before any PR-review stage."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def run_engineer_handoff(item_id: str) -> int:
    """Persist a single, approved work-item contract without accessing its repository."""
    timestamp = datetime.now(UTC)
    state_dir = Path(os.environ["AGENT_STATE_DIR"])
    plan_path = state_dir / "latest-architect-plan.json"
    report: dict[str, Any] = {
        "kind": "senior_software_engineer_handoff",
        "started_at": timestamp.isoformat(),
        "mode": "read_only",
        "plan_path": str(plan_path),
        "requested_item_id": item_id,
        "status": "blocked",
    }
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        if not isinstance(plan, dict):
            raise RuntimeError("latest architect plan must be a JSON object")
        source, decision = _approved_item(item_id, plan)
        repository = source.get("repository")
        if not isinstance(repository, str):
            raise RuntimeError("selected work item does not name a repository")
        metadata = _load_repository_info().get(repository)
        if metadata is None:
            raise RuntimeError(
                f"selected repository is not configured for execution: {repository}. "
                f"Add it to {_repository_info_path()} before enabling implementation."
            )
        report["work_item"] = source
        report["architect_decision"] = decision
        report["repository"] = metadata
        report["workspace_path"] = str(_workspace_path(repository, metadata))
        report["engineer_agent"] = {"id": "senior_software_engineer"}
        report["status"] = "ready_for_implementation"
    except (OSError, RuntimeError, json.JSONDecodeError) as exc:
        report["error"] = str(exc)

    report["completed_at"] = datetime.now(UTC).isoformat()
    run_dir = state_dir / "runs" / timestamp.strftime("%Y%m%dT%H%M%SZ")
    run_dir.mkdir(parents=True, exist_ok=True)
    report_path = run_dir / "engineer-handoff.json"
    markdown_path = run_dir / "engineer-handoff.md"
    report["report_path"] = str(report_path)
    report["markdown_path"] = str(markdown_path)
    rendered = _markdown_report(report)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown_path.write_text(rendered, encoding="utf-8")
    (state_dir / "latest-engineer-handoff.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (state_dir / "latest-engineer-handoff.md").write_text(rendered, encoding="utf-8")
    print(
        json.dumps(
            {
                "event": "engineer_handoff_completed",
                "report": str(report_path),
                "status": report["status"],
            }
        )
    )
    return 1 if report["status"] == "blocked" else 0


def _prepare_workspace(repository: str, metadata: dict[str, Any]) -> Path:
    """Verify the configured checkout is clean before a later branch-changing stage."""
    workspace = _workspace_path(repository, metadata)
    if not (workspace / ".git").is_dir():
        raise RuntimeError(f"configured repository checkout is unavailable: {workspace}")
    try:
        status = run(
            ["git", "-C", str(workspace), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        )
    except CalledProcessError as exc:
        message = exc.stderr.strip() or exc.stdout.strip() or "unknown git status failure"
        raise RuntimeError(f"failed to inspect repository checkout: {message}") from exc
    if status.stdout.strip():
        raise RuntimeError(f"configured repository checkout has uncommitted changes: {workspace}")
    return workspace


def run_engineer_preflight(item_id: str) -> int:
    """Prepare one isolated checkout, without creating a branch or modifying application code."""
    timestamp = datetime.now(UTC)
    state_dir = Path(os.environ["AGENT_STATE_DIR"])
    handoff_path = state_dir / "latest-engineer-handoff.json"
    report: dict[str, Any] = {
        "kind": "senior_software_engineer_preflight",
        "started_at": timestamp.isoformat(),
        "mode": "isolated_checkout_only",
        "requested_item_id": item_id,
        "handoff_path": str(handoff_path),
        "status": "blocked",
    }
    try:
        handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
        if not isinstance(handoff, dict) or handoff.get("status") != "ready_for_implementation":
            raise RuntimeError("latest engineer handoff must be ready_for_implementation")
        work_item = handoff.get("work_item")
        if not isinstance(work_item, dict) or work_item.get("id") != item_id:
            raise RuntimeError("latest engineer handoff does not match the requested work item")
        repository = work_item.get("repository")
        if not isinstance(repository, str):
            raise RuntimeError("latest engineer handoff does not name a repository")
        metadata = handoff.get("repository")
        if not isinstance(metadata, dict):
            raise RuntimeError("latest engineer handoff does not contain repository metadata")
        workspace = _prepare_workspace(repository, metadata)
        report["repository"] = repository
        report["workspace_path"] = str(workspace)
        report["status"] = "ready_for_coding"
    except (OSError, RuntimeError, json.JSONDecodeError) as exc:
        report["error"] = str(exc)

    report["completed_at"] = datetime.now(UTC).isoformat()
    run_dir = state_dir / "runs" / timestamp.strftime("%Y%m%dT%H%M%SZ")
    run_dir.mkdir(parents=True, exist_ok=True)
    report_path = run_dir / "engineer-preflight.json"
    report["report_path"] = str(report_path)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (state_dir / "latest-engineer-preflight.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "event": "engineer_preflight_completed",
                "report": str(report_path),
                "status": report["status"],
            }
        )
    )
    return 1 if report["status"] == "blocked" else 0
