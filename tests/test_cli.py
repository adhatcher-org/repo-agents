from __future__ import annotations

import io
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from subprocess import CalledProcessError, TimeoutExpired
from types import SimpleNamespace
from urllib.error import URLError
from urllib.request import Request

import pytest

from repo_agent import cli, engineering, planning, testing


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def __enter__(self) -> io.StringIO:
        return io.StringIO(json.dumps(self._payload))

    def __exit__(self, *_: object) -> None:
        return None


def test_health_passes_with_writable_state_config_and_ollama(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = tmp_path / "repos.yml"
    config_path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AGENT_CONFIG", str(config_path))
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama:11434")
    monkeypatch.setattr(cli, "urlopen", lambda *_args, **_kwargs: _Response({"models": [{}, {}]}))

    assert cli.health() == 0

    result = json.loads(capsys.readouterr().out)
    assert result["healthy"] is True
    assert result["checks"]["ollama_model_count"] == 2


def test_health_reports_unwritable_state_and_unreachable_ollama(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state_path = tmp_path / "not-a-directory"
    state_path.write_text("x", encoding="utf-8")
    monkeypatch.setenv("AGENT_STATE_DIR", str(state_path))
    monkeypatch.setenv("AGENT_CONFIG", str(tmp_path / "missing.yml"))
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama:11434")
    monkeypatch.setattr(
        cli, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(URLError("down"))
    )

    assert cli.health() == 1

    result = json.loads(capsys.readouterr().out)
    assert result["checks"]["state_writable"] is False
    assert result["checks"]["ollama_reachable"] is False


def test_load_config_rejects_non_object(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config_path = tmp_path / "repos.yml"
    config_path.write_text("[]", encoding="utf-8")
    monkeypatch.setenv("AGENT_CONFIG", str(config_path))

    with pytest.raises(RuntimeError, match="JSON object"):
        cli._load_config()


def test_load_config_returns_object(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config_path = tmp_path / "repos.yml"
    config_path.write_text('{"repositories": []}', encoding="utf-8")
    monkeypatch.setenv("AGENT_CONFIG", str(config_path))

    assert cli._load_config() == {"repositories": []}


@pytest.mark.parametrize(
    ("contents", "message"),
    [(None, "missing"), ("{", "JSON-compatible YAML")],
)
def test_load_config_reports_missing_or_invalid_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, contents: str | None, message: str
) -> None:
    config_path = tmp_path / "repos.yml"
    if contents is not None:
        config_path.write_text(contents, encoding="utf-8")
    monkeypatch.setenv("AGENT_CONFIG", str(config_path))

    with pytest.raises(RuntimeError, match=message):
        cli._load_config()


def test_agent_definitions_are_discovered(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "team.md").write_text(
        "---\nid: team\nstatus: active\nexecution: deterministic\n---\n# Team\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENT_DEFINITIONS_DIR", str(tmp_path))

    assert cli._agent_definitions() == [
        {
            "id": "team",
            "status": "active",
            "execution": "deterministic",
            "provider": "none",
            "model": "none",
            "definition": str(tmp_path / "team.md"),
        }
    ]


def test_agent_definitions_skip_unreadable_and_default_missing_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "plain.md").write_text("# Plain\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_DEFINITIONS_DIR", str(tmp_path))

    assert cli._agent_definitions()[0]["status"] == "unknown"


def test_planning_timeout_accepts_positive_integer() -> None:
    assert planning._positive_integer("15", "timeout_seconds") == 15


@pytest.mark.parametrize("value", ["invalid", "0"])
def test_planning_timeout_rejects_invalid_values(value: str) -> None:
    with pytest.raises(RuntimeError, match="timeout_seconds"):
        planning._positive_integer(value, "timeout_seconds")


def test_ollama_json_posts_structured_request_and_decodes_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(request: object, timeout: int) -> _Response:
        captured["request"] = request
        captured["timeout"] = timeout
        return _Response({"message": {"content": '{"summary":"ok"}'}})

    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama:11434/")
    monkeypatch.setattr(planning, "urlopen", fake_urlopen)

    assert planning._ollama_json(
        "architect", "instructions", {"work_items": []}, temperature=0, timeout=5
    ) == {"summary": "ok"}
    request = captured["request"]
    assert isinstance(request, type(Request("http://example.test")))
    assert request.full_url == "http://ollama:11434/api/chat"
    assert captured["timeout"] == 5


def test_agent_configuration_reads_prompt_and_model_from_mounted_definition(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "architect.md").write_text(
        '---\nid: senior_architect\nprovider: ollama\nmodel: architect\ntemperature: "0"\n'
        'timeout_seconds: "15"\n---\n# Editable prompt\n\nReturn JSON.\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENT_DEFINITIONS_DIR", str(tmp_path))

    assert planning._agent_configuration("senior_architect") == {
        "definition": str(tmp_path / "architect.md"),
        "instructions": "# Editable prompt\n\nReturn JSON.",
        "model": "architect",
        "temperature": 0.0,
        "timeout_seconds": 15,
    }


def test_agent_configuration_rejects_missing_definition(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AGENT_DEFINITIONS_DIR", str(tmp_path))

    with pytest.raises(RuntimeError, match="unavailable"):
        planning._agent_configuration("senior_architect")


def test_agent_configuration_rejects_empty_definition(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "architect.md").write_text(
        "---\nid: senior_architect\nprovider: ollama\nmodel: architect\n---\n", encoding="utf-8"
    )
    monkeypatch.setenv("AGENT_DEFINITIONS_DIR", str(tmp_path))

    with pytest.raises(RuntimeError, match="empty"):
        planning._agent_configuration("senior_architect")


@pytest.mark.parametrize(
    ("front_matter", "message"),
    [
        ("provider: none\nmodel: none", "requires an Ollama model"),
        ("provider: ollama\nmodel: architect\ntemperature: high", "temperature must be numeric"),
        ("provider: ollama\nmodel: architect\ntemperature: 3", "temperature must be between"),
    ],
)
def test_agent_configuration_rejects_invalid_model_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, front_matter: str, message: str
) -> None:
    (tmp_path / "architect.md").write_text(
        f"---\nid: senior_architect\n{front_matter}\n---\nPrompt\n", encoding="utf-8"
    )
    monkeypatch.setenv("AGENT_DEFINITIONS_DIR", str(tmp_path))

    with pytest.raises(RuntimeError, match=message):
        planning._agent_configuration("senior_architect")


@pytest.mark.parametrize(
    "payload, message",
    [
        ({"message": {"content": "not-json"}}, "not valid JSON"),
        ({"message": {"content": "[]"}}, "must be a JSON object"),
        ({}, "message content string"),
    ],
)
def test_ollama_json_rejects_invalid_model_responses(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, object], message: str
) -> None:
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama:11434")
    monkeypatch.setattr(planning, "urlopen", lambda *_args, **_kwargs: _Response(payload))

    with pytest.raises(RuntimeError, match=message):
        planning._ollama_json("architect", "instructions", {}, temperature=0, timeout=5)


def test_ollama_json_wraps_transport_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama:11434")
    monkeypatch.setattr(
        planning,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(URLError("unreachable")),
    )

    with pytest.raises(RuntimeError, match="Ollama request failed"):
        planning._ollama_json("architect", "instructions", {}, temperature=0, timeout=5)


@pytest.mark.parametrize(
    ("alert", "title", "severity"),
    [
        (
            {"security_advisory": {"summary": "Dependency issue", "severity": "HIGH"}},
            "Dependency issue",
            "high",
        ),
        ({}, "Security alert", "unclassified"),
    ],
)
def test_planning_alert_helpers(alert: dict[str, object], title: str, severity: str) -> None:
    assert planning._alert_title(alert) == title
    assert planning._alert_severity(alert) == severity


def test_work_items_cover_pr_alerts_and_unavailable_security_sources() -> None:
    items = planning.work_items(
        {
            "repositories": [
                {
                    "repository": "owner/repo",
                    "open_pull_requests": {
                        "items": [{"number": 7, "title": "Bump", "url": "https://example.test/pr"}]
                    },
                    "dependabot": {"status": "available", "alerts": []},
                    "code_scanning": {
                        "status": "available",
                        "alerts": [
                            {
                                "number": 8,
                                "html_url": "https://example.test/alert",
                                "rule": {"name": "Stack trace exposure"},
                                "security_severity_level": "medium",
                            }
                        ],
                    },
                    "secret_scanning": {"status": "unavailable"},
                }
            ]
        }
    )

    assert [item["id"] for item in items] == [
        "owner/repo:pr:7",
        "owner/repo:code_scanning:8",
        "owner/repo:unavailable:secret_scanning",
    ]
    assert items[1]["severity"] == "medium"


def test_validate_architect_plan_requires_exact_coverage() -> None:
    plan = {
        "items": [{"id": "owner/repo:pr:7", "disposition": "review", "acceptance_criteria": []}]
    }

    assert planning._validate_architect_plan(plan, {"owner/repo:pr:7"}) == plan
    with pytest.raises(RuntimeError, match="missing"):
        planning._validate_architect_plan(plan, {"owner/repo:pr:7", "owner/repo:pr:8"})


def test_run_planning_persists_blocked_report_when_models_are_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    inventory = {
        "status": "passed",
        "repositories": [
            {
                "repository": "owner/repo",
                "open_pull_requests": {"items": [{"number": 7, "title": "Bump"}]},
                "dependabot": {"status": "available", "alerts": []},
                "code_scanning": {"status": "available", "alerts": []},
                "secret_scanning": {"status": "available", "alerts": []},
            }
        ],
    }
    (tmp_path / "latest-inventory.json").write_text(json.dumps(inventory), encoding="utf-8")
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))

    assert planning.run_planning() == 1

    report = json.loads((tmp_path / "latest-architect-plan.json").read_text(encoding="utf-8"))
    assert report["status"] == "blocked"
    assert "agent definition is unavailable" in report["error"]
    assert json.loads(capsys.readouterr().out)["event"] == "planning_completed"


def test_run_planning_requires_independent_approved_critic(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    inventory = {
        "status": "passed",
        "repositories": [
            {
                "repository": "owner/repo",
                "open_pull_requests": {"items": [{"number": 7, "title": "Bump"}]},
                "dependabot": {"status": "available", "alerts": []},
                "code_scanning": {"status": "available", "alerts": []},
                "secret_scanning": {"status": "available", "alerts": []},
            }
        ],
    }
    (tmp_path / "latest-inventory.json").write_text(json.dumps(inventory), encoding="utf-8")
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        planning,
        "_agent_configuration",
        lambda agent_id: {
            "definition": f"/agents/{agent_id}.md",
            "instructions": "prompt",
            "model": "architect" if agent_id == "senior_architect" else "critic",
            "temperature": 0.0,
            "timeout_seconds": 15,
        },
    )
    responses = iter(
        [
            {
                "summary": "Review the update",
                "architecture_document_updates": [],
                "items": [
                    {
                        "id": "owner/repo:pr:7",
                        "disposition": "review_and_test",
                        "rationale": "Dependency update",
                        "acceptance_criteria": ["Tests pass"],
                        "architecture_impact": "none",
                    }
                ],
            },
            {
                "verdict": "approved",
                "covered_item_ids": ["owner/repo:pr:7"],
                "findings": [],
            },
        ]
    )
    monkeypatch.setattr(planning, "_ollama_json", lambda *_args, **_kwargs: next(responses))

    assert planning.run_planning() == 0

    report = json.loads((tmp_path / "latest-architect-plan.json").read_text(encoding="utf-8"))
    assert report["status"] == "approved"
    assert report["architect_agent"]["model"] == "architect"
    assert report["critic"]["verdict"] == "approved"


def test_run_planning_records_no_work_without_calling_ollama(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    inventory = {"status": "passed", "repositories": []}
    (tmp_path / "latest-inventory.json").write_text(json.dumps(inventory), encoding="utf-8")
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        planning,
        "_ollama_json",
        lambda *_args: (_ for _ in ()).throw(AssertionError("Ollama should not be called")),
    )

    assert planning.run_planning() == 0

    report = json.loads((tmp_path / "latest-architect-plan.json").read_text(encoding="utf-8"))
    assert report["status"] == "no_work"


def test_engineer_handoff_requires_a_selected_item_from_an_approved_plan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plan = {
        "status": "approved",
        "critic": {"verdict": "approved"},
        "work_items": [
            {
                "id": "owner/repo:code_scanning:7",
                "repository": "owner/repo",
                "kind": "code_scanning",
                "title": "Stack trace exposure",
            }
        ],
        "architect_plan": {
            "items": [
                {
                    "id": "owner/repo:code_scanning:7",
                    "disposition": "remediate",
                    "rationale": "Avoid leaking internal details.",
                    "acceptance_criteria": ["Tests pass"],
                }
            ]
        },
    }
    metadata_path = tmp_path / "repo-info.yml"
    metadata_path.write_text(
        "repositories:\n"
        "  - slug: owner/repo\n"
        "    path: /work/repo\n"
        "    default_branch: main\n"
        "    architecture_docs: [docs/architecture.md]\n"
        "    quality_gates: {test: make test}\n",
        encoding="utf-8",
    )
    (tmp_path / "latest-architect-plan.json").write_text(json.dumps(plan), encoding="utf-8")
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("AGENT_REPOSITORY_INFO", str(metadata_path))
    monkeypatch.setenv("ENGINEER_REPOSITORY_ROOT", "/work")

    assert engineering.run_engineer_handoff("owner/repo:code_scanning:7") == 0

    report = json.loads((tmp_path / "latest-engineer-handoff.json").read_text(encoding="utf-8"))
    assert report["status"] == "ready_for_implementation"
    assert report["repository"]["path"] == "/work/repo"
    assert report["architect_decision"]["acceptance_criteria"] == ["Tests pass"]


def _dispatch_plan(*decisions: dict[str, object]) -> dict[str, object]:
    return {
        "status": "approved",
        "critic": {"verdict": "approved"},
        "report_path": "/data/runs/plan/architect-plan.json",
        "work_items": [
            {
                "id": decision["id"],
                "repository": "owner/repo",
                "kind": "dependabot",
                "title": str(decision["id"]),
            }
            for decision in decisions
        ],
        "architect_plan": {"items": list(decisions)},
    }


def _ready_handoff(tmp_path: Path, item_id: str) -> int:
    (tmp_path / "latest-engineer-handoff.json").write_text(
        json.dumps(
            {
                "status": "ready_for_implementation",
                "report_path": "/data/runs/handoff/engineer-handoff.json",
                "requested_item_id": item_id,
            }
        ),
        encoding="utf-8",
    )
    return 0


def test_team_lead_dispatch_selects_first_explicitly_eligible_item_in_plan_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plan = _dispatch_plan(
        {"id": "owner/repo:pr:1", "disposition": "defer"},
        {"id": "owner/repo:pr:2", "disposition": "Remediate"},
        {"id": "owner/repo:pr:3", "disposition": "approve"},
    )
    (tmp_path / "latest-architect-plan.json").write_text(json.dumps(plan), encoding="utf-8")
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    called: list[str] = []

    def fake_handoff(item_id: str) -> int:
        called.append(item_id)
        return _ready_handoff(tmp_path, item_id)

    monkeypatch.setattr(engineering, "run_engineer_handoff", fake_handoff)

    assert engineering.run_team_lead_dispatch() == 0

    active = json.loads((tmp_path / "active-work-item.json").read_text(encoding="utf-8"))
    report = json.loads((tmp_path / "latest-team-lead-dispatch.json").read_text(encoding="utf-8"))
    assert called == ["owner/repo:pr:2"]
    assert active["item_id"] == "owner/repo:pr:2"
    assert active["stage"] == "engineer_handoff"
    assert active["status"] == "assigned"
    assert report["status"] == "assigned"
    assert (tmp_path / "latest-team-lead-dispatch.md").is_file()


@pytest.mark.parametrize("disposition", ["Approve", "approved", "remediate"])
def test_team_lead_dispatch_accepts_only_normalized_eligible_dispositions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, disposition: str
) -> None:
    item_id = "owner/repo:pr:7"
    (tmp_path / "latest-architect-plan.json").write_text(
        json.dumps(_dispatch_plan({"id": item_id, "disposition": disposition})), encoding="utf-8"
    )
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        engineering, "run_engineer_handoff", lambda selected: _ready_handoff(tmp_path, selected)
    )

    assert engineering.run_team_lead_dispatch() == 0

    active = json.loads((tmp_path / "active-work-item.json").read_text(encoding="utf-8"))
    assert active["item_id"] == item_id


def test_team_lead_dispatch_refuses_a_second_nonterminal_assignment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "active-work-item.json").write_text(
        json.dumps({"status": "assigned", "item_id": "owner/repo:pr:1", "stage": "testing"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        engineering,
        "run_engineer_handoff",
        lambda _item_id: (_ for _ in ()).throw(AssertionError("handoff should not run")),
    )

    assert engineering.run_team_lead_dispatch() == 0

    report = json.loads((tmp_path / "latest-team-lead-dispatch.json").read_text(encoding="utf-8"))
    assert report["status"] == "already_assigned"
    assert report["item_id"] == "owner/repo:pr:1"


def test_team_lead_dispatch_reports_no_eligible_work_without_creating_active_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "latest-architect-plan.json").write_text(
        json.dumps(_dispatch_plan({"id": "owner/repo:pr:1", "disposition": "defer"})),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        engineering,
        "run_engineer_handoff",
        lambda _item_id: (_ for _ in ()).throw(AssertionError("handoff should not run")),
    )

    assert engineering.run_team_lead_dispatch() == 0

    report = json.loads((tmp_path / "latest-team-lead-dispatch.json").read_text(encoding="utf-8"))
    assert report["status"] == "no_eligible_work"
    assert not (tmp_path / "active-work-item.json").exists()


def test_team_lead_dispatch_blocks_invalid_plan_or_unsuccessful_handoff(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    (tmp_path / "latest-architect-plan.json").write_text(
        json.dumps({"status": "approved", "critic": {"verdict": "changes_requested"}}),
        encoding="utf-8",
    )

    assert engineering.run_team_lead_dispatch() == 1

    report = json.loads((tmp_path / "latest-team-lead-dispatch.json").read_text(encoding="utf-8"))
    assert report["status"] == "blocked"
    assert "critic verdict" in report["error"]
    assert not (tmp_path / "active-work-item.json").exists()

    (tmp_path / "latest-architect-plan.json").write_text(
        json.dumps(_dispatch_plan({"id": "owner/repo:pr:1", "disposition": "approve"})),
        encoding="utf-8",
    )
    monkeypatch.setattr(engineering, "run_engineer_handoff", lambda _item_id: 1)

    assert engineering.run_team_lead_dispatch() == 1

    report = json.loads((tmp_path / "latest-team-lead-dispatch.json").read_text(encoding="utf-8"))
    assert report["status"] == "blocked"
    assert "did not become ready" in report["error"]
    assert not (tmp_path / "active-work-item.json").exists()


def test_engineer_handoff_blocks_unconfigured_repository(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plan = {
        "status": "approved",
        "critic": {"verdict": "approved"},
        "work_items": [
            {"id": "owner/repo:pr:7", "repository": "owner/repo", "kind": "open_pull_request"}
        ],
        "architect_plan": {"items": [{"id": "owner/repo:pr:7"}]},
    }
    metadata_path = tmp_path / "repo-info.yml"
    metadata_path.write_text("repositories: []\n", encoding="utf-8")
    (tmp_path / "latest-architect-plan.json").write_text(json.dumps(plan), encoding="utf-8")
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("AGENT_REPOSITORY_INFO", str(metadata_path))

    assert engineering.run_engineer_handoff("owner/repo:pr:7") == 1

    report = json.loads((tmp_path / "latest-engineer-handoff.json").read_text(encoding="utf-8"))
    assert report["status"] == "blocked"
    assert "not configured" in report["error"]


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        ("[not-a-mapping]", "repositories list"),
        ("repositories:\n  - slug: owner/repo\n  - slug: owner/repo\n", "duplicate slug"),
        ("repositories: [", "not valid YAML"),
    ],
)
def test_repository_metadata_validation_rejects_invalid_inputs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    contents: str,
    message: str,
) -> None:
    metadata_path = tmp_path / "repo-info.yml"
    metadata_path.write_text(contents, encoding="utf-8")
    monkeypatch.setenv("AGENT_REPOSITORY_INFO", str(metadata_path))

    with pytest.raises(RuntimeError, match=message):
        engineering._load_repository_info()


def test_repository_metadata_validation_reports_missing_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AGENT_REPOSITORY_INFO", str(tmp_path / "missing.yml"))

    with pytest.raises(RuntimeError, match="metadata is missing"):
        engineering._load_repository_info()


@pytest.mark.parametrize(
    ("plan", "message"),
    [
        ({"status": "blocked"}, "status approved"),
        ({"status": "approved", "critic": {"verdict": "changes_requested"}}, "critic verdict"),
        ({"status": "approved", "critic": {"verdict": "approved"}}, "architect items"),
        (
            {
                "status": "approved",
                "critic": {"verdict": "approved"},
                "architect_plan": {"items": []},
                "work_items": [],
            },
            "not present",
        ),
    ],
)
def test_approved_item_rejects_unapproved_or_unknown_inputs(
    plan: dict[str, object], message: str
) -> None:
    with pytest.raises(RuntimeError, match=message):
        engineering._approved_item("owner/repo:pr:7", plan)


@pytest.mark.parametrize(
    ("repository", "workspace"),
    [
        ("adhatcher-org/bourbonbook", "/projects/bourbonbook"),
        ("owner/repo", "/projects/nested/repo"),
    ],
)
def test_workspace_path_is_configured_and_contained(
    monkeypatch: pytest.MonkeyPatch, repository: str, workspace: str
) -> None:
    monkeypatch.setenv("ENGINEER_REPOSITORY_ROOT", "/projects")

    assert engineering._workspace_path(repository, {"path": workspace}) == Path(workspace)


@pytest.mark.parametrize(
    ("metadata", "message"),
    [
        ({}, "requires a path"),
        ({"path": "/other/repo"}, "must be inside"),
        ({"path": 1}, "requires a path"),
    ],
)
def test_workspace_path_rejects_missing_or_outside_configuration(
    monkeypatch: pytest.MonkeyPatch, metadata: dict[str, object], message: str
) -> None:
    monkeypatch.setenv("ENGINEER_REPOSITORY_ROOT", "/projects")
    with pytest.raises(RuntimeError, match=message):
        engineering._workspace_path("owner/repo", metadata)


def test_workspace_path_requires_absolute_repository_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENGINEER_REPOSITORY_ROOT", "projects")

    with pytest.raises(RuntimeError, match="must be an absolute path"):
        engineering._workspace_path("owner/repo", {"path": "/projects/repo"})


def test_engineer_preflight_prepares_only_the_handoff_repository(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    handoff = {
        "status": "ready_for_implementation",
        "work_item": {"id": "owner/repo:pr:7", "repository": "owner/repo"},
        "repository": {"path": "/projects/repo"},
    }
    (tmp_path / "latest-engineer-handoff.json").write_text(json.dumps(handoff), encoding="utf-8")
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        engineering, "_prepare_workspace", lambda repository, metadata: Path("/projects/repo")
    )

    assert engineering.run_engineer_preflight("owner/repo:pr:7") == 0

    report = json.loads((tmp_path / "latest-engineer-preflight.json").read_text(encoding="utf-8"))
    assert report["status"] == "ready_for_coding"
    assert report["repository"] == "owner/repo"
    assert report["workspace_path"] == "/projects/repo"
    assert report["work_item"] == handoff["work_item"]
    assert report["repository_metadata"] == handoff["repository"]


def test_engineer_preflight_blocks_mismatched_handoff(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    handoff = {
        "status": "ready_for_implementation",
        "work_item": {"id": "owner/repo:pr:8", "repository": "owner/repo"},
    }
    (tmp_path / "latest-engineer-handoff.json").write_text(json.dumps(handoff), encoding="utf-8")
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))

    assert engineering.run_engineer_preflight("owner/repo:pr:7") == 1

    report = json.loads((tmp_path / "latest-engineer-preflight.json").read_text(encoding="utf-8"))
    assert report["status"] == "blocked"
    assert "does not match" in report["error"]


def test_engineer_preflight_blocks_missing_repository_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    handoff = {
        "status": "ready_for_implementation",
        "work_item": {"id": "owner/repo:pr:7", "repository": "owner/repo"},
    }
    (tmp_path / "latest-engineer-handoff.json").write_text(json.dumps(handoff), encoding="utf-8")
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))

    assert engineering.run_engineer_preflight("owner/repo:pr:7") == 1

    report = json.loads((tmp_path / "latest-engineer-preflight.json").read_text(encoding="utf-8"))
    assert "does not contain repository metadata" in report["error"]


def test_prepare_workspace_inspects_clean_configured_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}
    workspace = tmp_path / "bourbonbook"
    (workspace / ".git").mkdir(parents=True)
    monkeypatch.setenv("ENGINEER_REPOSITORY_ROOT", str(tmp_path))

    def fake_run(arguments: list[str], **kwargs: object) -> SimpleNamespace:
        captured["arguments"] = arguments
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(engineering, "run", fake_run)

    assert engineering._prepare_workspace("owner/repo", {"path": str(workspace)}) == workspace
    assert captured["arguments"] == ["git", "-C", str(workspace), "status", "--porcelain"]


def test_prepare_workspace_rejects_missing_checkout_and_dirty_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ENGINEER_REPOSITORY_ROOT", str(tmp_path))
    workspace = tmp_path / "repo"
    with pytest.raises(RuntimeError, match="checkout is unavailable"):
        engineering._prepare_workspace("owner/repo", {"path": str(workspace)})

    (workspace / ".git").mkdir(parents=True)
    monkeypatch.setattr(
        engineering, "run", lambda *_args, **_kwargs: SimpleNamespace(stdout=" M app.py\n")
    )
    with pytest.raises(RuntimeError, match="uncommitted changes"):
        engineering._prepare_workspace("owner/repo", {"path": str(workspace)})


def test_prepare_workspace_reports_git_status_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "repo"
    (workspace / ".git").mkdir(parents=True)
    monkeypatch.setenv("ENGINEER_REPOSITORY_ROOT", str(tmp_path))
    error = CalledProcessError(1, ["git"], output="", stderr="unavailable")
    monkeypatch.setattr(engineering, "run", lambda *_args, **_kwargs: (_ for _ in ()).throw(error))

    with pytest.raises(RuntimeError, match="failed to inspect repository checkout: unavailable"):
        engineering._prepare_workspace("owner/repo", {"path": str(workspace)})


def test_engineer_response_requires_safe_complete_patch_contract() -> None:
    response = {
        "implementation_summary": "Update the pinned dependency.",
        "files_to_change": ["pyproject.toml"],
        "architecture_documents_to_update": [],
        "test_strategy": ["Run the project test target."],
        "risks": [],
        "patches": [
            {
                "path": "pyproject.toml",
                "diff": "diff --git a/pyproject.toml b/pyproject.toml\n"
                "--- a/pyproject.toml\n+++ b/pyproject.toml\n",
            }
        ],
    }

    assert engineering._validate_engineer_response(response) == response

    response["patches"][0]["path"] = "../outside.py"
    with pytest.raises(RuntimeError, match="unsafe patch path"):
        engineering._validate_engineer_response(response)


def test_engineer_response_rejects_mismatched_or_missing_patches() -> None:
    response = {
        "implementation_summary": "x",
        "files_to_change": ["app.py"],
        "architecture_documents_to_update": [],
        "test_strategy": [],
        "risks": [],
        "patches": [],
    }
    with pytest.raises(RuntimeError, match="at least one patch"):
        engineering._validate_engineer_response(response)


def test_engineer_execute_applies_validated_patches_after_preflight(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    preflight = {
        "status": "ready_for_coding",
        "work_item": {"id": "owner/repo:pr:7", "repository": "owner/repo"},
        "repository_metadata": {"path": "/projects/repo", "default_branch": "main"},
        "workspace_path": "/projects/repo",
    }
    (tmp_path / "latest-engineer-preflight.json").write_text(
        json.dumps(preflight), encoding="utf-8"
    )
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ENGINEER_REPOSITORY_ROOT", "/projects")
    monkeypatch.setattr(engineering, "_default_base", lambda *_args: "base-sha")
    monkeypatch.setattr(engineering, "_git_output", lambda *_args: "")
    monkeypatch.setattr(
        engineering,
        "_agent_configuration",
        lambda *_args: {
            "definition": "/agents/engineer.md",
            "instructions": "JSON only",
            "model": "coder",
            "temperature": 0.0,
            "timeout_seconds": 5,
        },
    )
    monkeypatch.setattr(engineering, "_repository_context", lambda *_args: {"tracked_files": []})
    response = {
        "implementation_summary": "x",
        "files_to_change": ["app.py"],
        "architecture_documents_to_update": [],
        "test_strategy": [],
        "risks": [],
        "patches": [{"path": "app.py", "diff": "diff --git a/app.py b/app.py\n"}],
    }
    monkeypatch.setattr(engineering, "_ollama_json", lambda *_args, **_kwargs: response)
    monkeypatch.setattr(
        engineering, "_apply_patches", lambda *_args: [{"path": "app.py", "sha256": "digest"}]
    )

    assert engineering.run_engineer_execute("owner/repo:pr:7") == 0

    report = json.loads((tmp_path / "latest-engineer-execution.json").read_text(encoding="utf-8"))
    assert report["status"] == "implementation_applied"
    assert report["base_commit"] == "base-sha"
    assert report["applied_patches"] == [{"path": "app.py", "sha256": "digest"}]
    assert "diff" not in json.dumps(report)


def test_engineer_execute_sends_existing_pull_request_to_testing_without_side_effects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    work_item = {
        "id": "owner/repo:pr:7",
        "kind": "open_pull_request",
        "repository": "owner/repo",
        "title": "Bump dependency",
        "url": "https://example.test/owner/repo/pull/7",
    }
    decision = {"id": work_item["id"], "disposition": "Approve"}
    preflight = {
        "status": "ready_for_coding",
        "work_item": work_item,
        "architect_decision": decision,
        "repository_metadata": {"path": "/projects/repo", "default_branch": "main"},
        "workspace_path": "/projects/repo",
    }
    (tmp_path / "latest-engineer-preflight.json").write_text(
        json.dumps(preflight), encoding="utf-8"
    )
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ENGINEER_REPOSITORY_ROOT", "/projects")

    def unexpected(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("existing pull request execution must not call this function")

    monkeypatch.setattr(engineering, "_default_base", unexpected)
    monkeypatch.setattr(engineering, "_git_output", unexpected)
    monkeypatch.setattr(engineering, "_agent_configuration", unexpected)
    monkeypatch.setattr(engineering, "_repository_context", unexpected)
    monkeypatch.setattr(engineering, "_ollama_json", unexpected)
    monkeypatch.setattr(engineering, "_apply_patches", unexpected)

    assert engineering.run_engineer_execute(work_item["id"]) == 0

    report = json.loads((tmp_path / "latest-engineer-execution.json").read_text(encoding="utf-8"))
    assert report["status"] == "existing_pull_request_ready_for_testing"
    assert report["mode"] == "existing_pull_request_no_repository_changes"
    assert report["work_item"] == work_item
    assert report["pull_request_url"] == work_item["url"]
    assert report["architect_decision"] == decision
    assert "branch" not in report


def test_engineer_execute_blocks_mismatched_preflight(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "latest-engineer-preflight.json").write_text(
        json.dumps({"status": "ready_for_coding", "work_item": {"id": "owner/repo:pr:8"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))

    assert engineering.run_engineer_execute("owner/repo:pr:7") == 1
    report = json.loads((tmp_path / "latest-engineer-execution.json").read_text(encoding="utf-8"))
    assert report["status"] == "blocked"
    assert "does not match" in report["error"]


def test_git_output_and_default_base_require_clean_current_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "repo"
    captured: list[list[str]] = []

    def fake_run(arguments: list[str], **_kwargs: object) -> SimpleNamespace:
        captured.append(arguments)
        return SimpleNamespace(stdout="abc123\n")

    monkeypatch.setattr(engineering, "run", fake_run)
    assert engineering._git_output(workspace, ["rev-parse", "HEAD"]) == "abc123"
    assert captured == [["git", "-C", str(workspace), "rev-parse", "HEAD"]]

    with pytest.raises(RuntimeError, match="requires a default_branch"):
        engineering._default_base(workspace, {})

    responses = iter(["", "main", "abc123", "abc123"])
    monkeypatch.setattr(engineering, "_git_output", lambda *_args: next(responses))
    assert engineering._default_base(workspace, {"default_branch": "main"}) == "abc123"

    monkeypatch.setattr(engineering, "_git_output", lambda *_args: " M app.py")
    with pytest.raises(RuntimeError, match="uncommitted changes"):
        engineering._default_base(workspace, {"default_branch": "main"})

    responses = iter(["", "feature"])
    monkeypatch.setattr(engineering, "_git_output", lambda *_args: next(responses))
    with pytest.raises(RuntimeError, match="default branch main"):
        engineering._default_base(workspace, {"default_branch": "main"})

    responses = iter(["", "main", "old", "new"])
    monkeypatch.setattr(engineering, "_git_output", lambda *_args: next(responses))
    with pytest.raises(RuntimeError, match="not current"):
        engineering._default_base(workspace, {"default_branch": "main"})


def test_git_output_reports_subprocess_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    error = CalledProcessError(1, ["git"], output="", stderr="not a repository")
    monkeypatch.setattr(engineering, "run", lambda *_args, **_kwargs: (_ for _ in ()).throw(error))
    with pytest.raises(RuntimeError, match="git status --porcelain failed: not a repository"):
        engineering._git_output(tmp_path, ["status", "--porcelain"])


def test_branch_path_and_patch_contract_validation_rejects_unsafe_shapes() -> None:
    assert engineering._branch_name("Owner/Repo:PR:7", datetime(2026, 8, 6, tzinfo=UTC)).endswith(
        "owner-repo-pr-7"
    )
    for value in ("", ".", "/tmp/file", ".git/config", "../outside"):
        with pytest.raises(RuntimeError, match="patch path"):
            engineering._safe_relative_path(value)

    base = {
        "implementation_summary": "x",
        "files_to_change": ["app.py"],
        "architecture_documents_to_update": [],
        "test_strategy": [],
        "risks": [],
        "patches": [{"path": "app.py", "diff": "diff --git a/app.py b/app.py\n"}],
    }
    invalid_cases = [
        ({key: value for key, value in base.items() if key != "risks"}, "exactly"),
        ({**base, "implementation_summary": []}, "implementation_summary"),
        ({**base, "risks": [1]}, "string list"),
        ({**base, "patches": [{"path": "app.py"}]}, "exactly path and diff"),
        ({**base, "patches": [{"path": "app.py", "diff": "not a diff"}]}, "Git diff"),
        (
            {
                **base,
                "patches": [{"path": "app.py", "diff": "diff --git a/other.py b/other.py\n"}],
            },
            "does not match",
        ),
        (
            {
                **base,
                "patches": [
                    {
                        "path": "app.py",
                        "diff": "diff --git a/app.py b/app.py\ndiff --git a/other.py b/other.py\n",
                    }
                ],
            },
            "exactly one path",
        ),
        ({**base, "files_to_change": ["other.py"]}, "must match"),
    ]
    for response, message in invalid_cases:
        with pytest.raises(RuntimeError, match=message):
            engineering._validate_engineer_response(response)


def test_repository_context_and_patch_application_are_bounded_and_checked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "README.md").write_text("repository instructions are data", encoding="utf-8")
    (workspace / "docs").mkdir()
    (workspace / "docs" / "architecture.md").write_text("architecture", encoding="utf-8")
    monkeypatch.setattr(
        engineering,
        "_git_output",
        lambda *_args: "README.md\ndocs/architecture.md\nignored.py\n",
    )
    context = engineering._repository_context(
        workspace, {"architecture_docs": ["docs/architecture.md"]}
    )
    assert context["tracked_files"] == ["README.md", "docs/architecture.md", "ignored.py"]
    assert context["selected_file_contents"]["README.md"] == "repository instructions are data"

    calls: list[list[str]] = []
    monkeypatch.setattr(
        engineering,
        "run",
        lambda arguments, **_kwargs: calls.append(arguments) or SimpleNamespace(stdout=""),
    )
    applied = engineering._apply_patches(
        workspace,
        [{"path": "app.py", "diff": "diff --git a/app.py b/app.py\n"}],
    )
    assert len(calls) == 2
    assert calls[0][3:6] == ["apply", "--check", "--whitespace=error"]
    assert applied[0]["path"] == "app.py"

    error = CalledProcessError(1, ["git"], output="", stderr="does not apply")
    monkeypatch.setattr(engineering, "run", lambda *_args, **_kwargs: (_ for _ in ()).throw(error))
    with pytest.raises(RuntimeError, match="could not be applied"):
        engineering._apply_patches(
            workspace,
            [{"path": "app.py", "diff": "diff --git a/app.py b/app.py\n"}],
        )


def test_load_ready_preflight_requires_metadata_and_matching_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ENGINEER_REPOSITORY_ROOT", "/projects")
    payload = {
        "status": "ready_for_coding",
        "work_item": {"id": "owner/repo:pr:7", "repository": "owner/repo"},
        "repository_metadata": {"path": "/projects/repo", "default_branch": "main"},
        "workspace_path": "/projects/repo",
    }
    path = tmp_path / "latest-engineer-preflight.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert engineering._load_ready_preflight("owner/repo:pr:7", tmp_path)[1] == Path(
        "/projects/repo"
    )

    payload["workspace_path"] = "/projects/other"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RuntimeError, match="workspace does not match"):
        engineering._load_ready_preflight("owner/repo:pr:7", tmp_path)


def test_gh_json_decodes_paginated_responses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout='[{"number": 1}]\n[{"number": 2}]\n'),
    )

    assert cli._gh_json(["api", "--paginate", "example"], {}) == [
        [{"number": 1}],
        [{"number": 2}],
    ]


@pytest.mark.parametrize(
    ("stdout", "message"),
    [("", "empty response"), ("not json", "invalid JSON")],
)
def test_gh_json_rejects_bad_output(
    monkeypatch: pytest.MonkeyPatch, stdout: str, message: str
) -> None:
    monkeypatch.setattr(cli, "run", lambda *_args, **_kwargs: SimpleNamespace(stdout=stdout))

    with pytest.raises(RuntimeError, match=message):
        cli._gh_json(["api", "example"], {})


def test_gh_json_reports_cli_error(monkeypatch: pytest.MonkeyPatch) -> None:
    error = CalledProcessError(1, ["gh"], output="fallback", stderr="denied")
    monkeypatch.setattr(cli, "run", lambda *_args, **_kwargs: (_ for _ in ()).throw(error))

    with pytest.raises(RuntimeError, match="denied"):
        cli._gh_json(["api", "example"], {})


def test_github_environment_reads_token_without_logging_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    token_path = tmp_path / "github-token"
    token_path.write_text("test-token\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_TOKEN_FILE", str(token_path))

    assert cli._github_environment()["GH_TOKEN"] == "test-token"


@pytest.mark.parametrize("contents", [None, "\n"])
def test_github_environment_rejects_missing_or_empty_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, contents: str | None
) -> None:
    token_path = tmp_path / "github-token"
    if contents is not None:
        token_path.write_text(contents, encoding="utf-8")
    monkeypatch.setenv("GITHUB_TOKEN_FILE", str(token_path))

    with pytest.raises(RuntimeError, match="token file"):
        cli._github_environment()


def test_optional_alerts_flattens_multiple_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_gh_json", lambda *_args: [[{"number": 1}], [{"number": 2}]])

    result = cli._optional_alerts("owner/repo", "dependabot/alerts", {})

    assert result["status"] == "available"
    assert result["count"] == 2


@pytest.mark.parametrize("alerts", [{"not": "a list"}, RuntimeError("denied")])
def test_optional_alerts_reports_unavailable(
    monkeypatch: pytest.MonkeyPatch, alerts: object
) -> None:
    if isinstance(alerts, Exception):
        monkeypatch.setattr(cli, "_gh_json", lambda *_args: (_ for _ in ()).throw(alerts))
    else:
        monkeypatch.setattr(cli, "_gh_json", lambda *_args: alerts)

    assert cli._optional_alerts("owner/repo", "dependabot/alerts", {})["status"] == "unavailable"


def test_inventory_repository_collects_prs_and_all_alert_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = iter([[{"number": 1}], [], [], []])
    monkeypatch.setattr(cli, "_gh_json", lambda *_args: next(responses))

    result = cli._inventory_repository("owner/repo", {})

    assert result["open_pull_requests"]["count"] == 1
    assert result["dependabot"]["count"] == 0


def test_inventory_repository_rejects_non_list_pr_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_gh_json", lambda *_args: {"number": 1})

    with pytest.raises(RuntimeError, match="unexpected pull-request"):
        cli._inventory_repository("owner/repo", {})


@pytest.mark.parametrize(
    ("alert", "severity"),
    [
        ({"security_advisory": {"severity": "HIGH"}}, "high"),
        ({"security_severity": "medium"}, "medium"),
        ({"severity": "low"}, "low"),
        ({}, "unclassified"),
    ],
)
def test_alert_severity(alert: dict[str, object], severity: str) -> None:
    assert cli._alert_severity(alert) == severity


def test_team_lead_report_prioritizes_high_alerts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AGENT_DEFINITIONS_DIR", str(tmp_path))
    inventory = {
        "started_at": "2026-08-04T00:00:00+00:00",
        "completed_at": "2026-08-04T00:01:00+00:00",
        "status": "passed",
        "repositories": [
            {
                "repository": "owner/repo",
                "open_pull_requests": {
                    "items": [{"number": 7, "title": "Fix", "url": "https://example.test/7"}]
                },
                "dependabot": {
                    "status": "available",
                    "alerts": [{"security_advisory": {"severity": "high"}}],
                },
                "code_scanning": {"status": "available", "alerts": []},
                "secret_scanning": {"status": "available", "alerts": []},
            }
        ],
    }

    report = cli._team_lead_report(inventory)

    assert "High: 1" in report
    assert "critical and high findings" in report
    assert "[#7 — Fix](https://example.test/7)" in report


def test_team_lead_report_lists_unavailable_sources_and_blocker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AGENT_DEFINITIONS_DIR", str(tmp_path))
    report = cli._team_lead_report(
        {
            "started_at": "start",
            "completed_at": "finish",
            "status": "blocked",
            "error": "token missing",
            "repositories": [
                {
                    "repository": "owner/repo",
                    "open_pull_requests": "invalid",
                    "dependabot": {"status": "unavailable"},
                    "code_scanning": {"status": "available", "alerts": "invalid"},
                    "secret_scanning": {"status": "available", "alerts": []},
                }
            ],
        }
    )

    assert "## Blocker" in report
    assert "token missing" in report
    assert "Dependabot: unavailable" in report
    assert "Restore unavailable alert permissions" in report


def test_run_inventory_persists_raw_and_team_lead_reports(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("AGENT_DEFINITIONS_DIR", str(tmp_path / "agents"))
    (tmp_path / "agents").mkdir()
    monkeypatch.setattr(cli, "_load_config", lambda: {"repositories": [{"slug": "owner/repo"}]})
    monkeypatch.setattr(cli, "_github_environment", lambda: {"GH_TOKEN": "test"})
    monkeypatch.setattr(
        cli,
        "_inventory_repository",
        lambda repository, _environment: {
            "repository": repository,
            "open_pull_requests": {"count": 0, "items": []},
            "dependabot": {"status": "available", "alerts": []},
            "code_scanning": {"status": "available", "alerts": []},
            "secret_scanning": {"status": "available", "alerts": []},
        },
    )

    assert cli.run_inventory() == 0

    latest = json.loads((tmp_path / "latest-inventory.json").read_text(encoding="utf-8"))
    assert latest["status"] == "passed"
    assert (tmp_path / "latest-team-lead-report.md").is_file()


def test_run_inventory_persists_blocked_report_for_invalid_repository_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("AGENT_DEFINITIONS_DIR", str(tmp_path / "agents"))
    (tmp_path / "agents").mkdir()
    monkeypatch.setattr(cli, "_load_config", lambda: {"repositories": [{}]})
    monkeypatch.setattr(cli, "_github_environment", lambda: {"GH_TOKEN": "test"})

    assert cli.run_inventory() == 1

    latest = json.loads((tmp_path / "latest-inventory.json").read_text(encoding="utf-8"))
    assert latest["status"] == "blocked"
    assert "requires a string slug" in latest["error"]


def test_daemon_rejects_short_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_RUN_INTERVAL_SECONDS", "59")

    assert cli.daemon() == 2


def test_daemon_rejects_non_integer_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_RUN_INTERVAL_SECONDS", "daily")

    assert cli.daemon() == 2


def test_daemon_runs_inventory_before_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    class StopLoop(Exception):
        pass

    calls: list[str] = []
    monkeypatch.setenv("AGENT_RUN_INTERVAL_SECONDS", "60")
    monkeypatch.setattr(cli, "run_inventory", lambda: calls.append("inventory") or 0)
    monkeypatch.setattr(cli.time, "sleep", lambda _interval: (_ for _ in ()).throw(StopLoop()))

    with pytest.raises(StopLoop):
        cli.daemon()
    assert calls == ["inventory"]


@pytest.mark.parametrize("command", ["version", "agents"])
def test_main_handles_non_exiting_commands(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    monkeypatch.setattr("sys.argv", ["repo-agent", command])
    monkeypatch.setattr(cli, "_agent_definitions", lambda: [])

    cli.main()

    assert capsys.readouterr().out


@pytest.mark.parametrize("command", ["health", "run-once"])
def test_main_exits_with_selected_workflow_result(
    monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    monkeypatch.setattr("sys.argv", ["repo-agent", command])
    monkeypatch.setattr(cli, "health", lambda: 0)
    monkeypatch.setattr(cli, "run_inventory", lambda: 0)

    with pytest.raises(SystemExit) as result:
        cli.main()

    assert result.value.code == 0


@pytest.mark.parametrize(
    ("command", "attribute"),
    [
        ("plan-once", "run_planning"),
        ("dispatch-once", "run_team_lead_dispatch"),
        ("daemon", "daemon"),
    ],
)
def test_main_routes_itemless_commands_to_their_stage(
    monkeypatch: pytest.MonkeyPatch, command: str, attribute: str
) -> None:
    """Every stage that takes no work item must reach exactly its own entry point."""
    called: list[str] = []
    monkeypatch.setattr("sys.argv", ["repo-agent", command])
    monkeypatch.setattr(cli, attribute, lambda: called.append(attribute) or 3)

    with pytest.raises(SystemExit) as result:
        cli.main()

    assert called == [attribute]
    assert result.value.code == 3


@pytest.mark.parametrize(
    ("command", "attribute"),
    [
        ("engineer-handoff", "run_engineer_handoff"),
        ("engineer-preflight", "run_engineer_preflight"),
        ("engineer-execute", "run_engineer_execute"),
        ("test-execute", "run_test_execute"),
    ],
)
def test_main_forwards_the_exact_item_to_engineer_stages(
    monkeypatch: pytest.MonkeyPatch, command: str, attribute: str
) -> None:
    """The engineer stages must receive the requested item ID unmodified."""
    received: list[str] = []
    monkeypatch.setattr("sys.argv", ["repo-agent", command, "--item", "alert-42"])
    monkeypatch.setattr(cli, attribute, lambda item: received.append(item) or 0)

    with pytest.raises(SystemExit) as result:
        cli.main()

    assert received == ["alert-42"]
    assert result.value.code == 0


@pytest.mark.parametrize(
    "command", ["engineer-handoff", "engineer-preflight", "engineer-execute", "test-execute"]
)
def test_main_refuses_an_engineer_stage_without_an_item(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    """An engineer stage may never run against an unspecified work item."""
    monkeypatch.setattr("sys.argv", ["repo-agent", command])

    with pytest.raises(SystemExit) as result:
        cli.main()

    assert result.value.code == 2
    assert "requires --item" in capsys.readouterr().err


def _git_command(path: Path, *arguments: str) -> str:
    """Drive the fixture repository directly, outside the code under test."""
    completed = subprocess.run(
        ["git", "-C", str(path), *arguments], check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def _testing_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, gates: dict[str, object]
) -> tuple[Path, Path]:
    """Build a real checkout, bare origin, repository metadata, and state directory."""
    projects = tmp_path / "projects"
    workspace = projects / "repo"
    workspace.mkdir(parents=True)
    _git_command(workspace, "init", "-b", "main")
    _git_command(workspace, "config", "user.email", "agent@example.test")
    _git_command(workspace, "config", "user.name", "Repo Agent")
    (workspace / "marker.txt").write_text("main-head\n", encoding="utf-8")
    (workspace / "coverage.txt").write_text("Total coverage: 95.00%\n", encoding="utf-8")
    _git_command(workspace, "add", "-A")
    _git_command(workspace, "commit", "-m", "seed")
    _git_command(workspace, "clone", "--bare", str(workspace), str(tmp_path / "origin.git"))
    _git_command(workspace, "remote", "add", "origin", str(tmp_path / "origin.git"))

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    info_path = tmp_path / "repo-info.yml"
    info_path.write_text(
        json.dumps(
            {
                "repositories": [
                    {
                        "slug": "owner/repo",
                        "path": str(workspace),
                        "default_branch": "main",
                        "quality_gates": gates,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENT_STATE_DIR", str(state_dir))
    monkeypatch.setenv("AGENT_REPOSITORY_INFO", str(info_path))
    monkeypatch.setenv("ENGINEER_REPOSITORY_ROOT", str(projects))
    return workspace, state_dir


def _write_execution(state_dir: Path, execution: dict[str, object]) -> None:
    (state_dir / "latest-engineer-execution.json").write_text(
        json.dumps(execution), encoding="utf-8"
    )


def test_test_execute_runs_configured_gates_against_an_existing_pull_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An existing PR is verified at its own head, with unconfigured gates recorded as skipped."""
    workspace, state_dir = _testing_environment(
        monkeypatch,
        tmp_path,
        {"test": "cat marker.txt", "coverage": "cat coverage.txt", "minimum_coverage": 90},
    )
    _git_command(workspace, "switch", "-c", "pull-source")
    (workspace / "marker.txt").write_text("pull-request-head\n", encoding="utf-8")
    _git_command(workspace, "commit", "-am", "pull request change")
    _git_command(workspace, "push", str(tmp_path / "origin.git"), "HEAD:refs/pull/7/head")
    _git_command(workspace, "switch", "main")
    _git_command(workspace, "branch", "-D", "pull-source")
    _write_execution(
        state_dir,
        {
            "status": "existing_pull_request_ready_for_testing",
            "requested_item_id": "owner/repo:pr:7",
            "repository": "owner/repo",
            "workspace_path": str(workspace),
            "work_item": {"id": "owner/repo:pr:7", "kind": "open_pull_request"},
            "architect_decision": {"acceptance_criteria": ["Every configured gate passes"]},
            "pull_request_url": "https://github.com/owner/repo/pull/7",
        },
    )

    assert testing.run_test_execute("owner/repo:pr:7") == 0

    report = json.loads((state_dir / "latest-test-report.json").read_text(encoding="utf-8"))
    assert report["status"] == "passed"
    assert report["checkout"]["source"] == "existing_pull_request"
    gates = {gate["gate"]: gate for gate in report["gates"]}
    assert gates["test"]["stdout"] == "pull-request-head\n"
    assert gates["coverage"]["coverage_percent"] == 95.0
    assert gates["coverage"]["minimum_coverage"] == 90
    assert [name for name, gate in gates.items() if gate["status"] == "skipped"] == [
        "bootstrap",
        "format",
        "lint",
        "security",
    ]
    assert report["worktree_removed"] is True
    assert not Path(report["worktree_path"]).exists()
    assert _git_command(workspace, "status", "--porcelain") == ""
    assert _git_command(workspace, "branch", "--show-current") == "main"
    markdown = (state_dir / "latest-test-report.md").read_text(encoding="utf-8")
    assert "Every configured gate passes" in markdown
    assert "`security`: **skipped**" in markdown
    assert json.loads(capsys.readouterr().out)["status"] == "passed"


def test_test_execute_runs_the_engineer_branch_with_its_uncommitted_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A remediation branch is verified as the engineer left it, and one failing gate fails."""
    workspace, state_dir = _testing_environment(
        monkeypatch,
        tmp_path,
        {"test": "cat marker.txt", "lint": "git rev-parse --verify missing-ref"},
    )
    _git_command(workspace, "switch", "-c", "repo-agent/engineer-1")
    (workspace / "marker.txt").write_text("branch-head\n", encoding="utf-8")
    _git_command(workspace, "commit", "-am", "branch change")
    (workspace / "marker.txt").write_text("engineer-uncommitted\n", encoding="utf-8")
    _write_execution(
        state_dir,
        {
            "status": "implementation_applied",
            "requested_item_id": "owner/repo:alert:1",
            "repository": "owner/repo",
            "workspace_path": str(workspace),
            "branch": "repo-agent/engineer-1",
        },
    )
    (state_dir / "latest-engineer-preflight.json").write_text(
        json.dumps(
            {
                "work_item": {"id": "owner/repo:alert:1", "repository": "owner/repo"},
                "architect_decision": {"acceptance_criteria": ["Patch the vulnerable dependency"]},
            }
        ),
        encoding="utf-8",
    )

    assert testing.run_test_execute("owner/repo:alert:1") == 0

    report = json.loads((state_dir / "latest-test-report.json").read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert report["checkout"]["uncommitted_changes_applied"] is True
    assert report["work_item"]["id"] == "owner/repo:alert:1"
    assert report["architect_decision"]["acceptance_criteria"] == [
        "Patch the vulnerable dependency"
    ]
    gates = {gate["gate"]: gate for gate in report["gates"]}
    assert gates["test"]["stdout"] == "engineer-uncommitted\n"
    assert gates["lint"]["status"] == "failed"
    assert gates["lint"]["exit_code"] != 0
    assert report["worktree_removed"] is True
    assert not Path(report["worktree_path"]).exists()


def test_test_execute_removes_the_worktree_when_a_gate_run_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The disposable worktree is created, then discarded even when the stage blocks."""
    workspace, state_dir = _testing_environment(monkeypatch, tmp_path, {"test": "cat marker.txt"})
    _git_command(workspace, "switch", "-c", "repo-agent/engineer-2")
    _write_execution(
        state_dir,
        {
            "status": "implementation_applied",
            "requested_item_id": "owner/repo:alert:2",
            "repository": "owner/repo",
            "workspace_path": str(workspace),
            "branch": "repo-agent/engineer-2",
        },
    )
    observed: list[bool] = []

    def explode(_gates: dict[str, object], worktree: Path) -> list[dict[str, object]]:
        observed.append((worktree / "marker.txt").is_file())
        raise RuntimeError("gate runner exploded")

    monkeypatch.setattr(testing, "_run_gates", explode)

    assert testing.run_test_execute("owner/repo:alert:2") == 1

    report = json.loads((state_dir / "latest-test-report.json").read_text(encoding="utf-8"))
    assert observed == [True]
    assert report["status"] == "blocked"
    assert report["error"] == "gate runner exploded"
    assert report["worktree_removed"] is True
    assert not Path(report["worktree_path"]).exists()
    assert "## Blocker" in (state_dir / "latest-test-report.md").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("execution", "message"),
    [
        ({"requested_item_id": "other", "status": "implementation_applied"}, "does not match"),
        ({"requested_item_id": "item", "status": "blocked"}, "not ready for testing"),
        ({"requested_item_id": "item", "status": "implementation_applied"}, "does not name a repo"),
        ("not an object", "must be a JSON object"),
    ],
)
def test_test_execute_blocks_an_unusable_upstream_artifact(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, execution: object, message: str
) -> None:
    """Only a matching, testable engineer execution may reach a repository checkout."""
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    _write_execution(tmp_path, execution)  # type: ignore[arg-type]

    assert testing.run_test_execute("item") == 1

    report = json.loads((tmp_path / "latest-test-report.json").read_text(encoding="utf-8"))
    assert report["status"] == "blocked"
    assert message in report["error"]


@pytest.mark.parametrize(
    ("gates", "message"),
    [
        ({"minimum_coverage": 90}, "configures no quality gates"),
        (None, "configures no quality gates"),
    ],
)
def test_test_execute_blocks_without_configured_gates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, gates: object, message: str
) -> None:
    """An unconfigured repository must block rather than report an empty pass."""
    workspace, state_dir = _testing_environment(monkeypatch, tmp_path, gates)  # type: ignore[arg-type]
    _write_execution(
        state_dir,
        {
            "status": "implementation_applied",
            "requested_item_id": "owner/repo:alert:3",
            "repository": "owner/repo",
            "workspace_path": str(workspace),
            "branch": "missing",
        },
    )

    assert testing.run_test_execute("owner/repo:alert:3") == 1

    report = json.loads((state_dir / "latest-test-report.json").read_text(encoding="utf-8"))
    assert message in report["error"]


def test_test_execute_blocks_unknown_repositories_and_mismatched_workspaces(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Repository metadata, not the upstream artifact, decides which checkout is tested."""
    workspace, state_dir = _testing_environment(monkeypatch, tmp_path, {"test": "cat marker.txt"})
    execution = {
        "status": "implementation_applied",
        "requested_item_id": "item",
        "repository": "owner/other",
        "workspace_path": str(workspace),
        "branch": "main",
    }
    _write_execution(state_dir, execution)
    assert testing.run_test_execute("item") == 1
    report = json.loads((state_dir / "latest-test-report.json").read_text(encoding="utf-8"))
    assert "not configured for execution" in report["error"]

    execution.update({"repository": "owner/repo", "workspace_path": "/projects/elsewhere"})
    _write_execution(state_dir, execution)
    assert testing.run_test_execute("item") == 1
    report = json.loads((state_dir / "latest-test-report.json").read_text(encoding="utf-8"))
    assert "workspace does not match" in report["error"]


def test_checkout_rejects_unusable_pull_request_urls_and_branches(tmp_path: Path) -> None:
    """Neither Git refspecs nor branch names may be assembled from unvalidated artifact fields."""
    for url in (None, "https://github.com/owner/repo/pull/seven", "file:///etc/passwd"):
        with pytest.raises(RuntimeError, match="pull request URL"):
            testing._pull_request_number(url)
    assert testing._pull_request_number("https://github.com/owner/repo/pull/53/") == "53"

    for branch in (None, ""):
        with pytest.raises(RuntimeError, match="does not record a branch"):
            testing._checkout_branch(tmp_path, tmp_path, branch)


def test_checkout_branch_requires_the_checkout_to_still_be_on_that_branch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Uncommitted engineer work is only trustworthy while the checkout still holds it."""
    monkeypatch.setattr(testing, "_git_output", lambda *_args: "main")
    with pytest.raises(RuntimeError, match="must still be on engineer branch feature"):
        testing._checkout_branch(tmp_path, tmp_path, "feature")


def test_worktree_path_is_contained_and_flattened(monkeypatch: pytest.MonkeyPatch) -> None:
    """The worktree directory name comes from a sanitized slug, never from raw artifact text."""
    monkeypatch.setenv("ENGINEER_REPOSITORY_ROOT", "/projects")
    assert testing._worktree_path("Owner/Repo", "run-1") == Path(
        "/projects/.repo-agent-worktrees/owner-repo/run-1"
    )
    with pytest.raises(RuntimeError, match="worktree directory"):
        testing._worktree_path("../../", "run-1")
    monkeypatch.setenv("ENGINEER_REPOSITORY_ROOT", "projects")
    with pytest.raises(RuntimeError, match="absolute path"):
        testing._worktree_path("owner/repo", "run-1")


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("Required test coverage reached. Total coverage: 91.63%\n", 91.63),
        ("TOTAL   902   51   340   53   92%\n", 92.0),
        ("statements covered: 84.5 %\n", 84.5),
        ("no numbers here\n", None),
    ],
)
def test_parse_coverage_reads_common_reporter_formats(output: str, expected: float | None) -> None:
    assert testing._parse_coverage(output) == expected


def test_record_coverage_fails_below_or_without_a_readable_minimum() -> None:
    """A coverage minimum that is unmet or unreadable is a failure, never an implicit pass."""
    below: dict[str, object] = {"status": "passed"}
    testing._record_coverage(below, "Total coverage: 71.20%", 90)
    assert below["status"] == "failed"
    assert "below the configured minimum" in str(below["error"])

    unreadable: dict[str, object] = {"status": "passed"}
    testing._record_coverage(unreadable, "no percentage", 90)
    assert unreadable["status"] == "failed"
    assert unreadable["coverage_percent"] is None

    unconfigured: dict[str, object] = {"status": "passed"}
    testing._record_coverage(unconfigured, "Total coverage: 12.00%", True)
    assert unconfigured["status"] == "passed"
    assert "minimum_coverage" not in unconfigured


def test_run_gate_bounds_output_and_survives_bad_commands(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A gate that is missing, hanging, or not a command fails alone instead of raising."""
    result, output = testing._run_gate("lint", "", tmp_path, 5)
    assert result == {"gate": "lint", "status": "failed", "error": "lint gate is not a command"}
    assert output == ""

    def timeout(*_args: object, **_kwargs: object) -> None:
        raise TimeoutExpired(["make"], 5)

    monkeypatch.setattr(testing, "run", timeout)
    result, _ = testing._run_gate("test", "make test", tmp_path, 5)
    assert result["status"] == "failed"
    assert "exceeded 5 seconds" in result["error"]

    def missing(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError("no such executable")

    monkeypatch.setattr(testing, "run", missing)
    result, _ = testing._run_gate("test", "make test", tmp_path, 5)
    assert result["status"] == "failed"
    assert "no such executable" in result["error"]

    monkeypatch.setattr(
        testing,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="x" * 5_000, stderr=""),
    )
    result, output = testing._run_gate("test", "make test", tmp_path, 5)
    assert result["status"] == "passed"
    assert result["stdout"].startswith("...truncated...")
    assert len(result["stdout"]) < len(output)


def test_gate_environment_hides_repository_write_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """Operator-configured repository commands must never inherit a GitHub write token."""
    monkeypatch.setenv("GH_TOKEN", "secret")
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    monkeypatch.setenv("PATH", "/usr/bin")
    environment = testing._gate_environment()
    assert "GH_TOKEN" not in environment
    assert "GITHUB_TOKEN" not in environment
    assert environment["PATH"] == "/usr/bin"


def test_gate_timeout_is_configurable_and_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    assert testing._gate_timeout() == 1800
    monkeypatch.setenv("TEST_GATE_TIMEOUT_SECONDS", "60")
    assert testing._gate_timeout() == 60
    monkeypatch.setenv("TEST_GATE_TIMEOUT_SECONDS", "0")
    with pytest.raises(RuntimeError, match="TEST_GATE_TIMEOUT_SECONDS"):
        testing._gate_timeout()


def test_preflight_details_only_supplies_a_matching_item(tmp_path: Path) -> None:
    """A stale or unrelated preflight may not supply acceptance criteria for another item."""
    assert testing._preflight_details(tmp_path, "item") == {}
    path = tmp_path / "latest-engineer-preflight.json"
    path.write_text(json.dumps(["not an object"]), encoding="utf-8")
    assert testing._preflight_details(tmp_path, "item") == {}
    path.write_text(json.dumps({"work_item": {"id": "other"}}), encoding="utf-8")
    assert testing._preflight_details(tmp_path, "item") == {}
    path.write_text(json.dumps({"work_item": {"id": "item"}}), encoding="utf-8")
    assert testing._preflight_details(tmp_path, "item") == {"work_item": {"id": "item"}}


def test_remove_worktree_falls_back_to_deleting_the_directory(tmp_path: Path) -> None:
    """Teardown may not depend on Git succeeding; the disposable directory must always go."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "file.txt").write_text("x", encoding="utf-8")
    assert testing._remove_worktree(tmp_path / "missing-workspace", worktree) is True
    assert not worktree.exists()


def test_test_markdown_omits_an_empty_acceptance_criteria_section() -> None:
    """A report without criteria must not render an empty heading a reviewer would trust."""
    rendered = testing._test_markdown(
        {
            "status": "passed",
            "execution_path": "/state/latest-engineer-execution.json",
            "requested_item_id": "item",
            "checkout": {"source": "engineer_branch", "head_commit": "abc"},
            "worktree_removed": True,
            "gates": [{"gate": "test", "status": "passed", "exit_code": 0}],
        }
    )
    assert "Acceptance criteria" not in rendered
    assert "`test`: **passed** — exit code 0" in rendered
