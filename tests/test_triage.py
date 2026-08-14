"""Tests for deterministic Dependabot pull-request triage."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from repo_agent import triage

OID = "a" * 40
NOW = datetime(2026, 8, 14, tzinfo=UTC)
REQUIRED = [{"context": "ci / Test and build", "app_id": 15368}]


def _commit(
    message: str = (
        "updated-dependencies:\n- dependency-name: requests\n"
        "  update-type: version-update:semver-patch"
    ),
    *,
    status: str = "COMPLETED",
    conclusion: str = "SUCCESS",
    committed: str = "2026-08-14T00:00:00Z",
) -> dict[str, Any]:
    return {
        "oid": OID,
        "messageBody": message,
        "committedDate": committed,
        "signature": {"isValid": True, "wasSignedByGitHub": True},
        "authors": {"nodes": [{"user": {"login": "dependabot[bot]"}}]},
        "statusCheckRollup": {
            "contexts": {
                "totalCount": 1,
                "nodes": [
                    {
                        "__typename": "CheckRun",
                        "name": "ci / Test and build",
                        "status": status,
                        "conclusion": conclusion,
                        "checkSuite": {"app": {"databaseId": 15368, "slug": "github-actions"}},
                    }
                ],
            }
        },
    }


def _pull_request(**changes: Any) -> dict[str, Any]:
    commit = _commit()
    result: dict[str, Any] = {
        "id": "PR_kwDOA-123",
        "number": 4,
        "title": "Bump requests",
        "url": "https://github.com/acme/repo/pull/4",
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "baseRefName": "main",
        "headRefName": "dependabot/pip/requests-2.0",
        "author": {"login": "dependabot[bot]"},
        "prCommits": {"totalCount": 1, "nodes": [{"commit": commit}]},
        "headCommit": {"nodes": [{"commit": commit}]},
    }
    result.update(changes)
    return result


def _facts(**changes: Any) -> dict[str, Any]:
    result = {
        "repository": "acme/repo",
        "default_branch": "main",
        "archived": False,
        "auto_merge_allowed": True,
        "squashMergeAllowed": True,
        "mergeCommitAllowed": True,
        "rebaseMergeAllowed": True,
    }
    result.update(changes)
    return result


def _policy(**changes: Any) -> dict[str, Any]:
    result = {
        "required_checks": REQUIRED,
        "merge_method": "SQUASH",
        "max_rebase_attempts": 3,
        "source": "defaults",
    }
    result.update(changes)
    return result


def test_text_and_dependency_helpers_bound_untrusted_values() -> None:
    assert triage._safe_text(None) == ""
    assert triage._safe_text(" a\x00b ") == "a b"
    assert triage._safe_text("abcdef", 3) == "abc..."
    assert triage._normalized_login("App/Dependabot[bot]") == "dependabot"
    assert triage._normalized_login(None) == ""
    assert triage._unquoted(" '@scope/pkg' ") == "@scope/pkg"
    updates = triage._dependency_updates(
        "noise\nupdated-dependencies:\n- dependency-name: 'requests'\n"
        "  dependency-type: direct:production\n"
        "  update-type: version-update:semver-minor\nend"
    )
    assert updates == [
        {
            "dependency-name": "requests",
            "dependency-type": "direct:production",
            "update-type": "version-update:semver-minor",
        }
    ]
    assert triage._dependency_updates(None) == []
    assert triage._highest_severity(updates) == 2
    assert triage._highest_severity([]) == triage._UNRECOGNISED_SEVERITY


def test_dependency_metadata_has_a_hard_entry_limit() -> None:
    message = "updated-dependencies:\n" + "\n".join(
        f"- dependency-name: package-{number}" for number in range(201)
    )
    with pytest.raises(RuntimeError, match="more updates"):
        triage._dependency_updates(message)


@pytest.mark.parametrize(
    "value", [[], "bad", [{"context": "", "app_id": 1}], [{"context": "ok", "app_id": True}]]
)
def test_required_check_config_rejects_invalid_values(value: object) -> None:
    with pytest.raises(RuntimeError):
        triage._required_check_config(value)


def test_required_check_config_defaults_and_valid_entry() -> None:
    assert triage._required_check_config(None) == [
        dict(item) for item in triage._DEFAULT_REQUIRED_CHECKS
    ]
    assert triage._required_check_config(REQUIRED) == REQUIRED


def test_triage_policy_defaults_and_repository_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    info = tmp_path / "repo-info.yml"
    monkeypatch.setattr(triage, "_repository_info_path", lambda: info)
    assert triage._triage_policy("acme/repo")["source"] == "defaults"
    info.write_text("configured", encoding="utf-8")
    monkeypatch.setattr(
        triage,
        "_load_repository_info",
        lambda: {
            "acme/repo": {
                "pr_triage": {
                    "required_checks": REQUIRED,
                    "merge_method": "rebase",
                    "max_rebase_attempts": 2,
                }
            }
        },
    )
    policy = triage._triage_policy("acme/repo")
    assert policy["merge_method"] == "REBASE"
    assert policy["max_rebase_attempts"] == 2


@pytest.mark.parametrize(
    "configured", ["bad", {"merge_method": "fast"}, {"max_rebase_attempts": 0}]
)
def test_triage_policy_rejects_invalid_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, configured: object
) -> None:
    info = tmp_path / "repo-info.yml"
    info.write_text("configured", encoding="utf-8")
    monkeypatch.setattr(triage, "_repository_info_path", lambda: info)
    monkeypatch.setattr(
        triage, "_load_repository_info", lambda: {"acme/repo": {"pr_triage": configured}}
    )
    with pytest.raises(RuntimeError):
        triage._triage_policy("acme/repo")


def test_fetch_facts_and_commit_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    node = {"defaultBranchRef": {"name": "main"}, "isArchived": False, "autoMergeAllowed": True}
    monkeypatch.setattr(triage, "_graphql", lambda *_: {"repository": node})
    assert triage._fetch_repository("acme/repo", {}) == node
    assert triage._repository_facts("acme/repo", node)["default_branch"] == "main"
    assert triage._commit_nodes({"x": {"nodes": [{"commit": {"oid": OID}}, {}]}}, "x") == [
        {"oid": OID}
    ]
    with pytest.raises(RuntimeError, match="default branch"):
        triage._repository_facts("acme/repo", {})


@pytest.mark.parametrize(
    ("change", "route", "reason"),
    [
        ({"headRefName": "repo-agent/task"}, triage._ROUTE_REPORT, "agent_authored"),
        ({"author": {"login": "person"}}, triage._ROUTE_REPORT, "not_dependabot_authored"),
        (
            {"prCommits": {"totalCount": 0, "nodes": []}},
            triage._ROUTE_ESCALATE,
            "no_commits_reported",
        ),
        ({"isDraft": True}, triage._ROUTE_REPORT, "draft"),
        ({"baseRefName": "release"}, triage._ROUTE_ESCALATE, "unexpected_base_branch"),
        (
            {"prCommits": {"totalCount": 1, "nodes": [{"commit": _commit("nothing")}]}},
            triage._ROUTE_ESCALATE,
            "dependency_metadata_unavailable",
        ),
        (
            {
                "prCommits": {
                    "totalCount": 1,
                    "nodes": [
                        {
                            "commit": _commit(
                                "updated-dependencies:\n- dependency-name: x\n"
                                "  update-type: version-update:semver-major"
                            )
                        }
                    ],
                }
            },
            triage._ROUTE_ESCALATE,
            "update_outside_policy",
        ),
        ({"mergeable": "CONFLICTING"}, triage._ROUTE_COMMENT, "merge_conflict"),
        ({"mergeStateStatus": "BEHIND"}, triage._ROUTE_COMMENT, "head_behind_base"),
        ({"mergeable": "UNKNOWN"}, triage._ROUTE_REQUEUE, "awaiting_github"),
    ],
)
def test_route_pull_request_safeguards(change: dict[str, Any], route: str, reason: str) -> None:
    decision = triage._route_pull_request(_pull_request(**change), _facts(), _policy(), {}, NOW, 24)
    assert (decision["route"], decision["reason"]) == (route, reason)


def test_route_pull_request_covers_checks_stalled_and_merge_capability() -> None:
    pr = _pull_request()
    assert (
        triage._route_pull_request(pr, _facts(auto_merge_allowed=False), _policy(), {}, NOW, 24)[
            "reason"
        ]
        == "auto_merge_disabled"
    )
    assert (
        triage._route_pull_request(pr, _facts(squashMergeAllowed=False), _policy(), {}, NOW, 24)[
            "reason"
        ]
        == "merge_method_not_allowed"
    )
    assert (
        triage._route_pull_request(pr, _facts(), _policy(), {}, NOW, 24)["route"]
        == triage._ROUTE_APPROVE
    )
    failed = _pull_request(headCommit={"nodes": [{"commit": _commit(conclusion="FAILURE")}]})
    assert (
        triage._route_pull_request(failed, _facts(), _policy(), {}, NOW, 24)["reason"]
        == "required_check_failed"
    )
    missing = _pull_request(headCommit={"nodes": [{"commit": _commit()}]})
    missing["headCommit"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]["nodes"] = []
    assert (
        triage._route_pull_request(missing, _facts(), _policy(), {}, NOW, 24)["reason"]
        == "required_check_missing"
    )
    pending = _pull_request(headCommit={"nodes": [{"commit": _commit(status="IN_PROGRESS")}]})
    assert (
        triage._route_pull_request(pending, _facts(), _policy(), {}, NOW, 24)["route"]
        == triage._ROUTE_REQUEUE
    )
    stale = _pull_request(
        headCommit={
            "nodes": [{"commit": _commit(status="IN_PROGRESS", committed="2020-01-01T00:00:00Z")}]
        }
    )
    assert (
        triage._route_pull_request(stale, _facts(), _policy(), {}, NOW, 24)["reason"]
        == "stalled_waiting_for_github"
    )
    assert (
        triage._route_pull_request(
            _pull_request(mergeable="CONFLICTING"),
            _facts(),
            _policy(),
            {"rebase_attempts": 3},
            NOW,
            24,
        )["reason"]
        == "rebase_attempts_exhausted"
    )


def test_rollup_check_state_and_head_age() -> None:
    contexts, truncated = triage._rollup_contexts(_pull_request())
    assert (
        not truncated and triage._required_check_state(contexts, REQUIRED)["state"] == "satisfied"
    )
    assert triage._required_check_state([], REQUIRED)["state"] == "missing"
    assert triage._head_age_exceeded(_pull_request(), NOW + timedelta(days=1), 1)
    assert not triage._head_age_exceeded({"headCommit": {"nodes": []}}, NOW, 1)


def test_history_summary_mutations_and_actions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    summary = triage._pull_request_summary("acme/repo", _pull_request())
    assert summary["head_oid"] == OID
    (tmp_path / "latest-pr-triage.json").write_text(
        json.dumps({"pull_requests": [{**summary, "rebase_attempts": 1}]}), encoding="utf-8"
    )
    history = triage._previous_history(tmp_path)
    assert triage._history_for(summary, history) == {"rebase_attempts": 1}
    assert triage._history_for({**summary, "head_oid": "b" * 40}, history) == {"rebase_attempts": 0}
    calls: list[dict[str, str]] = []
    monkeypatch.setattr(
        triage, "_graphql", lambda _doc, variables, _env: calls.append(variables) or {}
    )
    actions = triage._act({"route": triage._ROUTE_APPROVE}, summary, _policy(), {})
    assert [item["action"] for item in actions] == ["enable_auto_merge", "approve"]
    assert len(calls) == 2
    assert (
        triage._act({"route": triage._ROUTE_COMMENT}, summary, _policy(), {})[0]["action"]
        == "comment_dependabot_rebase"
    )
    assert (
        triage._act(
            {"route": triage._ROUTE_APPROVE}, {**summary, "node_id": "bad space"}, _policy(), {}
        )[0]["action"]
        == "abort"
    )


def test_notify_configuration_and_delivery(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    escalation = triage._escalation(
        {"repository": "acme/repo", "number": 4, "url": "url", "title": "title"},
        {"reason": "reason", "summary": "detail", "evidence": {}},
        "now",
    )
    assert triage._notify([], False)["status"] == "nothing_to_notify"
    assert triage._notify([escalation], False)["status"] == "dry_run"
    assert not triage._telegram_configuration()["configured"]
    token = tmp_path / "token"
    token.write_text("secret", encoding="utf-8")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_FILE", str(token))
    monkeypatch.setattr(triage, "_send_telegram", lambda *_: {"delivered": True})
    assert triage._notify([escalation], True)["status"] == "sent"
    assert "repo-agent escalation" in triage._escalation_message(escalation)


def test_inventory_triage_repository_and_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    inventory = tmp_path / "latest-inventory.json"
    inventory.write_text(
        json.dumps({"status": "passed", "repositories": [{"repository": "acme/repo"}]}),
        encoding="utf-8",
    )
    assert triage._inventory_repositories(inventory) == ["acme/repo"]
    node = {
        "isArchived": False,
        "autoMergeAllowed": True,
        "squashMergeAllowed": True,
        "mergeCommitAllowed": True,
        "rebaseMergeAllowed": True,
        "defaultBranchRef": {"name": "main"},
        "pullRequests": {"nodes": [_pull_request()]},
    }
    monkeypatch.setattr(triage, "_fetch_repository", lambda *_: node)
    monkeypatch.setattr(triage, "_triage_policy", lambda *_: _policy())
    results = triage._triage_repository("acme/repo", {}, {}, NOW, 24, False)
    assert results[0]["actions"][0]["action"] == "would_enable_auto_merge_then_approve"
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(triage, "_github_environment", lambda *_: {})
    assert triage.run_pr_triage() == 0
    report = json.loads((tmp_path / "latest-pr-triage.json").read_text(encoding="utf-8"))
    assert report["status"] == "passed"
