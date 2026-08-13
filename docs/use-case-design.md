# Design: solving the four initial use cases

Derived from `docs/Initial_use_cases.md`. This is the primary design document — where it conflicts
with `docs/remainingwork.md` or `docs/workspace-and-escalation-design.md`, this wins.

The workspace model, gate isolation, escalation loop, and self-exclusion rule from
`workspace-and-escalation-design.md` still hold. What changes is which of them the common path
actually needs.

## The cases

| # | Case | Flow |
| --- | --- | --- |
| 1 | Dependabot PR, ready to review, no conflicts | A |
| 2 | Dependabot PR, ready to review, with conflicts | A |
| 3 | CodeQL finding | B |
| 4 | Dependabot security alert | A if a PR exists, else B |

## The reframing

Three of the four need **no clone, no model, and no local test execution**.

Case 1 is a policy decision — is this a bot dependency PR, is CI green, is the bump within
tolerance. Case 2 is a comment. Case 4 usually collapses into case 1 or 2, because Dependabot
security updates open their own PR.

Only case 3 requires understanding code, generating a fix, and verifying it locally.

The pipeline as built — architect, critic, engineer handoff/preflight/execute, test executor — is
all Flow B machinery. Dependabot PRs arrive weekly per repository; CodeQL findings arrive
occasionally. **The rarest case was built first, and the common case has almost nothing.**

## Flow A — pull request triage

GitHub API and policy only. No workspace, no Ollama, no gate execution.

### Inputs

The inventory must collect more than it does today. Current selection is
`number,title,author,headRefName,baseRefName,isDraft,reviewDecision,url` — which cannot distinguish
case 1 from case 2, and cannot tell whether CI passed.

Add `mergeable`, `mergeStateStatus`, and `statusCheckRollup`.

For the policy axes, **do not parse the PR title**. Dependabot writes structured metadata into its
commit message:

```
updated-dependencies:
- dependency-name: requests
  dependency-type: direct:production
  update-type: version-update:semver-minor
```

That yields `dependency-type` and `update-type` directly, with no semver inference. Read the PR's
commit message; treat a PR whose metadata cannot be parsed as not-auto-mergeable.

### Routing

| Condition | Action |
| --- | --- |
| `mergeStateStatus` indicates conflict | comment `@dependabot rebase`, mark waiting, requeue |
| required checks failing | escalate by email |
| required checks absent | escalate by email (see below) |
| checks green, within policy | approve, enable GitHub auto-merge |
| checks green, outside policy | escalate by email |
| PR authored by this pipeline | report only — never auto-merge |

### Auto-merge policy

Per-repository in `repo-info.yml`, defaulting to:

| | patch | minor | major |
| --- | --- | --- | --- |
| `direct:development` | merge | merge | merge |
| `direct:production` | merge | merge | **escalate** |
| `indirect` | merge | merge | **escalate** |

A security fix that requires a major bump escalates like any other major, but the email must carry
the vulnerability details and be marked more urgent — it should not sit in a queue.

### Two traps

**An empty check list is not success.** A repository with no CI returns zero check runs, and testing
for "nothing failed" would auto-merge it unverified. Flow A must test for *required checks present
and successful*, never for absence of failure. This is the same shape as a bare `passed` meaning "no
gates ran", which the test executor already had to be fixed for once.

**GitHub auto-merge is not a gate by itself.** It requires *Allow auto-merge* enabled per repository,
and it only waits when branch protection defines required status checks. Without them, enabling
auto-merge merges immediately. The pipeline therefore verifies the check rollup itself before
approving, and never relies on GitHub to hold the gate.

### Waiting state

Case 2 introduces a state the pipeline does not have: an item that is neither finished nor failed,
waiting on an external actor. `@dependabot rebase` is asynchronous.

The active work item needs `waiting` with a retry count and a next-check timestamp. After a bounded
number of rebase attempts without reaching a mergeable state, escalate — Dependabot cannot resolve
every conflict, and a lockfile conflict against another merged PR often needs a human.

This is a second reason the `active-work-item.json` lifecycle must be built first.

## Flow B — code remediation

Case 3, and case 4 when no PR exists. This is the existing pipeline: disposable clone, architect and
critic, engineer patch, local gates, push, open PR.

Routed by the architect's disposition, per `workspace-and-escalation-design.md` §4.1:

- `remediate` — produce a branch and a pull request for review
- `escalate` — email the alert and a suggested approach **before** writing code
- `decline` — record why, with rationale, in the report

CodeQL findings vary too much for a fixed rule. Some are mechanical, some are false positives, some
need real judgment, and a wrong "fix" to a security finding is worse than none.

**Nothing produced by Flow B is ever auto-merged.** Agent-authored code gets human review;
Dependabot's does not need it. That is the correct form of self-exclusion — Flow A may watch and
report on the pipeline's own PRs, but must never merge them.

For case 4 without a PR the change is usually a mechanical version bump rather than a model-authored
patch, and should be treated as such rather than sent to the engineer model.

## Repositories without CI

Escalate by default. Local gates are a per-repository opt-in in `repo-info.yml`.

This keeps a freshly bumped, unvetted dependency from executing on the NAS unless explicitly
requested for that repository — and gives the test executor a real role in Flow A where opted in.

## Escalation

Email via SMTP, credentials held by the controller only. A pushed branch with **no** pull request
where there is a change to show, because a PR would be re-ingested by this pipeline as new work.

Answered with `repo-agent decide --item <id> --approve|--reject|--defer --note "..."`, which writes a
state artifact the next dispatch consults.

## Impact on what exists

**Keep** — the artifact and status contract, output validation discipline, the inventory foundation,
the disposable workspace model, the escalation path, and the test executor. Flow B needs all of it.

**Reposition** — architect and critic become Flow B tools. Running two model calls to decide whether
a patch-level Dependabot bump with green CI should merge adds latency and risk to what is a policy
lookup.

**Build** — Flow A, essentially in full.

## Sequencing

1. `active-work-item.json` lifecycle, including the `waiting` state — nothing runs twice without it,
   and case 2 needs it directly.
2. Inventory fields: `mergeable`, `mergeStateStatus`, `statusCheckRollup`, Dependabot commit metadata.
3. Flow A routing and policy matrix — closes cases 1, 2, and most of 4.
4. Approve + enable auto-merge.
5. Escalation: disposition vocabulary, `decide` verb, pending-decision artifact, SMTP.
6. Workspace model and gate isolation (Option A: read-only state mount).
7. Publisher — closes case 3 and the remainder of case 4.
8. Failed-CI trigger and unattended chaining.

Flow A first is a deliberate inversion of the original plan: it covers three of four cases and the
overwhelming majority of real volume, and it needs neither the workspace rework nor the publisher.

## Provisional decisions

Recommended and adopted here, but not yet explicitly confirmed:

1. GitHub auto-merge with pipeline-side check verification, rather than the agent merging directly.
2. CodeQL routed by architect disposition rather than a fixed auto-fix rule.
3. No-CI repositories escalate by default, with local gates as per-repository opt-in.

## Open items

1. Which of `college_planner`, `financial_analysis`, `frontend_api` have branch protection with
   required checks — determines where auto-merge is safe.
2. Retention windows for workspaces and `data/runs`; operator config rather than a prompt line.
3. Does `decline` close an item permanently, or may a later plan re-raise it?
4. Rebase attempt limit before case 2 escalates.
