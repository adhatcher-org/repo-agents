"""Read-only architect and critic planning stage backed by local Ollama."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen


def _ollama_chat_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/api/chat"


def _positive_integer(value: str, setting: str) -> int:
    try:
        timeout = int(value)
    except ValueError as exc:
        raise RuntimeError(f"{setting} must be an integer") from exc
    if timeout < 1:
        raise RuntimeError(f"{setting} must be at least 1")
    return timeout


def _front_matter_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[:1] == value[-1:] and value[:1] in {'"', "'"}:
        return value[1:-1]
    return value


def _agent_configuration(agent_id: str) -> dict[str, Any]:
    """Load editable prompt and provider settings from a mounted agent definition."""
    definitions_dir = Path(os.environ.get("AGENT_DEFINITIONS_DIR", "/app/agents"))
    for definition_path in sorted(definitions_dir.glob("*.md")):
        try:
            text = definition_path.read_text(encoding="utf-8")
        except OSError:
            continue
        lines = text.splitlines()
        if lines[:1] != ["---"]:
            continue
        metadata: dict[str, str] = {}
        body_start = 1
        for index, line in enumerate(lines[1:], start=1):
            if line == "---":
                body_start = index + 1
                break
            key, separator, value = line.partition(":")
            if separator:
                metadata[key.strip()] = _front_matter_value(value)
        if metadata.get("id") == agent_id:
            prompt = "\n".join(lines[body_start:]).strip()
            if prompt:
                provider = metadata.get("provider", "").lower()
                model = metadata.get("model", "").strip()
                if provider != "ollama" or not model or model == "none":
                    raise RuntimeError(
                        f"agent definition requires an Ollama model: {definition_path}"
                    )
                try:
                    temperature = float(metadata.get("temperature", "0"))
                except ValueError as exc:
                    raise RuntimeError(
                        f"agent temperature must be numeric: {definition_path}"
                    ) from exc
                if not 0 <= temperature <= 2:
                    raise RuntimeError(
                        f"agent temperature must be between 0 and 2: {definition_path}"
                    )
                return {
                    "definition": str(definition_path),
                    "instructions": prompt,
                    "model": model,
                    "temperature": temperature,
                    "timeout_seconds": _positive_integer(
                        metadata.get("timeout_seconds", "120"), "agent timeout_seconds"
                    ),
                }
            raise RuntimeError(f"agent definition is empty: {definition_path}")
    raise RuntimeError(f"agent definition is unavailable: {agent_id} in {definitions_dir}")


def _ollama_json(
    model: str, instructions: str, payload: dict[str, Any], *, temperature: float, timeout: int
) -> dict[str, Any]:
    """Request strict JSON from a local Ollama model without sending credentials."""
    request_payload = {
        "model": model,
        "stream": False,
        "format": "json",
        "messages": [
            {"role": "system", "content": instructions},
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False, sort_keys=True),
            },
        ],
        "options": {"temperature": temperature},
    }
    request = Request(
        _ollama_chat_url(os.environ["OLLAMA_BASE_URL"]),
        data=json.dumps(request_payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 -- local endpoint
            response_payload = json.load(response)
    except (OSError, URLError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Ollama request failed: {exc}") from exc
    message = response_payload.get("message") if isinstance(response_payload, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        raise RuntimeError("Ollama response did not contain a message content string")
    try:
        result = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Ollama response was not valid JSON") from exc
    if not isinstance(result, dict):
        raise RuntimeError("Ollama response must be a JSON object")
    return result


def _alert_title(alert: dict[str, Any]) -> str:
    rule = alert.get("rule")
    if isinstance(rule, dict) and isinstance(rule.get("name"), str):
        return rule["name"]
    advisory = alert.get("security_advisory")
    if isinstance(advisory, dict) and isinstance(advisory.get("summary"), str):
        return advisory["summary"]
    return "Security alert"


def _alert_severity(alert: dict[str, Any]) -> str:
    advisory = alert.get("security_advisory")
    if isinstance(advisory, dict) and isinstance(advisory.get("severity"), str):
        return advisory["severity"].lower()
    for key in ("security_severity", "security_severity_level", "severity"):
        if isinstance(alert.get(key), str):
            return alert[key].lower()
    return "unclassified"


def work_items(inventory: dict[str, Any]) -> list[dict[str, Any]]:
    """Make a compact, deterministic inventory contract for both planning agents."""
    items: list[dict[str, Any]] = []
    repositories = inventory.get("repositories", [])
    if not isinstance(repositories, list):
        raise RuntimeError("inventory repositories must be a list")
    for repository_data in repositories:
        if not isinstance(repository_data, dict):
            continue
        repository = repository_data.get("repository")
        if not isinstance(repository, str):
            continue
        pull_requests = repository_data.get("open_pull_requests", {})
        pull_request_items = (
            pull_requests.get("items", []) if isinstance(pull_requests, dict) else []
        )
        if isinstance(pull_request_items, list):
            for pull_request in pull_request_items:
                if not isinstance(pull_request, dict) or not isinstance(
                    pull_request.get("number"), int
                ):
                    continue
                number = pull_request["number"]
                items.append(
                    {
                        "id": f"{repository}:pr:{number}",
                        "kind": "open_pull_request",
                        "repository": repository,
                        "title": str(pull_request.get("title", "Untitled pull request")),
                        "url": str(pull_request.get("url", "")),
                        "is_dependabot": bool(
                            isinstance(pull_request.get("author"), dict)
                            and pull_request["author"].get("is_bot")
                        ),
                    }
                )
        for source in ("dependabot", "code_scanning", "secret_scanning"):
            source_data = repository_data.get(source, {})
            if not isinstance(source_data, dict) or source_data.get("status") != "available":
                items.append(
                    {
                        "id": f"{repository}:unavailable:{source}",
                        "kind": "unavailable_security_source",
                        "repository": repository,
                        "title": f"Restore {source.replace('_', ' ')} access",
                    }
                )
                continue
            alerts = source_data.get("alerts", [])
            if not isinstance(alerts, list):
                continue
            for alert in alerts:
                if not isinstance(alert, dict) or not isinstance(alert.get("number"), int):
                    continue
                number = alert["number"]
                items.append(
                    {
                        "id": f"{repository}:{source}:{number}",
                        "kind": source,
                        "repository": repository,
                        "title": _alert_title(alert),
                        "severity": _alert_severity(alert),
                        "url": str(alert.get("html_url", "")),
                    }
                )
    return items


def _validate_architect_plan(plan: dict[str, Any], expected_ids: set[str]) -> dict[str, Any]:
    entries = plan.get("items")
    if not isinstance(entries, list):
        raise RuntimeError("architect response requires an items list")
    plan_ids: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            raise RuntimeError("every architect plan item requires a string id")
        if not isinstance(entry.get("disposition"), str):
            raise RuntimeError("every architect plan item requires a disposition")
        if not isinstance(entry.get("acceptance_criteria"), list):
            raise RuntimeError("every architect plan item requires acceptance_criteria")
        plan_ids.append(entry["id"])
    if len(plan_ids) != len(set(plan_ids)):
        raise RuntimeError("architect plan contains duplicate item ids")
    provided_ids = set(plan_ids)
    if provided_ids != expected_ids:
        missing = sorted(expected_ids - provided_ids)
        unexpected = sorted(provided_ids - expected_ids)
        raise RuntimeError(
            "architect plan does not cover the inventory exactly; "
            f"missing={missing}, unexpected={unexpected}"
        )
    return plan


def _validate_critic_response(critique: dict[str, Any], expected_ids: set[str]) -> dict[str, Any]:
    verdict = critique.get("verdict")
    if verdict not in {"approved", "changes_requested"}:
        raise RuntimeError("critic verdict must be approved or changes_requested")
    coverage = critique.get("covered_item_ids")
    if not isinstance(coverage, list) or not all(isinstance(item, str) for item in coverage):
        raise RuntimeError("critic response requires covered_item_ids")
    if set(coverage) != expected_ids:
        raise RuntimeError("critic coverage does not match the inventory")
    findings = critique.get("findings")
    if not isinstance(findings, list):
        raise RuntimeError("critic response requires findings")
    return critique


def _markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# Architect planning review",
        "",
        f"- Inventory: `{report.get('inventory_path', 'unknown')}`",
        f"- Status: **{report['status']}**",
        "- Mode: read-only (no repository changes, branches, pull requests, or merges were made)",
    ]
    if report.get("error"):
        lines.extend(["", "## Blocker", "", str(report["error"])])
    items = report.get("work_items", [])
    lines.extend(["", "## Inventory coverage", ""])
    if not items:
        lines.append("No open pull-request, security-alert, or unavailable-source work items.")
    else:
        for item in items:
            if isinstance(item, dict):
                lines.append(f"- `{item.get('id')}` — {item.get('title')}")
    architect = report.get("architect_plan")
    if isinstance(architect, dict):
        lines.extend(
            ["", "## Architect plan", "", str(architect.get("summary", "No summary provided."))]
        )
        for item in architect.get("items", []):
            if isinstance(item, dict):
                lines.append(
                    f"- `{item.get('id')}` — **{item.get('disposition')}**: {item.get('rationale')}"
                )
    critic = report.get("critic")
    if isinstance(critic, dict):
        lines.extend(["", "## Architect critic", "", f"Verdict: **{critic.get('verdict')}**"])
        for finding in critic.get("findings", []):
            lines.append(f"- {finding}")
    lines.append("")
    return "\n".join(lines)


def run_planning() -> int:
    """Create an architect plan and independent critic result from the latest passed inventory."""
    timestamp = datetime.now(UTC)
    state_dir = Path(os.environ["AGENT_STATE_DIR"])
    inventory_path = state_dir / "latest-inventory.json"
    report: dict[str, Any] = {
        "kind": "architect_critic_planning",
        "started_at": timestamp.isoformat(),
        "mode": "read_only",
        "inventory_path": str(inventory_path),
        "status": "blocked",
        "work_items": [],
    }
    try:
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
        if not isinstance(inventory, dict) or inventory.get("status") != "passed":
            raise RuntimeError("latest inventory must be a passed JSON object")
        items = work_items(inventory)
        report["work_items"] = items
        if not items:
            report["status"] = "no_work"
        else:
            architect = _agent_configuration("senior_architect")
            critic = _agent_configuration("senior_architect_critic")
            report["architect_agent"] = {
                key: architect[key]
                for key in ("definition", "model", "temperature", "timeout_seconds")
            }
            report["critic_agent"] = {
                key: critic[key]
                for key in ("definition", "model", "temperature", "timeout_seconds")
            }
            expected_ids = {item["id"] for item in items}
            architect_plan = _validate_architect_plan(
                _ollama_json(
                    str(architect["model"]),
                    str(architect["instructions"]),
                    {"work_items": items},
                    temperature=float(architect["temperature"]),
                    timeout=int(architect["timeout_seconds"]),
                ),
                expected_ids,
            )
            critique = _validate_critic_response(
                _ollama_json(
                    str(critic["model"]),
                    str(critic["instructions"]),
                    {"work_items": items, "architect_plan": architect_plan},
                    temperature=float(critic["temperature"]),
                    timeout=int(critic["timeout_seconds"]),
                ),
                expected_ids,
            )
            report["architect_plan"] = architect_plan
            report["critic"] = critique
            report["status"] = critique["verdict"]
    except (OSError, RuntimeError, json.JSONDecodeError) as exc:
        report["error"] = str(exc)

    report["completed_at"] = datetime.now(UTC).isoformat()
    run_dir = state_dir / "runs" / timestamp.strftime("%Y%m%dT%H%M%SZ")
    run_dir.mkdir(parents=True, exist_ok=True)
    report_path = run_dir / "architect-plan.json"
    markdown_path = run_dir / "architect-plan.md"
    report["report_path"] = str(report_path)
    report["markdown_path"] = str(markdown_path)
    rendered = _markdown_report(report)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown_path.write_text(rendered, encoding="utf-8")
    (state_dir / "latest-architect-plan.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (state_dir / "latest-architect-plan.md").write_text(rendered, encoding="utf-8")
    print(
        json.dumps(
            {"event": "planning_completed", "report": str(report_path), "status": report["status"]}
        )
    )
    return 1 if report["status"] == "blocked" else 0
