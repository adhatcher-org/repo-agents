"""Small, dependency-free health CLI for the initial container image."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from subprocess import CalledProcessError, run
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

from repo_agent import __version__
from repo_agent.engineering import (
    run_engineer_execute,
    run_engineer_handoff,
    run_engineer_preflight,
)
from repo_agent.planning import run_planning


def _ollama_tags_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/api/tags"


def _agent_definitions() -> list[dict[str, str]]:
    """Read the individually versioned agent definitions bundled into the image."""
    definitions_dir = Path(os.environ.get("AGENT_DEFINITIONS_DIR", "/app/agents"))
    definitions: list[dict[str, str]] = []
    for definition_path in sorted(definitions_dir.glob("*.md")):
        metadata: dict[str, str] = {}
        try:
            lines = definition_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        if lines[:1] == ["---"]:
            for line in lines[1:]:
                if line == "---":
                    break
                key, separator, value = line.partition(":")
                if separator:
                    metadata[key.strip()] = value.strip()
        definitions.append(
            {
                "id": metadata.get("id", definition_path.stem),
                "status": metadata.get("status", "unknown"),
                "execution": metadata.get("execution", "unknown"),
                "provider": metadata.get("provider", "none"),
                "model": metadata.get("model", "none"),
                "definition": str(definition_path),
            }
        )
    return definitions


def health() -> int:
    """Check mounted state/config paths and Ollama reachability without secrets."""
    state_dir = Path(os.environ["AGENT_STATE_DIR"])
    config_path = Path(os.environ["AGENT_CONFIG"])
    tags_url = _ollama_tags_url(os.environ["OLLAMA_BASE_URL"])
    result: dict[str, object] = {
        "version": __version__,
        "state_dir": str(state_dir),
        "config_path": str(config_path),
        "ollama_tags_url": tags_url,
        "checks": {},
    }
    checks = result["checks"]
    assert isinstance(checks, dict)

    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=state_dir, prefix=".health-", delete=True):
            pass
        checks["state_writable"] = True
    except OSError as exc:
        checks["state_writable"] = False
        checks["state_error"] = str(exc)

    checks["config_present"] = config_path.is_file()

    try:
        with urlopen(tags_url, timeout=5) as response:  # noqa: S310 -- configured local endpoint
            payload = json.load(response)
        models = payload.get("models", [])
        checks["ollama_reachable"] = True
        checks["ollama_model_count"] = len(models) if isinstance(models, list) else None
    except (OSError, URLError, ValueError, json.JSONDecodeError) as exc:
        checks["ollama_reachable"] = False
        checks["ollama_error"] = str(exc)

    healthy = all(
        checks.get(name) is True
        for name in ("state_writable", "config_present", "ollama_reachable")
    )
    result["healthy"] = healthy
    print(json.dumps(result, sort_keys=True))
    return 0 if healthy else 1


def _load_config() -> dict[str, Any]:
    """Load JSON content from repos.yml (JSON is valid YAML)."""
    config_path = Path(os.environ["AGENT_CONFIG"])
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"Repository configuration is missing: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{config_path} must contain JSON-compatible YAML: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Repository configuration must be a JSON object")
    return payload


def _github_environment() -> dict[str, str]:
    """Read a GitHub token from a mounted secret file without logging it."""
    token_file = Path(os.environ.get("GITHUB_TOKEN_FILE", "/run/secrets/github-token"))
    try:
        token = token_file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(
            f"GitHub token file is unavailable: {token_file}. "
            "Create it as a read-only mounted secret."
        ) from exc
    if not token:
        raise RuntimeError(f"GitHub token file is empty: {token_file}")
    environment = os.environ.copy()
    environment["GH_TOKEN"] = token
    return environment


def _gh_json(arguments: list[str], environment: dict[str, str]) -> Any:
    """Call GitHub CLI and decode a JSON response without exposing tokens."""
    try:
        completed = run(
            ["gh", *arguments],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
    except CalledProcessError as exc:
        message = exc.stderr.strip() or exc.stdout.strip() or "unknown gh failure"
        raise RuntimeError(message) from exc
    decoder = json.JSONDecoder()
    position = 0
    values: list[Any] = []
    output = completed.stdout
    while position < len(output):
        while position < len(output) and output[position].isspace():
            position += 1
        if position == len(output):
            break
        try:
            value, position = decoder.raw_decode(output, position)
        except json.JSONDecodeError as exc:
            raise RuntimeError("GitHub CLI returned invalid JSON") from exc
        values.append(value)
    if not values:
        raise RuntimeError("GitHub CLI returned an empty response")
    return values[0] if len(values) == 1 else values


def _optional_alerts(repository: str, endpoint: str, environment: dict[str, str]) -> dict[str, Any]:
    """Report a security-alert class as unavailable when GitHub denies access."""
    try:
        alerts = _gh_json(
            [
                "api",
                "--paginate",
                f"repos/{repository}/{endpoint}?state=open&per_page=100",
            ],
            environment,
        )
        if not isinstance(alerts, list):
            raise RuntimeError("unexpected response shape")
        pages = alerts if all(isinstance(page, list) for page in alerts) else [alerts]
        flattened = [item for page in pages for item in page]
        return {"status": "available", "count": len(flattened), "alerts": flattened}
    except RuntimeError as exc:
        return {"status": "unavailable", "error": str(exc)}


def _inventory_repository(repository: str, environment: dict[str, str]) -> dict[str, Any]:
    pull_requests = _gh_json(
        [
            "pr",
            "list",
            "--repo",
            repository,
            "--state",
            "open",
            "--limit",
            "100",
            "--json",
            "number,title,author,headRefName,baseRefName,isDraft,reviewDecision,url",
        ],
        environment,
    )
    if not isinstance(pull_requests, list):
        raise RuntimeError("GitHub CLI returned an unexpected pull-request response")
    return {
        "repository": repository,
        "open_pull_requests": {"count": len(pull_requests), "items": pull_requests},
        "dependabot": _optional_alerts(repository, "dependabot/alerts", environment),
        "code_scanning": _optional_alerts(repository, "code-scanning/alerts", environment),
        "secret_scanning": _optional_alerts(repository, "secret-scanning/alerts", environment),
    }


def _alert_severity(alert: dict[str, Any]) -> str:
    advisory = alert.get("security_advisory")
    if isinstance(advisory, dict) and isinstance(advisory.get("severity"), str):
        return advisory["severity"].lower()
    for key in ("security_severity", "severity"):
        if isinstance(alert.get(key), str):
            return alert[key].lower()
    return "unclassified"


def _team_lead_report(inventory: dict[str, Any]) -> str:
    """Render an evidence-only daily maintenance briefing from the raw inventory."""
    lines = [
        "# Repository maintenance briefing",
        "",
        f"- Started: {inventory['started_at']}",
        f"- Completed: {inventory['completed_at']}",
        f"- Inventory status: **{inventory['status']}**",
        "- Mode: read-only (no branches, commits, PRs, or alert dismissals were made)",
    ]
    lines.extend(["", "## Agent roster", ""])
    for agent in _agent_definitions():
        lines.append(f"- `{agent['id']}` — {agent['status']} ({agent['execution']})")
    if inventory.get("error"):
        lines.extend(["", "## Blocker", "", str(inventory["error"])])

    priority: dict[str, int] = {
        severity: 0 for severity in ("critical", "high", "medium", "low", "unclassified")
    }
    unavailable: list[str] = []
    repositories = inventory.get("repositories", [])
    if not repositories:
        lines.extend(["", "## Repositories", "", "No repository inventory was completed."])

    for repository_data in repositories:
        if not isinstance(repository_data, dict):
            continue
        repository = str(repository_data.get("repository", "unknown repository"))
        pull_requests = repository_data.get("open_pull_requests", {})
        pull_request_items = (
            pull_requests.get("items", []) if isinstance(pull_requests, dict) else []
        )
        lines.extend(
            ["", f"## {repository}", "", f"Open pull requests: **{len(pull_request_items)}**"]
        )
        for pull_request in pull_request_items:
            if not isinstance(pull_request, dict):
                continue
            number = pull_request.get("number")
            title = pull_request.get("title")
            url = pull_request.get("url")
            lines.append(f"- [#{number} — {title}]({url})")

        lines.extend(["", "Security alerts:"])
        for source, label in (
            ("dependabot", "Dependabot"),
            ("code_scanning", "Code scanning"),
            ("secret_scanning", "Secret scanning"),
        ):
            source_data = repository_data.get(source, {})
            if not isinstance(source_data, dict) or source_data.get("status") != "available":
                unavailable.append(f"{repository}: {label}")
                lines.append(f"- {label}: unavailable")
                continue
            alerts = source_data.get("alerts", [])
            if not isinstance(alerts, list):
                alerts = []
            for alert in alerts:
                if isinstance(alert, dict):
                    severity = _alert_severity(alert)
                    priority[severity] = priority.get(severity, 0) + 1
            lines.append(f"- {label}: {len(alerts)} open")

    lines.extend(["", "## Priority summary", ""])
    for severity in ("critical", "high", "medium", "low", "unclassified"):
        lines.append(f"- {severity.title()}: {priority[severity]}")
    if unavailable:
        lines.extend(["", "## Required follow-up", ""])
        lines.append(
            "- Restore unavailable alert permissions before treating this inventory as complete."
        )
        lines.extend(f"- {item}" for item in unavailable)
    elif priority["critical"] or priority["high"]:
        lines.extend(
            [
                "",
                "## Required follow-up",
                "",
                "- Send critical and high findings to the architect planning stage.",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "## Required follow-up",
                "",
                "- Send the open PR and remaining alert inventory to the architect planning stage.",
            ]
        )
    lines.append("")
    return "\n".join(lines)


def run_inventory() -> int:
    """Create a read-only inventory report for all configured repositories."""
    timestamp = datetime.now(UTC)
    report: dict[str, Any] = {
        "kind": "github_inventory",
        "started_at": timestamp.isoformat(),
        "mode": "read_only",
        "repositories": [],
        "status": "passed",
    }
    try:
        configuration = _load_config()
        repositories = configuration.get("repositories", [])
        if not isinstance(repositories, list):
            raise RuntimeError("repositories must be a list")
        environment = _github_environment()
        for entry in repositories:
            if not isinstance(entry, dict) or not isinstance(entry.get("slug"), str):
                raise RuntimeError("each repository requires a string slug, e.g. owner/repository")
            report["repositories"].append(_inventory_repository(entry["slug"], environment))
    except RuntimeError as exc:
        report["status"] = "blocked"
        report["error"] = str(exc)

    report["completed_at"] = datetime.now(UTC).isoformat()
    state_dir = Path(os.environ["AGENT_STATE_DIR"])
    run_dir = state_dir / "runs" / timestamp.strftime("%Y%m%dT%H%M%SZ")
    run_dir.mkdir(parents=True, exist_ok=True)
    team_lead_path = run_dir / "team-lead-report.md"
    latest_team_lead_path = state_dir / "latest-team-lead-report.md"
    report["team_lead_report"] = str(team_lead_path)
    team_lead_text = _team_lead_report(report)
    team_lead_path.write_text(team_lead_text, encoding="utf-8")
    latest_team_lead_path.write_text(team_lead_text, encoding="utf-8")
    report_text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    (run_dir / "inventory.json").write_text(report_text, encoding="utf-8")
    (state_dir / "latest-inventory.json").write_text(report_text, encoding="utf-8")
    print(
        json.dumps(
            {
                "event": "inventory_completed",
                "report": str(run_dir / "inventory.json"),
                "status": report["status"],
            }
        )
    )
    return 0 if report["status"] == "passed" else 1


def daemon() -> int:
    """Run a read-only inventory at startup and at a fixed interval."""
    try:
        interval = int(os.environ.get("AGENT_RUN_INTERVAL_SECONDS", "86400"))
    except ValueError:
        print(
            json.dumps(
                {
                    "event": "configuration_error",
                    "error": "AGENT_RUN_INTERVAL_SECONDS must be an integer",
                }
            )
        )
        return 2
    if interval < 60:
        print(
            json.dumps(
                {
                    "event": "configuration_error",
                    "error": "AGENT_RUN_INTERVAL_SECONDS must be at least 60",
                }
            )
        )
        return 2

    print(
        json.dumps({"event": "daemon_started", "interval_seconds": interval, "mode": "read_only"})
    )
    while True:
        run_inventory()
        time.sleep(interval)


def main() -> None:
    parser = argparse.ArgumentParser(prog="repo-agent")
    parser.add_argument(
        "command",
        choices=(
            "agents",
            "daemon",
            "engineer-handoff",
            "engineer-preflight",
            "engineer-execute",
            "health",
            "plan-once",
            "run-once",
            "version",
        ),
    )
    parser.add_argument("--item", help="exact approved work-item ID for engineer-handoff")
    args = parser.parse_args()

    if args.command == "version":
        print(__version__)
        return
    if args.command == "agents":
        print(json.dumps(_agent_definitions(), indent=2))
        return
    if args.command == "run-once":
        sys.exit(run_inventory())
    if args.command == "plan-once":
        sys.exit(run_planning())
    if args.command == "engineer-handoff":
        if not args.item:
            parser.error("engineer-handoff requires --item <approved-work-item-id>")
        sys.exit(run_engineer_handoff(args.item))
    if args.command == "engineer-preflight":
        if not args.item:
            parser.error("engineer-preflight requires --item <approved-work-item-id>")
        sys.exit(run_engineer_preflight(args.item))
    if args.command == "engineer-execute":
        if not args.item:
            parser.error("engineer-execute requires --item <approved-work-item-id>")
        sys.exit(run_engineer_execute(args.item))
    if args.command == "daemon":
        sys.exit(daemon())
    sys.exit(health())


if __name__ == "__main__":
    main()
