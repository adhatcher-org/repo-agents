"""Prepare one approved maintenance item for a later write-enabled engineer stage."""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from subprocess import CalledProcessError, run
from typing import Any

import yaml

from repo_agent.planning import _agent_configuration, _ollama_json


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
        report["work_item"] = work_item
        report["repository_metadata"] = metadata
        report["architect_decision"] = handoff.get("architect_decision")
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


def _git_output(workspace: Path, arguments: list[str]) -> str:
    """Run a fixed Git command and return its text without using a shell."""
    try:
        completed = run(
            ["git", "-C", str(workspace), *arguments],
            check=True,
            capture_output=True,
            text=True,
        )
    except CalledProcessError as exc:
        message = exc.stderr.strip() or exc.stdout.strip() or "unknown git failure"
        raise RuntimeError(f"git {' '.join(arguments[:2])} failed: {message}") from exc
    return completed.stdout.strip()


def _default_base(workspace: Path, metadata: dict[str, Any]) -> str:
    """Require a clean checkout exactly at its locally tracked default branch."""
    default_branch = metadata.get("default_branch")
    if not isinstance(default_branch, str) or not default_branch:
        raise RuntimeError("repository metadata requires a default_branch for execution")
    if _git_output(workspace, ["status", "--porcelain"]):
        raise RuntimeError(f"configured repository checkout has uncommitted changes: {workspace}")
    current_branch = _git_output(workspace, ["branch", "--show-current"])
    if current_branch != default_branch:
        current_display = current_branch or "DETACHED"
        raise RuntimeError(
            f"repository checkout must be on default branch {default_branch}, "
            f"found {current_display}"
        )
    head = _git_output(workspace, ["rev-parse", "HEAD"])
    tracked = _git_output(workspace, ["rev-parse", f"refs/remotes/origin/{default_branch}"])
    if head != tracked:
        raise RuntimeError(
            f"repository checkout is not current with origin/{default_branch}; "
            "update it before execution"
        )
    return head


def _branch_name(item_id: str, timestamp: datetime) -> str:
    """Make a fixed, Git-safe branch name that cannot be chosen by the model."""
    normalized = re.sub(r"[^a-z0-9]+", "-", item_id.lower()).strip("-")
    return f"repo-agent/engineer-{timestamp.strftime('%Y%m%d%H%M%S')}-{normalized[:48]}"


def _safe_relative_path(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError("engineer response patch paths must be non-empty strings")
    path = Path(value)
    if path == Path(".") or path.is_absolute() or ".." in path.parts or ".git" in path.parts:
        raise RuntimeError(f"engineer response has an unsafe patch path: {value}")
    return path.as_posix()


def _validate_engineer_response(response: dict[str, Any]) -> dict[str, Any]:
    """Accept only the narrowly-defined JSON contract used by the patch applier."""
    required = {
        "implementation_summary",
        "files_to_change",
        "architecture_documents_to_update",
        "test_strategy",
        "risks",
        "patches",
    }
    if set(response) != required:
        raise RuntimeError("engineer response must contain exactly the required execution fields")
    if not isinstance(response["implementation_summary"], str):
        raise RuntimeError("engineer response requires implementation_summary")
    for field in ("files_to_change", "architecture_documents_to_update", "test_strategy", "risks"):
        if not isinstance(response[field], list) or not all(
            isinstance(value, str) for value in response[field]
        ):
            raise RuntimeError(f"engineer response requires {field} to be a string list")
    patches = response["patches"]
    if not isinstance(patches, list) or not patches:
        raise RuntimeError("engineer response requires at least one patch")
    seen_paths: set[str] = set()
    for patch in patches:
        if not isinstance(patch, dict) or set(patch) != {"path", "diff"}:
            raise RuntimeError("every engineer patch requires exactly path and diff")
        path = _safe_relative_path(patch["path"])
        if path in seen_paths:
            raise RuntimeError(f"engineer response has duplicate patch path: {path}")
        seen_paths.add(path)
        if not isinstance(patch["diff"], str) or not patch["diff"].startswith("diff --git "):
            raise RuntimeError(f"engineer response patch must be a Git diff: {path}")
        header = f"diff --git a/{path} b/{path}"
        lines = patch["diff"].splitlines()
        if not lines or lines[0] != header:
            raise RuntimeError(f"engineer response patch path does not match diff header: {path}")
        if sum(line.startswith("diff --git ") for line in lines) != 1:
            raise RuntimeError(f"engineer response patch must modify exactly one path: {path}")
        if any(
            line.startswith(("rename from ", "rename to ", "copy from ", "copy to "))
            for line in lines
        ):
            raise RuntimeError(f"engineer response patch may not rename or copy paths: {path}")
    file_paths = {_safe_relative_path(path) for path in response["files_to_change"]}
    if file_paths != seen_paths:
        raise RuntimeError("engineer response files_to_change must match patch paths exactly")
    return response


def _repository_context(workspace: Path, metadata: dict[str, Any]) -> dict[str, Any]:
    """Supply a bounded, read-only repository view to the model; never credentials or Git config."""
    files = _git_output(workspace, ["ls-files"]).splitlines()
    if len(files) > 1_000:
        files = files[:1_000]
    selected = [
        path
        for path in files
        if Path(path).name in {"README.md", "pyproject.toml", "package.json", "Makefile"}
        or path in metadata.get("architecture_docs", [])
    ]
    excerpts: dict[str, str] = {}
    remaining = 24_000
    for relative in selected:
        candidate = workspace / relative
        if not candidate.is_file() or candidate.is_symlink() or candidate.stat().st_size > 12_000:
            continue
        try:
            contents = candidate.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        excerpt = contents[: min(len(contents), remaining)]
        if excerpt:
            excerpts[relative] = excerpt
            remaining -= len(excerpt)
        if remaining <= 0:
            break
    return {"tracked_files": files, "selected_file_contents": excerpts}


def _apply_patches(workspace: Path, patches: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Check the complete model patch set before applying it as one Git operation."""
    applied = []
    rendered: list[str] = []
    for patch in patches:
        path = _safe_relative_path(patch["path"])
        diff = patch["diff"]
        assert isinstance(diff, str)
        rendered.append(diff)
        applied.append({"path": path, "sha256": sha256(diff.encode()).hexdigest()})
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".patch") as patch_file:
        patch_file.write("\n".join(rendered))
        patch_file.flush()
        for arguments in (
            ["apply", "--check", "--whitespace=error", patch_file.name],
            ["apply", "--whitespace=error", patch_file.name],
        ):
            try:
                run(
                    ["git", "-C", str(workspace), *arguments],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            except CalledProcessError as exc:
                message = exc.stderr.strip() or exc.stdout.strip() or "patch rejected"
                raise RuntimeError(f"engineer patch set could not be applied: {message}") from exc
    return applied


def _load_ready_preflight(
    item_id: str, state_dir: Path
) -> tuple[dict[str, Any], Path, dict[str, Any]]:
    preflight_path = state_dir / "latest-engineer-preflight.json"
    preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
    if not isinstance(preflight, dict) or preflight.get("status") != "ready_for_coding":
        raise RuntimeError("latest engineer preflight must be ready_for_coding")
    work_item = preflight.get("work_item")
    metadata = preflight.get("repository_metadata")
    if not isinstance(work_item, dict) or work_item.get("id") != item_id:
        raise RuntimeError("latest engineer preflight does not match the requested work item")
    if not isinstance(metadata, dict):
        raise RuntimeError("latest engineer preflight does not contain repository metadata")
    workspace = _workspace_path(str(work_item.get("repository", "")), metadata)
    if str(workspace) != preflight.get("workspace_path"):
        raise RuntimeError("latest engineer preflight workspace does not match repository metadata")
    return preflight, workspace, metadata


def run_engineer_execute(item_id: str) -> int:
    """Create one branch and apply validated model patches; never test, commit, or publish."""
    timestamp = datetime.now(UTC)
    state_dir = Path(os.environ["AGENT_STATE_DIR"])
    report: dict[str, Any] = {
        "kind": "senior_software_engineer_execution",
        "started_at": timestamp.isoformat(),
        "mode": "write_enabled_no_test_no_publish",
        "requested_item_id": item_id,
        "preflight_path": str(state_dir / "latest-engineer-preflight.json"),
        "status": "blocked",
    }
    try:
        preflight, workspace, metadata = _load_ready_preflight(item_id, state_dir)
        base_commit = _default_base(workspace, metadata)
        branch = _branch_name(item_id, timestamp)
        _git_output(workspace, ["switch", "-c", branch])
        report.update(
            {
                "repository": preflight["work_item"].get("repository"),
                "workspace_path": str(workspace),
                "base_commit": base_commit,
                "branch": branch,
            }
        )
        agent = _agent_configuration("senior_software_engineer")
        payload = {
            "work_item": preflight["work_item"],
            "architect_decision": preflight.get("architect_decision"),
            "repository_metadata": {
                "architecture_docs": metadata.get("architecture_docs", []),
                "quality_gates": metadata.get("quality_gates", {}),
            },
            "repository_context": _repository_context(workspace, metadata),
            "constraints": {
                "create_or_update_only_patch_paths": True,
                "do_not_run_commands": True,
                "do_not_commit_or_publish": True,
            },
        }
        response = _validate_engineer_response(
            _ollama_json(
                agent["model"],
                agent["instructions"],
                payload,
                temperature=agent["temperature"],
                timeout=agent["timeout_seconds"],
            )
        )
        patches = response["patches"]
        assert isinstance(patches, list)
        applied = _apply_patches(workspace, patches)
        report.update(
            {
                "status": "implementation_applied",
                "engineer_agent": {
                    "id": "senior_software_engineer",
                    "definition": agent["definition"],
                    "model": agent["model"],
                    "temperature": agent["temperature"],
                },
                "implementation_summary": response["implementation_summary"],
                "files_to_change": response["files_to_change"],
                "architecture_documents_to_update": response["architecture_documents_to_update"],
                "test_strategy": response["test_strategy"],
                "risks": response["risks"],
                "applied_patches": applied,
            }
        )
    except (OSError, RuntimeError, json.JSONDecodeError) as exc:
        report["error"] = str(exc)

    report["completed_at"] = datetime.now(UTC).isoformat()
    run_dir = state_dir / "runs" / timestamp.strftime("%Y%m%dT%H%M%SZ")
    run_dir.mkdir(parents=True, exist_ok=True)
    report_path = run_dir / "engineer-execution.json"
    report["report_path"] = str(report_path)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (state_dir / "latest-engineer-execution.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "event": "engineer_execution_completed",
                "report": str(report_path),
                "status": report["status"],
            }
        )
    )
    return 1 if report["status"] == "blocked" else 0
