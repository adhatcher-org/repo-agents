# Intent vs. implementation

A reconciliation of `docs/overview.md` (what this repo is meant to do) against what is actually
built, as of the `test-execute` stage landing.

`docs/remainingwork.md` was the forward-looking plan for the stages that did not exist yet. This
document is the backward-looking check: for each intent in the overview, what exists, what is
partial, and what is missing — with the evidence.

## Summary

| # | Intent (from `overview.md`) | Status |
| --- | --- | --- |
| R1 | Monitor several GitHub repos listed in config | Built |
| R2a | Trigger on security findings | Built |
| R2b | Trigger on Dependabot version bumps | Built |
| R2c | Trigger on failed CI jobs | **Missing** |
| R3 | Review the findings | Built |
| R4 | Fix them | Partial |
| R5 | Test the fix | Built |
| R6 | Verify the fix | Partial |
| R7 | Submit a PR and commit | **Missing** |
| R8 | A set of agents handling specific jobs | Built |
| R9 | Team lead assigns work to one or more agents | Divergent (one, serialized) |
| R10 | Automated process running in a container | Partial |
| R11 | Clone repos into `/projects` | Divergent (pre-provisioned) |
| R12 | Escalate major architectural changes for a human decision | **Missing** |
| R13 | Minor changes documented and committed, noting behavior changes | Partial |
| R14 | Agents check each other's work | Partial |

Six of fourteen are fully built. The two that most directly carry the stated payoff — R7 (commit and
open the PR) and R2c (failed CI jobs) — are absent, and R12 (escalation) has no mechanism at all.

## Detail

### R1 — Monitor configured repositories · Built

`run-once` / `daemon` in `cli.py` inventory each configured repository every 24 hours and write
`latest-inventory.json` plus a team-lead Markdown report.

One correction to the overview: the monitored repository list is `config/repos.yml`
(`AGENT_CONFIG`), **not** `config/repo-info.yml`. `repo-info.yml` (`AGENT_REPOSITORY_INFO`) is a
separate file holding per-repo execution metadata — checkout path, default branch, architecture
docs, quality gates, policy. Two files keyed by the same slug, for two different purposes.

### R2 — Trigger sources · Partial

Collected today (`cli._inventory_repository`): open pull requests, Dependabot alerts, code-scanning
alerts, secret-scanning alerts.

**Not collected: failed CI jobs.** There is no workflow-run or check-run query anywhere. The
overview names failed CI as a first-class trigger ("failed CI jobs b/c the version of x/y/z thing is
no longer valid"), and that input source does not exist. Closing it means an additional GitHub API
call and a new work-item kind alongside `open_pull_request`.

### R3 — Review the findings · Built

`plan-once` runs the architect and then the critic, both via Ollama, both output-validated:
`_validate_architect_plan` requires an exact ID set match with the inventory; `_validate_critic_response`
requires a verdict in `{approved, changes_requested}` and exact coverage of every item.

### R4 — Fix · Partial

`engineer-execute` obtains a strictly validated patch contract from the model and applies it to a
new `repo-agent/engineer-*` branch. It deliberately stops there: no commit, no push.

For an existing pull request it does not generate a patch at all — it records
`existing_pull_request_ready_for_testing` so the PR's real diff reaches the test stage. That is the
right call for the Dependabot case and worth keeping.

### R5 — Test · Built

`test-execute` runs the configured `quality_gates` in a disposable worktree and always tears it
down. Deterministic; never calls Ollama.

### R6 — Verify · Partial

Two of the three verification loops exist: the critic checks the architect, and the test executor
checks the engineer's output. The independent diff review (`pr_reviewer`) is not implemented, so
nothing reads the actual change with judgment before it would be published.

### R7 — Submit a PR and commit · Missing

Not implemented. The pipeline ends at an uncommitted dirty branch in the configured checkout.

This is the stated payoff of the whole system ("so I don't have to"). Everything built so far is the
safety scaffolding around a step that does not exist. Until it lands, the pipeline reduces manual
work only by producing a reviewed, tested branch a human must still finish.

### R8 — A set of agents for specific jobs · Built

Seven definitions in `agents/`, discovered by globbing rather than registered in code. Five active:
`team_lead`, `senior_architect`, `senior_architect_critic`, `senior_software_engineer`, `test_agent`.
`pr_reviewer` and `ci_monitor` are `status: planned`.

Caveat: the `status` field is **not enforced**. `_agent_configuration` validates provider, model,
temperature, and timeout, but never refuses a definition marked `planned`. The roster is
documentation, not a control.

### R9 — Assign to one or more agents · Divergent

`dispatch-once` assigns exactly one item, globally, and refuses a second while one is in flight.
This is a deliberate safety property — it prevents concurrent write-capable containers and
overlapping Ollama jobs — but it is a different shape from the overview's "one or more agents."

Worth an explicit decision rather than leaving the divergence implicit.

### R10 — Automated · Partial

The daemon runs **only** the inventory. `plan-once`, `dispatch-once`, and every engineer and test
stage are manual invocations. Nothing chains them.

Compounding this: `active-work-item.json` is written once by the dispatcher with `status: "assigned"`
and **never updated by any stage**. `assigned` is not terminal, so every later `dispatch-once`
returns `already_assigned` indefinitely. The pipeline processes one item and then stops until the
file is cleared by hand.

### R11 — Clone into `/projects` · Divergent

Nothing clones. The engineer stages require a pre-existing checkout at a configured `path`, and
`engineer-preflight` blocks if it is missing, dirty, or off its default branch.

**Decision: keep pre-provisioned checkouts.** This is simpler and keeps clone/credential handling
out of the write-capable service. `docs/overview.md` should be amended to match, since it currently
describes cloning.

### R12 — Escalate major architectural changes · Missing

No mechanism exists.

`disposition` is validated only as "is a string" (`planning.py:224`) — there is no enumerated
vocabulary, and the architect prompt does not constrain the values. `_ELIGIBLE_DISPOSITIONS` then
accepts `{approve, approved, remediate}`; **every other value is silently skipped** by a bare
`continue` in `_selected_approved_item`.

So an item the architect deems too architecturally significant to auto-fix is treated identically to
a typo, and neither is reported. There is no "needs your decision" queue, and no operator-facing
signal that items were passed over.

Additionally, the `policy` block in `repo-info.yml` — `create_draft_prs`, `never_merge`,
`require_architect_critic`, `address_severities` — is **never read by any code**. It looks like
enforceable configuration and enforces nothing.

Closing R12 needs: a closed disposition vocabulary validated at parse time, an explicit escalation
value, a report listing everything not auto-actioned and why, and `policy` actually consulted.

### R13 — Document minor changes and note behavior shifts · Partial

The engineer contract includes `architecture_documents_to_update`, and `repo-info.yml` lists
`architecture_docs`, so the intent is represented in the data model. But nothing commits, so nothing
is recorded durably, and there is no changelog capturing behavior or architecture shifts of the kind
the overview describes ("moved secrets into a vault vs keeping them in a local secret file").

### R14 — Agents check each other's work · Partial

Present: critic reviews architect; test executor independently validates the engineer's output
against operator-configured gates rather than the model's own claims.

Absent: `pr_reviewer`. Also, no stage reviews the *test executor's* verdict, which matters because
gate output is repository-controlled.

## Cross-cutting defects

These are not intent gaps — they are defects in what exists.

1. **Dispatch deadlock** — `active-work-item.json` has no lifecycle. Blocks R10 entirely.
2. **`policy` is decorative** — never read. Blocks R12.
3. **`status` is decorative** — never enforced. Weakens R8.
4. **`disposition` is unvalidated free text** — enables the silent-skip behavior in R12.
5. **Gate commands execute repository code inside the container holding the write token, a
   read-write `/projects` mount, and the state directory.** `test-execute` treats gate *output* as
   hostile, but cannot contain a gate that writes to the mounts it can already reach. This must be
   settled before the pipeline is pointed at a third-party pull request.

## Recommended order

1. `active-work-item.json` lifecycle — nothing runs twice without it.
2. Disposition vocabulary + escalation report + honour `policy` (R12).
3. Gate isolation container (defect 5) — before any third-party PR.
4. `pr_reviewer` (R6, R14).
5. Publisher (R7) — the payoff, and the first stage that needs push authority.
6. Failed-CI inventory source (R2c).
7. Chain the stages so the daemon can run the loop unattended (R10).
