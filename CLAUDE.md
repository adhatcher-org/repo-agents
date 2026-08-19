# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Everything below describes code that exists at `383d28d`. Anything documented elsewhere but not
backed by code is collected under [Not implemented](#not-implemented) at the end, with a line
reference to whatever partial machinery does exist. `docs/implementation-status.md` is the
authoritative claim-by-claim reconciliation.

## Commands

`uv` is the only supported entry point; `pyproject.toml` + the committed `uv.lock` are authoritative.

```bash
make install      # uv sync --all-groups
make check        # lock-check + format-check + lint + test + coverage + security (what CI runs)
make test         # uv run pytest
make coverage     # pytest --cov=repo_agent --cov-branch (fail_under = 90)
make lint         # ruff check .
make format       # ruff format .
make security     # pip-audit
make lock         # regenerate uv.lock after a deliberate dependency change
```

Run a single test:

```bash
uv run pytest tests/test_cli.py::test_run_planning_records_no_work_without_calling_ollama
```

CI (`.github/workflows/ci.yml`) runs `make check` plus a Docker build. Coverage below 90% fails the
build (`pyproject.toml:37`), so new branches in `src/repo_agent/` need tests. There are two test
modules: `tests/test_cli.py` (2413 lines, covering `cli.py`, `planning.py`, `engineering.py`,
`testing.py`, `github.py`) and `tests/test_triage.py` (381 lines, covering `triage.py`).

## Architecture

A containerized, staged repository-maintenance pipeline. Every stage is a subcommand of the same
`repo-agent` CLI (`src/repo_agent/cli.py:382` `main()`), reads the previous stage's artifact from the
state directory, and writes both a timestamped artifact under `$AGENT_STATE_DIR/runs/<UTC>/` and a
`latest-*.json` pointer at the state-dir root. Stages never talk to each other in-process — the
`latest-*.json` file *is* the interface, and each stage re-validates it.

### The complete CLI surface

`cli.py:386-399` defines exactly these twelve commands and two flags (`--item`, `--apply`). There are
no others.

| Command | Entry point | Artifact | Service |
| --- | --- | --- | --- |
| `version` | `cli.py:409` | none (prints `__version__`) | either |
| `health` | `cli.py:74` | none (prints JSON, exit 1 if unhealthy) | either |
| `agents` | `cli.py:412` → `_agent_definitions()` `cli.py:44` | none (prints the roster) | either |
| `run-once` | `run_inventory()` `cli.py:300` | `latest-inventory.json`, `latest-team-lead-report.md` | controller |
| `daemon` | `daemon()` `cli.py:349` | loops `run_inventory()` on `AGENT_RUN_INTERVAL_SECONDS` | controller |
| `pr-triage [--apply]` | `triage.run_pr_triage()` `triage.py:1022` | `latest-pr-triage.json/.md`, `latest-escalations.json` | controller |
| `plan-once` | `planning.run_planning()` `planning.py:294` | `latest-architect-plan.json/.md` | controller |
| `dispatch-once` | `engineering.run_team_lead_dispatch()` `engineering.py:166` | `active-work-item.json`, `latest-team-lead-dispatch.json/.md` | controller |
| `engineer-handoff --item` | `engineering.run_engineer_handoff()` `engineering.py:318` | `latest-engineer-handoff.json/.md` | controller |
| `engineer-preflight --item` | `engineering.run_engineer_preflight()` `engineering.py:400` | `latest-engineer-preflight.json` | engineer |
| `engineer-execute --item` | `engineering.run_engineer_execute()` `engineering.py:639` | `latest-engineer-execution.json` | engineer |
| `test-execute --item` | `testing.run_test_execute()` `testing.py:430` | `latest-test-report.json/.md` | engineer |

`--apply` is consumed only by `pr-triage` (`cli.py:420`). `--item` is required by the four
item-scoped stages and rejected by `parser.error` otherwise (`cli.py:424-438`).

### Two flows, not one pipeline

**Flow A — `pr-triage` (`triage.py`).** Deterministic Dependabot pull-request routing from the GitHub
GraphQL API and policy alone. No clone, no Ollama, no gate execution, no merge call. It reads
repository slugs out of `latest-inventory.json` (`triage.py:944`) and then re-fetches live state per
repository (`triage.py:301`) rather than acting on the inventory snapshot. It is independent of the
Flow B chain below — nothing links them.

**Flow B — the staged chain.** Inventory → plan → dispatch → handoff → preflight → execute → test.
Each stage consumes the previous `latest-*.json`.

```
run-once / daemon  → latest-inventory.json          (cli.py:300,      read-only GitHub inventory + team-lead report)
plan-once          → latest-architect-plan.json     (planning.py:294, architect + critic via Ollama)
dispatch-once      → active-work-item.json          (engineering.py:166, selects ONE item, calls the handoff)
engineer-handoff   → latest-engineer-handoff.json   (engineering.py:318, one approved item, no repo access)
engineer-preflight → latest-engineer-preflight.json (engineering.py:400, verifies clean checkout)
engineer-execute   → latest-engineer-execution.json (engineering.py:639, branch + patch apply)
test-execute       → latest-test-report.json        (testing.py:430,  disposable worktree + gates)
```

`dispatch-once` is the serialization point (`engineering.py:180-189`). It refuses to assign anything
while `active-work-item.json` exists with a non-terminal status (terminal set at `engineering.py:20`
= `blocked`, `cancelled`, `completed`, `failed`), so only one engineering job is ever in flight
across all repositories. It picks the first architect item whose disposition is
`approve`/`approved`/`remediate` **in plan order** (`_ELIGIBLE_DISPOSITIONS`, `engineering.py:19`;
selection at `engineering.py:95`), and treats an unreadable or malformed `active-work-item.json` as a
hard blocker rather than falling through to a fresh assignment (`engineering.py:132-135`).

**Nothing ever updates `active-work-item.json` after the dispatcher writes it.** The only write is
`engineering.py:228`; the only read is `engineering.py:125`. `assigned` is not in the terminal set,
so every later `dispatch-once` returns `already_assigned` until the file is removed by hand.

Statuses are the gate between stages. Each stage refuses to run unless the upstream artifact carries
the exact expected status *and* its `work_item.id` matches `--item`:

| Reader | Required upstream status | Guard |
| --- | --- | --- |
| `plan-once` | inventory `passed` | `planning.py:309` |
| `pr-triage` | inventory `passed` | `triage.py:950` |
| `dispatch-once` / `engineer-handoff` | plan `approved` **and** critic verdict `approved` | `engineering.py:67-71`, `195-198` |
| `engineer-preflight` | handoff `ready_for_implementation` | `engineering.py:415` |
| `engineer-execute` | preflight `ready_for_coding` | `engineering.py:625` |
| `test-execute` | execution `implementation_applied` or `existing_pull_request_ready_for_testing` | `testing.py:29`, `467` |

Failures are never exceptions to the caller — every stage catches, sets `status: "blocked"` with an
`error` string, still writes its artifact, and returns exit code 1. `pr-triage` and `test-execute`
additionally persist in a `finally` (`triage.py:1073`, `testing.py:448`), because a stage that returns
without writing leaves the previous run's `latest-*.json` to be read as the current verdict.

### Two container services

`compose.yml` defines two services from the same image with deliberately different blast radius:

- `repo-agent` — the long-running daemon, `command: ["repo-agent", "daemon"]` (`compose.yml:45`).
  **The daemon runs the inventory and nothing else** (`cli.py:377-379`). It gets
  `/run/secrets/github-token` (read scope) and never mounts repository code.
- `repo-agent-engineer` — `profiles: ["engineer"]`, one-shot, excluded from `docker compose up`. Its
  default command runs `engineer-execute --item "$ENGINEER_ITEM_ID"` (`compose.yml:85-90`). Gets only
  the write-capable `se-gh-token` (not the controller's whole secrets dir) plus `/projects:rw`.

No compose service runs `pr-triage`, `plan-once`, `dispatch-once`, `engineer-handoff`,
`engineer-preflight`, or `test-execute`. Those are manual `docker exec` / `docker compose run`
invocations.

Keep that separation when adding stages: anything that can write to a repo belongs in the profile-only
service, and the controller must not gain access to write tokens or repository mounts.

### Agent definitions are runtime config, not code

`agents/*.md` are mounted live at `$AGENT_DEFINITIONS_DIR` and can be edited without rebuilding the
image. YAML front matter declares `id`, `status`, `execution`, `provider`, `model`, `temperature`,
`timeout_seconds`; the Markdown body is the system prompt.
`planning._agent_configuration()` (`planning.py:35`) parses them and rejects any definition that isn't
`provider: ollama` with a real model (`planning.py:60`), temperature in [0, 2] (`planning.py:70`), and
a positive timeout (`planning.py:79`). Adding a role means adding a definition file — the roster is
discovered by globbing (`planning.py:38`, `cli.py:48`), not registered in code.

**`status` is not enforced.** `_agent_configuration` never inspects it; `cli._agent_definitions`
(`cli.py:65`) only echoes it into the `agents` listing. The current front matter says
`active`: `team_lead`, `senior_architect`, `senior_architect_critic`, `test_agent`. It says `planned`:
`senior_software_engineer`, `pr_reviewer`, `ci_monitor`. `senior_software_engineer` is marked
`planned` and is nevertheless invoked by `engineer-execute` (`engineering.py:678`). Treat the roster
as documentation of intent, not as a control on what can execute.

`team_lead` and `test_agent` are `provider: none` and are correct as such: the dispatcher and the test
executor are deterministic and never call `_agent_configuration`. Their Markdown bodies are
documentation, not prompts.

### Model output is untrusted input

Every Ollama response goes through `format: "json"` (`planning.py:94`) and then a hand-written
validator before it can affect anything:

- `_validate_architect_plan` (`planning.py:216`) — plan item IDs must be an exact set match with the
  inventory work items (no missing, no extra, no duplicates). `disposition` is checked only for
  `isinstance(..., str)` at `planning.py:225`; there is no closed vocabulary.
- `_validate_critic_response` (`planning.py:242`) — verdict ∈ {approved, changes_requested} and
  `covered_item_ids` must exactly match the inventory.
- `_validate_engineer_response` (`engineering.py:511`) — exact field set; each patch is a single-file
  `diff --git a/<p> b/<p>` whose header matches its declared path; no renames/copies; no absolute,
  `..`, or `.git` paths (`_safe_relative_path`, `engineering.py:502`); `files_to_change` must equal the
  patch path set. Patches are then written to one temp file and run through `git apply --check` before
  `git apply` (`engineering.py:600-616`).

Branch names come from `_branch_name()` (`engineering.py:496`, `repo-agent/engineer-<timestamp>-<slug>`),
never from the model. Repository paths are validated with
`Path.relative_to(ENGINEER_REPOSITORY_ROOT)` (`engineering.py:36-39`). Prompts instruct agents to
treat all inventory/repo content as data, never instructions — preserve that framing in any new agent
definition.

### Operator config is untrusted input too

`quality_gates` values are command strings from `repo-info.yml`. `testing._run_gate` (`testing.py:206`)
splits them with `shlex.split` and runs them with no `shell=True`, a bounded per-gate timeout
(`TEST_GATE_TIMEOUT_SECONDS`, default 1800, `testing.py:52`), and GitHub tokens stripped from the child
environment (`_gate_environment`, `testing.py:198`, removing `GH_TOKEN`, `GITHUB_TOKEN`,
`GH_ENTERPRISE_TOKEN`). Keep that discipline: no stage may hand a configured or model-supplied string
to a shell. `shlex.split` raises `ValueError` on an unbalanced quote, so `test-execute` catches
`ValueError` too (`testing.py:446`) — and writes its artifact in a `finally`.

Gate *output* is repository-controlled, so nothing that gates a decision may be derived from it
alone. `minimum_coverage` exists as the operator's independent check on a PR that edits the
project's own coverage threshold; `_record_coverage` (`testing.py:276`) reads `coverage.json` /
`coverage.xml` in preference to stdout, counts only anchored `TOTAL` rows
(`_COVERAGE_TOTAL_LINE`, `testing.py:38`), and fails on disagreeing totals instead of picking one
(`testing.py:296-298`).

### Flow A routing

`triage.py` collapses the 3×3 dependency-type matrix in `docs/Initial_use_cases.md` to a single
severity comparison, stated in the comment at `triage.py:49-59`: patch and minor may merge, every
major escalates, an unrecognised update type ranks above major so it can never merge by accident.
A grouped update is evaluated at its most severe member (`_highest_severity`, `triage.py:234`).

Update classification reads Dependabot's own `updated-dependencies:` commit trailer
(`_dependency_updates`, `triage.py:206`). The pull-request title is never parsed. An unparseable
trailer escalates (`triage.py:517-522`).

Routes (`triage.py:26-30`): `approve_and_enable_auto_merge`, `comment_rebase`, `escalate`, `requeue`,
`report_only`. There is deliberately **no merge route** — the stage enables GitHub's own auto-merge
and lets GitHub perform the merge (`_AUTO_MERGE_MUTATION`, `triage.py:157`, carrying `expectedHeadOid`
so a PR that moved after judgement is refused). Auto-merge is enabled *before* the approval is
recorded (`triage.py:698-716`), so an approval is never left standing on a PR whose auto-merge failed.

Invariants that must survive any edit to this stage:

- **An absent required check is never a pass.** `_required_check_state` (`triage.py:386`) asserts each
  required context is present, from the expected `app_id`, and green. A repository with no CI reports
  zero contexts and lands in `missing` → escalate.
- **Only Dependabot-authored PRs with Dependabot-authored, GitHub-signed commits are eligible.**
  `_author_check` (`triage.py:341`) checks the PR author, every commit's author, the signature, and
  that the commit list was not truncated.
- **Self-exclusion.** A PR whose `headRefName` starts with `repo-agent/` is `report_only`
  (`triage.py:471-476`) and can never be auto-merged.
- **Acting requires `--apply`.** Without it the stage records `would_*` pseudo-actions and performs no
  mutation (`triage.py:998-1003`), and notification is `dry_run` (`triage.py:793`).

Rebase attempts are counted forward only while the head commit is unchanged (`_history_for`,
`triage.py:660`), from this stage's own previous artifact only (`_previous_history`, `triage.py:635`).

### Escalation and notification

Escalations are recorded as artifact data (`_escalation`, `triage.py:720`) in `latest-pr-triage.json`
and in a dedicated `latest-escalations.json` (`triage.py:922`). Delivery is **Telegram**
(`_send_telegram`, `triage.py:768`; API constant `triage.py:81`), best effort, and never gates the
record. It is configured by `TELEGRAM_CHAT_ID` and `TELEGRAM_BOT_TOKEN_FILE` with optional
`TELEGRAM_TIMEOUT_SECONDS` (`triage.py:736`, `triage.py:811`), and sends plain text with no parse mode
so an attacker-influenceable PR title cannot become markup (`triage.py:754`). At most 20 messages per
run (`_MAX_NOTIFICATIONS`, `triage.py:79`).

There is no SMTP code anywhere. Where a design document says email, it is describing a decision that
was superseded; `docs/handoff.md` records the change to Telegram on 2026-08-14.

`compose.yml` sets no `TELEGRAM_*` variables, so notification is `unconfigured` (`triage.py:802-809`)
as the stack currently ships. The escalation record in the artifact is unaffected.

### Config files

- `AGENT_CONFIG` (`config/repos.yml`, from `config/repos.example.json`) — the inventory repo list.
  Parsed with `json.loads`, not YAML (`cli.py:123`): JSON is valid YAML and this keeps the controller
  dependency-free. Only `repositories[].slug` is read (`cli.py:317`).
- `AGENT_REPOSITORY_INFO` (`config/repo-info.yml`, from `config/repo-info.example.yml`) — per-repo
  execution metadata, loaded and keyed by slug in `_load_repository_info` (`engineering.py:43`).
  Keys actually read by code:

  | Key | Read at | Purpose |
  | --- | --- | --- |
  | `slug` | `engineering.py:57` | the map key |
  | `path` | `engineering.py:29` | checkout location, must resolve under `ENGINEER_REPOSITORY_ROOT` |
  | `default_branch` | `engineering.py:473` | required by `engineer-execute` |
  | `architecture_docs` | `engineering.py:569`, `684` | selects repo context excerpts for the model |
  | `quality_gates` | `testing.py:480` | gate commands; a `test` gate is mandatory |
  | `quality_gates.minimum_coverage` | `testing.py:314` | operator coverage backstop |
  | `pr_triage` | `triage.py:276` | `required_checks`, `merge_method`, `max_rebase_attempts` |

  **The `policy` block in `config/repo-info.example.yml:16-20`** (`create_draft_prs`, `never_merge`,
  `require_architect_critic`, `address_severities`) **is read by no code.** It is decorative. Do not
  add it to a repository's metadata expecting it to be enforced.

  Note also that `config/repo-info.example.yml:3` uses `path: /work/REPOSITORY`, which cannot pass
  `_workspace_path` under the shipped `ENGINEER_REPOSITORY_ROOT=/projects` (`compose.yml:62`). The
  example is wrong; real paths must be under `/projects`.

Real versions of both config files are gitignored; edit the `.example` files when changing the schema.

Only `PyYAML` is a runtime dependency (`pyproject.toml:11`, for `repo-info.yml`). GitHub access goes
through the `gh` CLI with `GH_TOKEN` injected from a mounted token file (`github.py:23`) — the token is
never logged or written into any artifact. `pr-triage` prefers `PR_TRIAGE_TOKEN_FILE` when set and
falls back to `GITHUB_TOKEN_FILE` (`triage.py:1040-1044`). Prefer `urllib` and `subprocess` over adding
dependencies.

Environment variables the code actually reads: `AGENT_STATE_DIR`, `AGENT_CONFIG`,
`AGENT_DEFINITIONS_DIR`, `AGENT_REPOSITORY_INFO`, `AGENT_RUN_INTERVAL_SECONDS`, `OLLAMA_BASE_URL`,
`GITHUB_TOKEN_FILE`, `PR_TRIAGE_TOKEN_FILE`, `ENGINEER_REPOSITORY_ROOT`, `TEST_GATE_TIMEOUT_SECONDS`,
`PR_TRIAGE_PENDING_HOURS`, `TELEGRAM_CHAT_ID`, `TELEGRAM_BOT_TOKEN_FILE`, `TELEGRAM_TIMEOUT_SECONDS`.
`compose.yml:19-20` sets `OLLAMA_ARCHITECT_MODEL` and `OLLAMA_CRITIC_MODEL`, which no code reads —
models come from agent front matter.

### What the pipeline deliberately does not do

`engineer-execute` applies patches to a dirty feature branch and stops (`engineering.py:704`). It does
not run tests, commit, push, open a PR, merge, or dismiss alerts. Open-PR work items are routed
straight to `existing_pull_request_ready_for_testing` (`engineering.py:654-665`) without invoking
Ollama or touching Git at all, so the real PR diff reaches the later test stage instead of a
regenerated patch.

`test-execute` runs the configured gates in a disposable worktree under
`<ENGINEER_REPOSITORY_ROOT>/.repo-agent-worktrees/<repository>/<run-id>` (`testing.py:66`) and removes
it in a `finally` (`testing.py:506`), on success and failure alike. It reads the configured checkout
(to fetch a PR head, `testing.py:108`, or to export the engineer branch's still-uncommitted
`git diff --binary HEAD` *and* copy in the untracked files that diff cannot express,
`testing.py:161-184`) and never changes its working tree or index. It *does* write Git metadata there,
unavoidably for a worktree-based executor: `git fetch` writes `FETCH_HEAD` and fetched objects, and
`git worktree add`/`remove` create and delete `.git/worktrees/<run-id>`. No ref is ever created,
moved, or deleted.

A gate absent from `quality_gates` is reported as `skipped`, never as a pass (`testing.py:318`) — and
because a bare `passed` must not mean "nothing ran", a repository without a `test` gate blocks
(`testing.py:481`), and `passed` is reserved for a run whose CI-equivalent `check` gate itself passed
(`_overall_status`, `testing.py:327`); anything else non-failing is `passed_partial`. Don't add
publishing side effects to existing stages.

Two output streams from that stage are byte-significant and must not go through
`engineering._git_output` (`engineering.py:457`), whose trailing `.strip()` is correct only for scalars
like a SHA: `git diff --binary HEAD` (stripping drops trailing whitespace on the final `+` line and the
blank line terminating a base85 block) and `git ls-files -z` (stripping eats a leading space in a
filename). Both use `testing._git_capture` (`testing.py:42`), which returns raw bytes.

## Style

Ruff, line length 100, target py313, rules `E,F,I,B,UP,SIM,ISC` (`pyproject.toml:45`).
`from __future__ import annotations` at the top of every module. Every function has a one-line
docstring stating its safety boundary — match that tone. All timestamps are `datetime.now(UTC)`; all
artifact JSON is written with `json.dumps(..., indent=2, sort_keys=True) + "\n"`.

## Not implemented

Documented elsewhere in this repository as if it were part of the architecture. It is not. Do not
build on any of it without writing it first.

- **Publisher (commit / push / open PR).** No subcommand, no code. `cli.py:386-399` is the complete
  command list. `docs/remainingwork.md` §3 specifies it; nothing implements it.
- **`pr_reviewer`.** `agents/pr_reviewer.md` exists at `status: planned`; no stage reads a diff with
  judgement. `docs/remainingwork.md` §2.
- **`ci_monitor`.** `agents/ci_monitor.md` exists at `status: planned`; no code polls checks after a
  publication. `docs/remainingwork.md` §4.
- **`repo-agent decide` verb and `latest-pending-decisions.json`.** Not in the parser choices; no code
  writes a pending-decision artifact. `docs/workspace-and-escalation-design.md` §4.3.
- **Closed `disposition` vocabulary and a Flow-B escalation path.** `disposition` is validated only as
  a string (`planning.py:225`); any value outside `_ELIGIBLE_DISPOSITIONS` is skipped by a bare
  `continue` (`engineering.py:107-108`) with no report, so an item the architect thought too
  significant to auto-fix is indistinguishable from a typo. The only escalation machinery that exists
  is Flow A's, and it covers Dependabot PRs only.
- **`active-work-item.json` lifecycle.** Written once (`engineering.py:228`), never advanced. The
  `waiting` state described in `docs/use-case-design.md` does not exist.
- **`policy` block enforcement.** See "Config files" above.
- **`status: planned` enforcement.** See "Agent definitions" above.
- **Failed-CI as a trigger source.** No workflow-run or check-run query exists anywhere;
  `_inventory_repository` (`cli.py:153`) collects open PRs and the three alert classes only. The
  check rollup read in `triage.py` judges an existing PR — it does not discover failing CI as new work.
- **Disposable per-item clone.** Nothing clones. The engineer stages require a pre-existing checkout at
  the configured `path` (`engineering.py:383`). `docs/workspace-and-escalation-design.md` §1 proposes
  replacing this; it has not been done.
- **Gate isolation.** Gates run in the same process tree that holds the write token, `/projects:rw`,
  and the state directory. `testing._gate_environment` removes token *variables* but the token is also
  a file mount. `docs/workspace-and-escalation-design.md` §2 proposes privilege separation; not built.
- **`data/runs` retention.** `agents/team_lead.md` instructs a 7-day cleanup; no code deletes anything
  under `runs/`.
- **Unattended chaining.** The daemon runs the inventory only (`cli.py:377-379`). Every other stage,
  including `pr-triage`, is a manual invocation.
- **Self-exclusion at work-item generation.** `planning.work_items` (`planning.py:148`) applies no
  author or branch-prefix filter, so a pipeline-authored PR would be planned and dispatched as new
  work. The `repo-agent/` self-exclusion exists in Flow A only (`triage.py:471`).
