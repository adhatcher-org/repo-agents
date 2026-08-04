from __future__ import annotations

import io
import json
from pathlib import Path
from subprocess import CalledProcessError
from types import SimpleNamespace
from urllib.error import URLError
from urllib.request import Request

import pytest

from repo_agent import cli, planning


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
