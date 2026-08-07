from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from pathlib import Path
from subprocess import CalledProcessError
from types import SimpleNamespace
from urllib.error import URLError
from urllib.request import Request

import pytest

from repo_agent import cli, engineering, planning


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


@pytest.mark.parametrize("command", ["engineer-handoff", "engineer-preflight", "engineer-execute"])
def test_main_refuses_an_engineer_stage_without_an_item(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    """An engineer stage may never run against an unspecified work item."""
    monkeypatch.setattr("sys.argv", ["repo-agent", command])

    with pytest.raises(SystemExit) as result:
        cli.main()

    assert result.value.code == 2
    assert "requires --item" in capsys.readouterr().err
