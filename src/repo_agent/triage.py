"""Route open Dependabot pull requests from GitHub state and policy alone; no clone, no model."""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from repo_agent.engineering import (
    _load_repository_info,
    _repository_info_path,
    _write_json_atomic,
)
from repo_agent.github import _github_environment, _graphql, repository_parts
from repo_agent.planning import _positive_integer

# Routes. Only `approve_and_enable_auto_merge` and `comment_rebase` ever act on GitHub, and only
# when the operator passed --apply. There is deliberately no merge route: Flow A enables GitHub's
# own auto-merge and lets GitHub perform the merge when the required checks pass.
_ROUTE_APPROVE = "approve_and_enable_auto_merge"
_ROUTE_COMMENT = "comment_rebase"
_ROUTE_ESCALATE = "escalate"
_ROUTE_REQUEUE = "requeue"
_ROUTE_REPORT = "report_only"

_AGENT_BRANCH_PREFIX = "repo-agent/"
_DEPENDABOT_LOGIN = "dependabot"
_REBASE_COMMENT = "@dependabot rebase"

# Step 1 established one canonical required context across the org. It is the default rather than a
# constant so a repository can declare its own in repo-info.yml without a code change.
_DEFAULT_REQUIRED_CHECKS: tuple[dict[str, Any], ...] = (
    {"context": "ci / Test and build", "app_id": 15368},
)
_DEFAULT_MERGE_METHOD = "SQUASH"
_DEFAULT_MAX_REBASE_ATTEMPTS = 3
_MERGE_METHODS = {"squash": "SQUASH", "merge": "MERGE", "rebase": "REBASE"}
_MERGE_METHOD_ALLOWED_FIELD = {
    "SQUASH": "squashMergeAllowed",
    "MERGE": "mergeCommitAllowed",
    "REBASE": "rebaseMergeAllowed",
}

# The three-by-three matrix in docs/Initial_use_cases.md collapses to one severity comparison:
# patch and minor merge, every major escalates, regardless of dependency type. An update type this
# stage does not recognise is ranked above major so it can never be merged by accident.
_UPDATE_TYPE_SEVERITY = {
    "version-update:semver-patch": 1,
    "version-update:semver-minor": 2,
    "version-update:semver-major": 3,
}
_SEVERITY_NAMES = {1: "patch", 2: "minor", 3: "major", 4: "unrecognised"}
_UNRECOGNISED_SEVERITY = 4
_MERGE_SEVERITY_LIMIT = 2

_CONFLICTED_MERGEABLE = "CONFLICTING"
_CONFLICTED_MERGE_STATES = frozenset({"DIRTY"})
_STALE_MERGE_STATES = frozenset({"BEHIND"})

_UPDATED_DEPENDENCIES_HEADER = "updated-dependencies:"
_ENTRY_START = re.compile(r"^-\s+dependency-name:\s*(?P<value>\S.*)$")
_ENTRY_FIELD = re.compile(
    r"^\s+(?P<key>dependency-name|dependency-version|dependency-type|update-type"
    r"|dependency-group):\s*(?P<value>\S.*)$"
)
_MAX_DEPENDENCY_ENTRIES = 200
_MAX_FIELD_LENGTH = 200
_MAX_TITLE_LENGTH = 200
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

_NODE_ID = re.compile(r"^[A-Za-z0-9_=-]{1,200}$")
_OBJECT_ID = re.compile(r"^[0-9a-f]{40}$")

_MAX_NOTIFICATIONS = 20
_MAX_MESSAGE_LENGTH = 3_500
_TELEGRAM_API = "https://api.telegram.org"

_REPOSITORY_QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    nameWithOwner
    isArchived
    autoMergeAllowed
    squashMergeAllowed
    mergeCommitAllowed
    rebaseMergeAllowed
    defaultBranchRef { name }
    pullRequests(states: OPEN, first: 50, orderBy: {field: CREATED_AT, direction: ASC}) {
      totalCount
      nodes {
        id
        number
        title
        url
        isDraft
        mergeable
        mergeStateStatus
        baseRefName
        headRefName
        author { __typename login }
        prCommits: commits(last: 20) {
          totalCount
          nodes {
            commit {
              oid
              messageBody
              signature { isValid wasSignedByGitHub }
              authors(first: 3) { nodes { name user { login } } }
            }
          }
        }
        headCommit: commits(last: 1) {
          nodes {
            commit {
              oid
              committedDate
              statusCheckRollup {
                state
                contexts(first: 50) {
                  totalCount
                  nodes {
                    __typename
                    ... on CheckRun {
                      name
                      status
                      conclusion
                      checkSuite { app { databaseId slug } }
                    }
                    ... on StatusContext { context state }
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""

_COMMENT_MUTATION = """
mutation($pullRequestId: ID!) {
  addComment(input: {subjectId: $pullRequestId, body: "@dependabot rebase"}) {
    clientMutationId
  }
}
"""

# expectedHeadOid makes GitHub reject the mutation if the pull request moved after it was judged,
# so a rebase between the read and the write cannot be auto-merged unreviewed.
_AUTO_MERGE_MUTATION = """
mutation($pullRequestId: ID!, $expectedHeadOid: GitObjectID!,
         $mergeMethod: PullRequestMergeMethod!) {
  enablePullRequestAutoMerge(input: {pullRequestId: $pullRequestId,
                                     expectedHeadOid: $expectedHeadOid,
                                     mergeMethod: $mergeMethod}) {
    pullRequest { number autoMergeRequest { enabledAt mergeMethod } }
  }
}
"""

_APPROVE_MUTATION = """
mutation($pullRequestId: ID!, $commitOID: GitObjectID!) {
  addPullRequestReview(input: {pullRequestId: $pullRequestId, commitOID: $commitOID,
                               event: APPROVE}) {
    pullRequestReview { state }
  }
}
"""


def _safe_text(value: object, limit: int = _MAX_FIELD_LENGTH) -> str:
    """Bound and de-control repository-authored text before it reaches a report or a message."""
    if not isinstance(value, str):
        return ""
    cleaned = _CONTROL_CHARACTERS.sub(" ", value).strip()
    return cleaned if len(cleaned) <= limit else cleaned[:limit] + "..."


def _normalized_login(value: object) -> str:
    """Collapse the three spellings GitHub uses for one bot into a single comparable name."""
    if not isinstance(value, str):
        return ""
    login = value.strip().lower()
    if login.startswith("app/"):
        login = login[4:]
    if login.endswith("[bot]"):
        login = login[:-5]
    return login


def _unquoted(value: str) -> str:
    """Strip the quoting Dependabot applies to scoped package names without accepting new text."""
    text = value.strip()
    if len(text) >= 2 and text[:1] == text[-1:] and text[:1] in {'"', "'"}:
        text = text[1:-1]
    return _safe_text(text)


def _dependency_updates(message: object) -> list[dict[str, str]]:
    """Read Dependabot's own structured trailer; the pull request title is never parsed."""
    if not isinstance(message, str):
        return []
    entries: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    in_block = False
    for raw in message.splitlines():
        line = raw.rstrip()
        if not in_block:
            in_block = line.strip() == _UPDATED_DEPENDENCIES_HEADER
            continue
        start = _ENTRY_START.match(line)
        if start:
            if len(entries) >= _MAX_DEPENDENCY_ENTRIES:
                raise RuntimeError("dependency metadata lists more updates than this stage accepts")
            current = {"dependency-name": _unquoted(start.group("value"))}
            entries.append(current)
            continue
        field = _ENTRY_FIELD.match(line)
        if field is not None and current is not None:
            current[field.group("key")] = _unquoted(field.group("value"))
            continue
        in_block = False
        current = None
    return entries


def _highest_severity(entries: list[dict[str, str]]) -> int:
    """Evaluate a grouped update at its most severe member so one major escalates the whole group."""
    return max(
        (_UPDATE_TYPE_SEVERITY.get(entry.get("update-type", ""), _UNRECOGNISED_SEVERITY)
         for entry in entries),
        default=_UNRECOGNISED_SEVERITY,
    )


def _required_check_config(value: object) -> list[dict[str, Any]]:
    """Refuse a malformed required-check list rather than silently triaging against no gate."""
    if value is None:
        return [dict(check) for check in _DEFAULT_REQUIRED_CHECKS]
    if not isinstance(value, list) or not value:
        raise RuntimeError("pr_triage.required_checks must be a non-empty list")
    checks: list[dict[str, Any]] = []
    for entry in value:
        if not isinstance(entry, dict):
            raise RuntimeError("every pr_triage.required_checks entry must be a mapping")
        context = entry.get("context")
        app_id = entry.get("app_id")
        if not isinstance(context, str) or not context.strip():
            raise RuntimeError("every required check needs a non-empty context string")
        if isinstance(app_id, bool) or not isinstance(app_id, int) or app_id <= 0:
            raise RuntimeError(f"required check {context} needs a positive integer app_id")
        checks.append({"context": context, "app_id": app_id})
    return checks


def _triage_policy(repository: str) -> dict[str, Any]:
    """Apply the org-wide defaults unless this repository overrides them; never guess on bad input."""
    policy: dict[str, Any] = {
        "required_checks": [dict(check) for check in _DEFAULT_REQUIRED_CHECKS],
        "merge_method": _DEFAULT_MERGE_METHOD,
        "max_rebase_attempts": _DEFAULT_MAX_REBASE_ATTEMPTS,
        "source": "defaults",
    }
    if not _repository_info_path().is_file():
        return policy
    metadata = _load_repository_info().get(repository)
    configured = metadata.get("pr_triage") if isinstance(metadata, dict) else None
    if configured is None:
        return policy
    if not isinstance(configured, dict):
        raise RuntimeError(f"pr_triage metadata must be a mapping: {repository}")
    policy["source"] = str(_repository_info_path())
    policy["required_checks"] = _required_check_config(configured.get("required_checks"))
    method = configured.get("merge_method", "squash")
    if not isinstance(method, str) or method.strip().lower() not in _MERGE_METHODS:
        raise RuntimeError(f"pr_triage.merge_method must be squash, merge, or rebase: {repository}")
    policy["merge_method"] = _MERGE_METHODS[method.strip().lower()]
    attempts = configured.get("max_rebase_attempts", _DEFAULT_MAX_REBASE_ATTEMPTS)
    if isinstance(attempts, bool) or not isinstance(attempts, int) or not 1 <= attempts <= 10:
        raise RuntimeError(f"pr_triage.max_rebase_attempts must be 1-10: {repository}")
    policy["max_rebase_attempts"] = attempts
    return policy


def _pending_hours() -> int:
    """Bound how long a requeued pull request may sit before it becomes a human's problem."""
    return _positive_integer(
        os.environ.get("PR_TRIAGE_PENDING_HOURS", "24"), "PR_TRIAGE_PENDING_HOURS"
    )


def _fetch_repository(repository: str, environment: dict[str, str]) -> dict[str, Any]:
    """Read one repository's live pull-request state; the inventory artifact is never acted on."""
    owner, name = repository_parts(repository)
    data = _graphql(_REPOSITORY_QUERY, {"owner": owner, "name": name}, environment)
    node = data.get("repository")
    if not isinstance(node, dict):
        raise RuntimeError(f"GitHub returned no repository for {repository}")
    return node


def _repository_facts(repository: str, node: dict[str, Any]) -> dict[str, Any]:
    """Derive the default branch and merge capabilities from GitHub, never from an assumption."""
    default_ref = node.get("defaultBranchRef")
    default_branch = default_ref.get("name") if isinstance(default_ref, dict) else None
    if not isinstance(default_branch, str) or not default_branch:
        raise RuntimeError(f"GitHub did not report a default branch for {repository}")
    return {
        "repository": repository,
        "default_branch": default_branch,
        "archived": bool(node.get("isArchived")),
        "auto_merge_allowed": bool(node.get("autoMergeAllowed")),
        "squashMergeAllowed": bool(node.get("squashMergeAllowed")),
        "mergeCommitAllowed": bool(node.get("mergeCommitAllowed")),
        "rebaseMergeAllowed": bool(node.get("rebaseMergeAllowed")),
    }


def _commit_nodes(pull_request: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """Return the commit objects for one aliased connection, tolerating a missing GitHub field."""
    connection = pull_request.get(key)
    nodes = connection.get("nodes") if isinstance(connection, dict) else None
    if not isinstance(nodes, list):
        return []
    return [node["commit"] for node in nodes if isinstance(node, dict) and isinstance(node.get("commit"), dict)]


def _author_check(pull_request: dict[str, Any]) -> tuple[bool, str]:
    """Accept only a Dependabot-authored pull request whose every commit is Dependabot's and signed."""
    author = pull_request.get("author")
    login = _normalized_login(author.get("login") if isinstance(author, dict) else None)
    if login != _DEPENDABOT_LOGIN:
        return False, "not_dependabot_authored"
    commits = _commit_nodes(pull_request, "prCommits")
    if not commits:
        return False, "no_commits_reported"
    connection = pull_request.get("prCommits")
    total = connection.get("totalCount") if isinstance(connection, dict) else None
    if not isinstance(total, int) or total > len(commits):
        return False, "commit_history_not_fully_read"
    for commit in commits:
        authors = commit.get("authors")
        nodes = authors.get("nodes") if isinstance(authors, dict) else None
        logins = {
            _normalized_login((node.get("user") or {}).get("login"))
            for node in (nodes or [])
            if isinstance(node, dict)
        }
        if logins != {_DEPENDABOT_LOGIN}:
            return False, "commit_not_authored_by_dependabot"
        signature = commit.get("signature")
        if not isinstance(signature, dict) or not (
            signature.get("isValid") and signature.get("wasSignedByGitHub")
        ):
            return False, "commit_not_signed_by_github"
    return True, "dependabot_authored"


def _rollup_contexts(pull_request: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    """Read the head commit's check contexts; an unreadable rollup yields no contexts, not a pass."""
    commits = _commit_nodes(pull_request, "headCommit")
    if not commits:
        return [], False
    rollup = commits[0].get("statusCheckRollup")
    contexts = rollup.get("contexts") if isinstance(rollup, dict) else None
    nodes = contexts.get("nodes") if isinstance(contexts, dict) else None
    total = contexts.get("totalCount") if isinstance(contexts, dict) else None
    listed = [node for node in (nodes or []) if isinstance(node, dict)]
    truncated = isinstance(total, int) and total > len(listed)
    return listed, truncated


def _required_check_state(
    contexts: list[dict[str, Any]], required: list[dict[str, Any]]
) -> dict[str, Any]:
    """Assert every required context is present, from the expected app, and green.

    An absent check is never a pass: a repository with no CI reports zero contexts, and an
    absence-of-failure test would auto-merge it unverified.
    """
    missing: list[str] = []
    failed: list[str] = []
    pending: list[str] = []
    satisfied: list[str] = []
    for check in required:
        matches = [
            context
            for context in contexts
            if context.get("__typename") == "CheckRun"
            and context.get("name") == check["context"]
            and isinstance(context.get("checkSuite"), dict)
            and isinstance(context["checkSuite"].get("app"), dict)
            and context["checkSuite"]["app"].get("databaseId") == check["app_id"]
        ]
        label = f"{check['context']} (app {check['app_id']})"
        if not matches:
            missing.append(label)
        elif any(match.get("status") != "COMPLETED" for match in matches):
            pending.append(label)
        elif any(match.get("conclusion") != "SUCCESS" for match in matches):
            failed.append(label)
        else:
            satisfied.append(label)
    if missing:
        state = "missing"
    elif failed:
        state = "failed"
    elif pending:
        state = "pending"
    else:
        state = "satisfied"
    return {
        "state": state,
        "missing": missing,
        "failed": failed,
        "pending": pending,
        "satisfied": satisfied,
        "observed_contexts": sorted(
            {
                _safe_text(context.get("name") or context.get("context"))
                for context in contexts
                if context.get("name") or context.get("context")
            }
        ),
    }


def _head_age_exceeded(pull_request: dict[str, Any], now: datetime, hours: int) -> bool:
    """Stop a pull request from being requeued forever when its checks never resolve."""
    commits = _commit_nodes(pull_request, "headCommit")
    committed = commits[0].get("committedDate") if commits else None
    if not isinstance(committed, str):
        return False
    try:
        timestamp = datetime.fromisoformat(committed.replace("Z", "+00:00"))
    except ValueError:
        return False
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return now - timestamp > timedelta(hours=hours)


def _decision(route: str, reason: str, summary: str, **evidence: Any) -> dict[str, Any]:
    """Package one route with the evidence a human needs to check it without opening GitHub."""
    return {"route": route, "reason": reason, "summary": summary, "evidence": evidence}


def _route_pull_request(
    pull_request: dict[str, Any],
    facts: dict[str, Any],
    policy: dict[str, Any],
    history: dict[str, Any],
    now: datetime,
    pending_hours: int,
) -> dict[str, Any]:
    """Decide one pull request's route deterministically; this function performs no GitHub call."""
    head_ref = pull_request.get("headRefName")
    if isinstance(head_ref, str) and head_ref.startswith(_AGENT_BRANCH_PREFIX):
        return _decision(
            _ROUTE_REPORT,
            "agent_authored",
            "Authored by this pipeline; reported only and never auto-merged.",
        )
    authored, author_reason = _author_check(pull_request)
    if not authored:
        if author_reason == "not_dependabot_authored":
            return _decision(
                _ROUTE_REPORT, author_reason, "Not a Dependabot pull request; left for a human."
            )
        return _decision(
            _ROUTE_ESCALATE,
            author_reason,
            "Dependabot opened this pull request but its commits are not Dependabot's own.",
        )
    if pull_request.get("isDraft"):
        return _decision(_ROUTE_REPORT, "draft", "Draft pull request; no action taken.")

    base = pull_request.get("baseRefName")
    if base != facts["default_branch"]:
        return _decision(
            _ROUTE_ESCALATE,
            "unexpected_base_branch",
            f"Targets {_safe_text(base)} rather than the default branch {facts['default_branch']}.",
            base_ref=_safe_text(base),
            default_branch=facts["default_branch"],
        )

    entries = _dependency_updates(
        "\n".join(
            str(commit.get("messageBody", ""))
            for commit in _commit_nodes(pull_request, "prCommits")
        )
    )
    severity = _highest_severity(entries)
    updates = [
        {
            "name": entry.get("dependency-name", ""),
            "type": entry.get("dependency-type", ""),
            "update": entry.get("update-type", ""),
            "group": entry.get("dependency-group", ""),
        }
        for entry in entries
    ]
    if not entries:
        return _decision(
            _ROUTE_ESCALATE,
            "dependency_metadata_unavailable",
            "No updated-dependencies trailer could be read, so the bump cannot be classified.",
        )
    if severity > _MERGE_SEVERITY_LIMIT:
        return _decision(
            _ROUTE_ESCALATE,
            "update_outside_policy",
            f"Highest member of this update is {_SEVERITY_NAMES[severity]}; every major escalates.",
            severity=_SEVERITY_NAMES[severity],
            updates=updates,
        )

    merge_state = pull_request.get("mergeStateStatus")
    mergeable = pull_request.get("mergeable")
    conflicted = mergeable == _CONFLICTED_MERGEABLE or merge_state in _CONFLICTED_MERGE_STATES
    if conflicted or merge_state in _STALE_MERGE_STATES:
        attempts = history.get("rebase_attempts", 0)
        reason = "merge_conflict" if conflicted else "head_behind_base"
        if attempts >= policy["max_rebase_attempts"]:
            return _decision(
                _ROUTE_ESCALATE,
                "rebase_attempts_exhausted",
                f"Asked Dependabot to rebase {attempts} times without reaching a mergeable state.",
                rebase_attempts=attempts,
                merge_state_status=_safe_text(merge_state),
            )
        return _decision(
            _ROUTE_COMMENT,
            reason,
            "Asking Dependabot to rebase; the result is judged on the next cycle.",
            rebase_attempts=attempts,
            merge_state_status=_safe_text(merge_state),
            mergeable=_safe_text(mergeable),
        )

    contexts, truncated = _rollup_contexts(pull_request)
    checks = _required_check_state(contexts, policy["required_checks"])
    checks["contexts_truncated"] = truncated
    if checks["state"] == "missing":
        return _decision(
            _ROUTE_ESCALATE,
            "required_check_missing",
            "A required check is absent from the rollup; an empty result is never a pass.",
            checks=checks,
            severity=_SEVERITY_NAMES[severity],
        )
    if checks["state"] == "failed":
        return _decision(
            _ROUTE_ESCALATE,
            "required_check_failed",
            "A required check did not conclude successfully.",
            checks=checks,
            severity=_SEVERITY_NAMES[severity],
        )
    stalled = _head_age_exceeded(pull_request, now, pending_hours)
    if checks["state"] == "pending" or mergeable == "UNKNOWN":
        if stalled:
            return _decision(
                _ROUTE_ESCALATE,
                "stalled_waiting_for_github",
                f"Still unresolved more than {pending_hours} hours after its head commit.",
                checks=checks,
                mergeable=_safe_text(mergeable),
            )
        return _decision(
            _ROUTE_REQUEUE,
            "awaiting_github",
            "Required checks or mergeability are not settled yet; judged again next cycle.",
            checks=checks,
            mergeable=_safe_text(mergeable),
        )

    if not facts["auto_merge_allowed"]:
        return _decision(
            _ROUTE_ESCALATE,
            "auto_merge_disabled",
            "Auto-merge is disabled for this repository and Flow A never merges directly.",
        )
    method = policy["merge_method"]
    if not facts[_MERGE_METHOD_ALLOWED_FIELD[method]]:
        return _decision(
            _ROUTE_ESCALATE,
            "merge_method_not_allowed",
            f"The repository does not allow the configured {method} merge method.",
            merge_method=method,
        )
    return _decision(
        _ROUTE_APPROVE,
        "within_policy_and_checks_green",
        f"{_SEVERITY_NAMES[severity]} update with every required check green.",
        checks=checks,
        severity=_SEVERITY_NAMES[severity],
        updates=updates,
        merge_method=method,
        merge_state_status=_safe_text(merge_state),
    )


def _pull_request_summary(repository: str, pull_request: dict[str, Any]) -> dict[str, Any]:
    """Copy only the identifying pull-request fields, bounded, into the artifact."""
    commits = _commit_nodes(pull_request, "headCommit")
    head_oid = commits[0].get("oid") if commits else None
    return {
        "repository": repository,
        "number": pull_request.get("number"),
        "title": _safe_text(pull_request.get("title"), _MAX_TITLE_LENGTH),
        "url": _safe_text(pull_request.get("url"), 500),
        "head_ref": _safe_text(pull_request.get("headRefName"), 300),
        "head_oid": head_oid if isinstance(head_oid, str) and _OBJECT_ID.match(head_oid) else None,
        "node_id": pull_request.get("id"),
        "mergeable": _safe_text(pull_request.get("mergeable")),
        "merge_state_status": _safe_text(pull_request.get("mergeStateStatus")),
    }


def _previous_history(state_dir: Path) -> dict[str, dict[str, Any]]:
    """Recover the rebase-attempt counter from this stage's own previous artifact only."""
    try:
        previous = json.loads((state_dir / "latest-pr-triage.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    entries = previous.get("pull_requests") if isinstance(previous, dict) else None
    history: dict[str, dict[str, Any]] = {}
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        repository = entry.get("repository")
        number = entry.get("number")
        attempts = entry.get("rebase_attempts", 0)
        if not isinstance(repository, str) or not isinstance(number, int):
            continue
        if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
            attempts = 0
        history[f"{repository}#{number}"] = {
            "head_oid": entry.get("head_oid"),
            "rebase_attempts": min(attempts, _DEFAULT_MAX_REBASE_ATTEMPTS * 10),
        }
    return history


def _history_for(summary: dict[str, Any], history: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Carry a rebase counter forward only while the head commit is unchanged."""
    entry = history.get(f"{summary['repository']}#{summary['number']}")
    if not isinstance(entry, dict) or entry.get("head_oid") != summary["head_oid"]:
        return {"rebase_attempts": 0}
    return {"rebase_attempts": entry.get("rebase_attempts", 0)}


def _mutate(document: str, variables: dict[str, str], environment: dict[str, str]) -> dict[str, Any]:
    """Run one GitHub mutation, reporting failure as data so one refusal cannot stop the stage."""
    try:
        _graphql(document, variables, environment)
    except RuntimeError as exc:
        return {"performed": False, "error": _safe_text(str(exc), 500)}
    return {"performed": True}


def _act(
    decision: dict[str, Any],
    summary: dict[str, Any],
    policy: dict[str, Any],
    environment: dict[str, str],
) -> list[dict[str, Any]]:
    """Perform the small, fixed set of GitHub calls a route allows; there is no merge call here."""
    node_id = summary.get("node_id")
    head_oid = summary.get("head_oid")
    if not isinstance(node_id, str) or not _NODE_ID.match(node_id):
        return [{"action": "abort", "performed": False, "error": "pull request node id is unusable"}]
    if not isinstance(head_oid, str) or not _OBJECT_ID.match(head_oid):
        return [{"action": "abort", "performed": False, "error": "head commit id is unusable"}]
    if decision["route"] == _ROUTE_COMMENT:
        result = _mutate(_COMMENT_MUTATION, {"pullRequestId": node_id}, environment)
        return [{"action": "comment_dependabot_rebase", **result}]
    # Auto-merge first: it carries the expected head commit, so a pull request that moved is
    # refused before any approval is recorded against it.
    enabled = _mutate(
        _AUTO_MERGE_MUTATION,
        {
            "pullRequestId": node_id,
            "expectedHeadOid": head_oid,
            "mergeMethod": policy["merge_method"],
        },
        environment,
    )
    actions = [{"action": "enable_auto_merge", **enabled}]
    if not enabled["performed"]:
        return actions
    approved = _mutate(
        _APPROVE_MUTATION,
        {"pullRequestId": node_id, "commitOID": head_oid},
        environment,
    )
    actions.append({"action": "approve", **approved})
    return actions


def _escalation(summary: dict[str, Any], decision: dict[str, Any], raised_at: str) -> dict[str, Any]:
    """Record an escalation as artifact data; delivery is best effort and never gates the record."""
    return {
        "repository": summary["repository"],
        "pull_request": summary["number"],
        "url": summary["url"],
        "title": summary["title"],
        "reason": decision["reason"],
        "summary": decision["summary"],
        "evidence": decision["evidence"],
        "raised_at": raised_at,
    }


def _telegram_configuration() -> dict[str, Any]:
    """Read the bot token from a mounted file; the token is never returned to a caller that logs."""
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    token_file = os.environ.get("TELEGRAM_BOT_TOKEN_FILE", "").strip()
    if not chat_id or not token_file:
        return {"configured": False, "reason": "TELEGRAM_CHAT_ID or TELEGRAM_BOT_TOKEN_FILE is unset"}
    try:
        token = Path(token_file).read_text(encoding="utf-8").strip()
    except OSError as exc:
        return {"configured": False, "reason": f"bot token file is unavailable: {exc.strerror}"}
    if not token:
        return {"configured": False, "reason": f"bot token file is empty: {token_file}"}
    return {"configured": True, "chat_id": chat_id, "token": token}


def _escalation_message(escalation: dict[str, Any]) -> str:
    """Render plain text only: a pull-request title is attacker-influenceable, never markup."""
    lines = [
        "repo-agent escalation",
        f"repository: {escalation['repository']}",
        f"pull request: #{escalation['pull_request']}",
        f"reason: {escalation['reason']}",
        f"title: {escalation['title']}",
        f"detail: {escalation['summary']}",
        f"url: {escalation['url']}",
    ]
    return "\n".join(lines)[:_MAX_MESSAGE_LENGTH]


def _send_telegram(configuration: dict[str, Any], text: str, timeout: int) -> dict[str, Any]:
    """Post one plain-text message; no parse mode, so untrusted content cannot become markup."""
    data = urlencode({"chat_id": configuration["chat_id"], "text": text}).encode("utf-8")
    request = Request(
        f"{_TELEGRAM_API}/bot{configuration['token']}/sendMessage",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 -- fixed HTTPS endpoint
            payload = json.load(response)
    except (OSError, URLError, ValueError, json.JSONDecodeError) as exc:
        message = str(exc).replace(configuration["token"], "REDACTED")
        return {"delivered": False, "error": _safe_text(message, 300)}
    if not isinstance(payload, dict) or not payload.get("ok"):
        return {"delivered": False, "error": "Telegram rejected the message"}
    return {"delivered": True}


def _notify(escalations: list[dict[str, Any]], apply: bool) -> dict[str, Any]:
    """Notify best effort; a delivery failure downgrades the report, never the recorded escalation."""
    if not escalations:
        return {"channel": "telegram", "status": "nothing_to_notify", "sent": 0, "failed": 0}
    if not apply:
        return {
            "channel": "telegram",
            "status": "dry_run",
            "sent": 0,
            "failed": 0,
            "would_send": len(escalations),
        }
    configuration = _telegram_configuration()
    if not configuration["configured"]:
        return {
            "channel": "telegram",
            "status": "unconfigured",
            "sent": 0,
            "failed": 0,
            "reason": configuration["reason"],
            "recorded": len(escalations),
        }
    try:
        timeout = _positive_integer(
            os.environ.get("TELEGRAM_TIMEOUT_SECONDS", "10"), "TELEGRAM_TIMEOUT_SECONDS"
        )
    except RuntimeError as exc:
        return {"channel": "telegram", "status": "failed", "sent": 0, "failed": 0, "reason": str(exc)}
    sent = 0
    failures: list[str] = []
    for escalation in escalations[:_MAX_NOTIFICATIONS]:
        result = _send_telegram(configuration, _escalation_message(escalation), timeout)
        if result["delivered"]:
            sent += 1
        else:
            failures.append(f"#{escalation['pull_request']}: {result['error']}")
    withheld = max(len(escalations) - _MAX_NOTIFICATIONS, 0)
    status = "sent" if not failures else ("partial" if sent else "failed")
    report = {
        "channel": "telegram",
        "status": status,
        "sent": sent,
        "failed": len(failures),
        "recorded": len(escalations),
        "not_notified": withheld,
    }
    if failures:
        report["errors"] = failures
    return report


def _triage_markdown(report: dict[str, Any]) -> str:
    """Render the verdicts an operator reads before ever enabling the acting half."""
    lines = [
        "# Pull request triage",
        "",
        f"- Status: **{report['status']}**",
        f"- Mode: **{report['mode']}**",
        "- Scope: GitHub API and policy only; no clone, no model, no gate execution, no merge call.",
        f"- Inventory: `{report['inventory_path']}`",
    ]
    if report.get("error"):
        lines.extend(["", "## Blocker", "", str(report["error"])])
    for entry in report.get("pull_requests", []):
        lines.extend(
            [
                "",
                f"## {entry['repository']}#{entry['number']} — {entry['route']}",
                "",
                f"- Title: {entry['title']}",
                f"- URL: {entry['url']}",
                f"- Reason: `{entry['reason']}`",
                f"- {entry['summary']}",
            ]
        )
        for action in entry.get("actions", []):
            state = "performed" if action.get("performed") else "not performed"
            detail = f" — {action['error']}" if action.get("error") else ""
            lines.append(f"- Action `{action['action']}`: {state}{detail}")
    if not report.get("pull_requests"):
        lines.extend(["", "No open pull request required a decision."])
    escalations = report.get("escalations", [])
    lines.extend(["", "## Escalations", ""])
    if escalations:
        lines.extend(
            f"- `{item['repository']}#{item['pull_request']}` **{item['reason']}** — {item['summary']}"
            for item in escalations
        )
    else:
        lines.append("- None.")
    notifications = report.get("notifications", {})
    lines.extend(
        [
            "",
            "## Notifications",
            "",
            f"- Channel `{notifications.get('channel', 'none')}`: "
            f"**{notifications.get('status', 'unknown')}** "
            f"(sent {notifications.get('sent', 0)}, failed {notifications.get('failed', 0)})",
        ]
    )
    if notifications.get("reason"):
        lines.append(f"- {notifications['reason']}")
    lines.append("")
    return "\n".join(lines)


def _persist_report(report: dict[str, Any], state_dir: Path, timestamp: datetime) -> None:
    """Write the artifact on every exit path; a stale `latest-*` pointer would be read as fresh."""
    if report["status"] == "blocked" and "error" not in report:
        report["error"] = "the triage stage was interrupted before it completed"
    report["completed_at"] = datetime.now(UTC).isoformat()
    run_dir = state_dir / "runs" / timestamp.strftime("%Y%m%dT%H%M%SZ")
    run_dir.mkdir(parents=True, exist_ok=True)
    report_path = run_dir / "pr-triage.json"
    markdown_path = run_dir / "pr-triage.md"
    report["report_path"] = str(report_path)
    report["markdown_path"] = str(markdown_path)
    rendered = _triage_markdown(report)
    _write_json_atomic(report_path, report)
    markdown_path.write_text(rendered, encoding="utf-8")
    _write_json_atomic(state_dir / "latest-pr-triage.json", report)
    (state_dir / "latest-pr-triage.md").write_text(rendered, encoding="utf-8")
    _write_json_atomic(
        state_dir / "latest-escalations.json",
        {
            "kind": "pr_triage_escalations",
            "created_at": report["completed_at"],
            "source_report": str(report_path),
            "notifications": report.get("notifications", {}),
            "escalations": report.get("escalations", []),
        },
    )
    print(
        json.dumps(
            {
                "event": "pr_triage_completed",
                "report": str(report_path),
                "status": report["status"],
                "mode": report["mode"],
            }
        )
    )


def _inventory_repositories(inventory_path: Path) -> list[str]:
    """Take the repository list from the upstream artifact, re-validating rather than trusting it."""
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    if not isinstance(inventory, dict):
        raise RuntimeError("latest inventory must be a JSON object")
    if inventory.get("status") != "passed":
        raise RuntimeError(f"latest inventory is not passed: {inventory.get('status')}")
    repositories = inventory.get("repositories")
    if not isinstance(repositories, list):
        raise RuntimeError("latest inventory does not contain a repositories list")
    slugs: list[str] = []
    for entry in repositories:
        if not isinstance(entry, dict):
            raise RuntimeError("every inventory repository must be an object")
        repository_parts(entry.get("repository"))
        slugs.append(str(entry["repository"]))
    if not slugs:
        raise RuntimeError("latest inventory contains no repositories")
    return slugs


def _triage_repository(
    repository: str,
    environment: dict[str, str],
    history: dict[str, dict[str, Any]],
    now: datetime,
    pending_hours: int,
    apply: bool,
) -> list[dict[str, Any]]:
    """Fetch, route, and — only in apply mode — act on one repository's open pull requests."""
    policy = _triage_policy(repository)
    node = _fetch_repository(repository, environment)
    facts = _repository_facts(repository, node)
    connection = node.get("pullRequests")
    nodes = connection.get("nodes") if isinstance(connection, dict) else None
    results: list[dict[str, Any]] = []
    for pull_request in nodes or []:
        if not isinstance(pull_request, dict) or not isinstance(pull_request.get("number"), int):
            continue
        summary = _pull_request_summary(repository, pull_request)
        entry = _history_for(summary, history)
        if facts["archived"]:
            decision = _decision(
                _ROUTE_ESCALATE, "repository_archived", "The repository is archived."
            )
        else:
            decision = _route_pull_request(
                pull_request, facts, policy, entry, now, pending_hours
            )
        attempts = entry["rebase_attempts"]
        actions: list[dict[str, Any]] = []
        if decision["route"] in {_ROUTE_APPROVE, _ROUTE_COMMENT}:
            if apply:
                actions = _act(decision, summary, policy, environment)
                if decision["route"] == _ROUTE_COMMENT and actions[0].get("performed"):
                    attempts += 1
            else:
                actions = [
                    {"action": "would_comment_dependabot_rebase", "performed": False}
                    if decision["route"] == _ROUTE_COMMENT
                    else {"action": "would_enable_auto_merge_then_approve", "performed": False}
                ]
        results.append(
            {
                **summary,
                **decision,
                "default_branch": facts["default_branch"],
                "policy": {
                    "required_checks": policy["required_checks"],
                    "merge_method": policy["merge_method"],
                    "max_rebase_attempts": policy["max_rebase_attempts"],
                    "source": policy["source"],
                },
                "rebase_attempts": attempts,
                "actions": actions,
            }
        )
    return results


def run_pr_triage(apply: bool = False) -> int:
    """Triage open Dependabot pull requests; acting is off unless the operator passed --apply."""
    timestamp = datetime.now(UTC)
    state_dir = Path(os.environ["AGENT_STATE_DIR"])
    inventory_path = state_dir / "latest-inventory.json"
    report: dict[str, Any] = {
        "kind": "pull_request_triage",
        "started_at": timestamp.isoformat(),
        "mode": "apply" if apply else "dry_run",
        "inventory_path": str(inventory_path),
        "pull_requests": [],
        "escalations": [],
        "repository_errors": [],
        "status": "blocked",
    }
    try:
        history = _previous_history(state_dir)
        pending_hours = _pending_hours()
        environment = _github_environment(
            "PR_TRIAGE_TOKEN_FILE" if os.environ.get("PR_TRIAGE_TOKEN_FILE") else "GITHUB_TOKEN_FILE"
        )
        entries: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        for repository in _inventory_repositories(inventory_path):
            try:
                entries.extend(
                    _triage_repository(
                        repository, environment, history, timestamp, pending_hours, apply
                    )
                )
            except (OSError, RuntimeError, json.JSONDecodeError) as exc:
                failures.append({"repository": repository, "error": _safe_text(str(exc), 500)})
        raised_at = datetime.now(UTC).isoformat()
        escalations = [
            _escalation(entry, entry, raised_at)
            for entry in entries
            if entry["route"] == _ROUTE_ESCALATE
        ]
        report["pull_requests"] = entries
        report["escalations"] = escalations
        report["repository_errors"] = failures
        report["notifications"] = _notify(escalations, apply)
        report["status"] = "blocked" if failures else "passed"
        if failures:
            report["error"] = "; ".join(
                f"{failure['repository']}: {failure['error']}" for failure in failures
            )
    except (OSError, RuntimeError, ValueError) as exc:
        report["error"] = _safe_text(str(exc), 500)
    finally:
        # Unconditional: an interrupt must not leave the previous run's pointer standing.
        _persist_report(report, state_dir, timestamp)
    return 1 if report["status"] == "blocked" else 0
