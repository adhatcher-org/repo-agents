# Design: disposable workspaces, gate isolation, and the escalation loop

Proposed changes to the workspace model, gate execution boundary, and human-decision path.
Nothing here is implemented yet. This document exists to be argued with before code moves.

It supersedes the workspace assumptions in `docs/remainingwork.md` and closes several gaps recorded
in `docs/intent-vs-implementation.md`.

## Decisions already taken

| Decision | Choice | Why |
| --- | --- | --- |
| Repository workspaces | Disposable clone per work item under `/projects` | Removes the shared dirty checkout |
| Mirror/reference cache | **No** | Premature at ~1 item/day against small repos; adds a `gc` corruption hazard |
| Escalation surface | Pushed branch, **no** PR | A PR would be re-ingested by this pipeline as new work |
| Notification channel | SMTP email | No Slack in use |
| Decision feedback | `repo-agent decide` writing a state artifact | Matches the existing artifact-passing architecture |

## 1. Workspace model

Today the engineer stages operate on a long-lived checkout at a configured `path`, require it to be
clean and on its default branch, and deliberately leave it dirty because nothing commits. The test
stage then cannot simply check that branch out, so it exports `git diff HEAD`, replays the patch into
a worktree, and separately copies untracked files the diff cannot express.

That replay path is the source of every defect found in the test executor so far: omitted untracked
files, corrupted trailing whitespace, and dead `--binary` support.

**Proposed:** one disposable clone per work item.

```
/projects/work/<sanitized-slug>/<run-id>/
```

- Cloned fresh from `origin` at dispatch time. Full clone — **not** `--depth` or
  `--filter=blob:none`, because a partial clone lazily fetches objects over the network during later
  operations, which is incompatible with running gates in a network-isolated container.
- The engineer **commits** into it. There is no dirty state to reproduce.
- For an existing-PR item, `git fetch origin pull/<n>/head` then checkout detached, as today.
- Purged when the item reaches a terminal state, with an age-based sweep as the backstop so an
  unmerged PR cannot leak disk forever. The same reaper should handle `data/runs` retention.

`<sanitized-slug>` uses the existing flattening rule (`[^a-z0-9]+` → `-`) so `owner/repo` cannot
introduce a path separator.

### What this deletes

- `_checkout_branch`, `_apply_pending`, `_copy_untracked`, `_worktree_destination`, `_git_capture`
  and their tests — the test stage checks out a commit instead.
- `engineer-preflight`'s "must be clean and on its default branch" precondition.
- The "never modify the configured checkout" guarantee, along with the `.git` footprint caveat —
  there is no shared checkout left to protect.
- `path` from `repo-info.yml`.

Dropping `path` also resolves the config confusion noted in the reconciliation: `repos.yml` becomes
purely *what to monitor*, `repo-info.yml` purely *policy, gates, and architecture docs*. Neither
file names a filesystem location.

## 2. Gate isolation

Gates execute PR-authored code. Today they run inside the service holding the write token, a
read-write `/projects`, and the state directory — so a hostile gate can rewrite the pipeline's own
control files. This is the one finding the test-executor hardening did not mitigate.

The project deliberately requires no Docker socket, so a stage cannot spawn a sibling container.
That rules out the obvious "run gates in another container" answer.

**Proposed:** privilege separation inside the test stage.

- The stage process runs as the normal UID and owns artifact I/O.
- Gate subprocesses run as a second, unprivileged UID via `subprocess.run(..., user=...)`.
- The state directory is mounted `0700` owned by the stage UID, so gate processes cannot read or
  write any pipeline artifact.
- The workspace is owned by the gate UID.
- Network egress is denied for the gate UID, or for the whole test service if gates never need it
  (`bootstrap` may — see open questions).

This keeps the no-socket property and needs no orchestration changes. A separate compose service
with narrower mounts remains an option if privilege separation proves insufficient.

### Push integrity

Because gates can write to the workspace, a hostile gate could move the branch ref to point at code
that was never tested. The publisher must therefore push **the exact commit SHA recorded in the test
report**, not a branch name:

```
git push origin <tested-sha>:refs/heads/<branch>
```

A tampered local ref then cannot affect what is published.

## 3. Self-exclusion — blocks the publisher

`cli._inventory_repository` lists every open PR with no filtering, though it already selects
`author`, `headRefName`, and `isDraft`.

The moment the publisher opens a PR from a `repo-agent/` branch, the next inventory ingests it as
third-party work: the architect plans it, the dispatcher assigns it, and `engineer-execute` routes it
to `existing_pull_request_ready_for_testing`. It re-enters the queue on every cycle until a human
merges it, and because only one item runs at a time, it can starve all real work.

**Proposed:** exclude PRs whose `headRefName` begins with the agent branch prefix, and/or whose
author is the pipeline's own identity, from work-item generation. Track them instead through
`ci_monitor`, which follows the URL recorded in the publication report.

This is a prerequisite for the publisher, not a follow-up.

## 4. Escalation loop

`overview.md` requires major architectural changes to be escalated for a human decision. There is no
mechanism today: `disposition` is validated only as "is a string", `_ELIGIBLE_DISPOSITIONS` accepts
three values, and every other value is discarded by a bare `continue` with no report. An item the
architect deems too significant to auto-fix is indistinguishable from a typo.

### 4.1 Closed disposition vocabulary

Validate `disposition` against an explicit set at parse time and reject anything else, exactly as
`_validate_critic_response` already does for `verdict`. Proposed values:

| Disposition | Meaning |
| --- | --- |
| `approve` / `remediate` | Agent may proceed |
| `escalate` | Needs a human decision before any change |
| `decline` | Deliberately not actioned |

An unrecognised value must **block the plan**, not skip the item silently.

### 4.2 Two escalation shapes

**Pre-implementation** — no code exists yet. No branch. The email carries the item, the architect's
rationale, and the affected repository.

**Proposed-change** — the agent has an implementation to show. It commits to a branch and pushes it,
with **no PR** (see §3). The email links the GitHub compare view.

### 4.3 Answering

```bash
repo-agent decide --item OWNER/REPO:alert:12 --approve --note "ok, keep it in the adapter layer"
repo-agent decide --item OWNER/REPO:alert:12 --reject --note "not now"
repo-agent decide --item OWNER/REPO:alert:12 --defer
```

Writes a decision artifact the next `dispatch-once` consults. The pending queue is written to
`latest-pending-decisions.json` so the state is inspectable without email.

Because gate processes cannot reach the state directory (§2), a hostile PR cannot approve itself.

### 4.4 SMTP placement

SMTP credentials belong to the **controller only** — it is read-only, never executes repository code,
and escalations are raised during planning and dispatch, which already run there. Supplied as a file
mount like `github-token`; never `.env`, never the image.

The engineer and test services must not receive them.

## 5. Container topology

| Service | Secrets | `/projects` | State dir | Runs repo code |
| --- | --- | --- | --- | --- |
| `repo-agent` (controller) | read token, SMTP | none | read-write | no |
| `repo-agent-engineer` | write token | read-write | read-write | no |
| test stage — stage process | none | read-write | read-write | no |
| test stage — gate subprocesses | none | workspace only | **none** | yes |

The significant change: applying model-authored patches and executing repository code are now
different trust levels. Only the gate subprocesses run untrusted code, and they hold nothing.

## 6. Config changes

- `repo-info.yml`: remove `path`. Begin honouring `policy` (`address_severities`, `never_merge`,
  `create_draft_prs`), which is currently read by no code at all.
- New environment: SMTP host/port/from/to, agent branch prefix, workspace root, retention windows.
- New secret: SMTP credentials file.

## 7. Sequencing

1. `active-work-item.json` lifecycle — nothing runs twice without it.
2. Workspace model (§1) + gate isolation (§2).
3. Disposition vocabulary and escalation reporting (§4.1).
4. Self-exclusion (§3).
5. `decide` verb, pending-decision artifact, SMTP (§4.3, §4.4).
6. `pr_reviewer`.
7. Publisher — first stage needing push authority.
8. Failed-CI inventory source.
9. Chain the stages so the daemon runs unattended.

## 8. Open questions

1. **Does `bootstrap` need network?** `uv sync` downloads packages. If gates must reach the network,
   the isolation in §2 weakens considerably — dependency resolution executes arbitrary package code.
   Options: pre-populate a package cache during workspace provisioning while still trusted, then deny
   egress for the remaining gates.
2. **Retention windows** for workspaces and `data/runs` — the `team_lead.md` note says 7 days.
   Should that be operator config rather than a prompt line?
3. **Terminal state for an escalated item** — does `decline` close the item permanently, or can a
   later plan re-raise it?
4. **Does `policy.address_severities` filter at inventory time or planning time?** Filtering at
   inventory loses the audit trail of what was seen and skipped.
