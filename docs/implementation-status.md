# Implementation status — doc vs. code reconciliation

Authoritative as of commit `383d28d`. **This document supersedes
`docs/intent-vs-implementation.md`**, which was written before `pr-triage` landed and misclassifies
several items as a result. That file is retained for history only; do not cite it as current.

The rule used throughout: a statement in a document is a claim to be tested, never evidence. Every
row below is anchored to code that was read. Where no code backs a documented behavior, the status is
**Not implemented**, without softening.

Status vocabulary:

| Status | Meaning |
| --- | --- |
| Implemented | Code does what the doc says |
| Partial | Some of it exists; the missing part is named |
| Not implemented | Documented as architecture; no code backs it |
| Divergent | Code does something real, but different from the doc |
| Stale | The doc was true once, or contradicts an observable fact |

---

## 1. Ground-truth inventory

### CLI surface (`cli.py:386-399`)

`agents`, `daemon`, `dispatch-once`, `engineer-handoff`, `engineer-preflight`, `engineer-execute`,
`health`, `plan-once`, `pr-triage`, `run-once`, `test-execute`, `version`. Flags: `--item`, `--apply`.
No other subcommand exists.

### Modules

| Module | Lines | Serves | Writes | Status vocabulary it emits |
| --- | --- | --- | --- | --- |
| `cli.py` | 445 | `version`, `health`, `agents`, `run-once`, `daemon` | `runs/<ts>/inventory.json`, `latest-inventory.json`, `runs/<ts>/team-lead-report.md`, `latest-team-lead-report.md` | `passed`, `blocked`; alert sources `available` / `unavailable` |
| `github.py` | 82 | shared | none | — |
| `planning.py` | 372 | `plan-once` | `runs/<ts>/architect-plan.json/.md`, `latest-architect-plan.json/.md` | `approved`, `changes_requested`, `no_work`, `blocked` |
| `engineering.py` | 745 | `dispatch-once`, `engineer-handoff`, `engineer-preflight`, `engineer-execute` | `active-work-item.json`, `latest-team-lead-dispatch.json/.md`, `latest-engineer-handoff.json/.md`, `latest-engineer-preflight.json`, `latest-engineer-execution.json` (+ per-run copies) | dispatch: `assigned`, `already_assigned`, `no_eligible_work`, `blocked`; handoff: `ready_for_implementation`, `blocked`; preflight: `ready_for_coding`, `blocked`; execute: `implementation_applied`, `existing_pull_request_ready_for_testing`, `blocked` |
| `testing.py` | 509 | `test-execute` | `runs/<ts>/test-report.json/.md`, `latest-test-report.json/.md` | run: `passed`, `passed_partial`, `failed`, `blocked`; gate: `passed`, `failed`, `skipped` |
| `triage.py` | 1076 | `pr-triage` | `runs/<ts>/pr-triage.json/.md`, `latest-pr-triage.json/.md`, `latest-escalations.json` | run: `passed`, `blocked`; mode: `apply` / `dry_run`; routes: `approve_and_enable_auto_merge`, `comment_rebase`, `escalate`, `requeue`, `report_only`; checks: `satisfied`, `missing`, `failed`, `pending`; notifications: `sent`, `partial`, `failed`, `dry_run`, `unconfigured`, `nothing_to_notify` |

### Validation actually enforced

| Validator | Location | Enforces |
| --- | --- | --- |
| `_validate_architect_plan` | `planning.py:216` | items is a list; each has string `id`, *a* `disposition` of any string value, `acceptance_criteria` list; no duplicate ids; id set == inventory id set |
| `_validate_critic_response` | `planning.py:242` | `verdict` ∈ {approved, changes_requested}; `covered_item_ids` == inventory id set; `findings` is a list |
| `_validate_engineer_response` | `engineering.py:511` | exact field set; ≥1 patch; each patch exactly `{path, diff}`; diff starts `diff --git `; header matches declared path; exactly one `diff --git` per patch; no rename/copy lines; no duplicate paths; `files_to_change` == patch path set |
| `_safe_relative_path` | `engineering.py:502` | non-empty, relative, no `..`, no `.git` |
| `_workspace_path` | `engineering.py:27` | configured `path` resolves under `ENGINEER_REPOSITORY_ROOT` |
| `repository_parts` | `github.py:15` | slug matches `owner/name`, cannot smuggle a flag or path |
| `_required_check_config` | `triage.py:245` | non-empty list of `{context: str, app_id: positive int}` |
| `_triage_policy` | `triage.py:265` | `merge_method` ∈ {squash, merge, rebase}; `max_rebase_attempts` 1-10 |
| `_pull_request_number` | `testing.py:93` | PR URL shape, and the URL's owner/name equals the configured slug |
| `_worktree_destination` | `testing.py:127` | copied untracked paths resolve inside the worktree |
| `_positive_integer` | `planning.py:18` | integer ≥ 1, used for timeouts and `PR_TRIAGE_PENDING_HOURS` |

### Compose services

| Service | Command | Effective reach |
| --- | --- | --- |
| `repo-agent` | `repo-agent daemon` (`compose.yml:45`) → inventory only (`cli.py:377-379`) | read token, `/config:ro`, `/agents:ro`, `/data:rw`. No repository mount |
| `repo-agent-engineer` | `engineer-execute --item "$ENGINEER_ITEM_ID"` (`compose.yml:85-90`), `profiles: ["engineer"]` | write token `se-gh-token`, `/projects:rw`, `/data:rw` |

No compose service runs `pr-triage`, `plan-once`, `dispatch-once`, `engineer-handoff`,
`engineer-preflight`, or `test-execute`.

---

## 2. CLAUDE.md

| # | Claim | Status | Evidence |
| --- | --- | --- | --- |
| C1 | Every stage is a `repo-agent` subcommand reading the prior artifact and writing `runs/<ts>/` + `latest-*` | Implemented | `cli.py:382`; e.g. `planning.py:354-366`, `engineering.py:236-248` |
| C2 | The stage table (7 rows) is the pipeline | Stale | Omits `pr-triage` entirely (`cli.py:419`, `triage.py:1022`), and omits `agents`/`health`/`version` |
| C3 | `dispatch-once` refuses while `active-work-item.json` is non-terminal; terminal = blocked/cancelled/completed/failed | Implemented | `engineering.py:20`, `180-189` |
| C4 | It picks the first `approve`/`approved`/`remediate` item in plan order | Implemented | `engineering.py:19`, `95-110` |
| C5 | A malformed `active-work-item.json` is a hard blocker | Implemented | `engineering.py:130-135` |
| C6 | Status chain `passed → approved → ready_for_implementation → ready_for_coding → implementation_applied / existing_pull_request_ready_for_testing → passed`, and `--item` must match | Implemented | `planning.py:309`, `engineering.py:67`, `415`, `418`, `625`, `629`, `testing.py:465-470` |
| C7 | Every stage catches, sets `blocked`, still writes, returns 1 | Implemented | `cli.py:320`, `planning.py:350`, `engineering.py:232`, `351`, `433`, `724`, `testing.py:446`, `triage.py:1071` |
| C8 | Two services with different blast radius; controller never mounts repository code | Implemented | `compose.yml:21-26` vs `66-72` |
| C9 | The controller daemon does inventory only | Implemented | `cli.py:377-379` — worth stating explicitly; CLAUDE.md left it implicit |
| C10 | `agents/*.md` are mounted live, globbed, not registered in code | Implemented | `compose.yml:23`, `planning.py:37-38`, `cli.py:46-48` |
| C11 | `_agent_configuration` rejects non-Ollama providers, missing models, temperature outside [0,2], non-positive timeout | Implemented | `planning.py:58-82` |
| C12 | "Currently active: team_lead, senior_architect, senior_architect_critic, **senior_software_engineer**, test_agent" | Stale / wrong | `agents/senior_software_engineer.md:3` is `status: planned`. Four definitions are `active`. It is invoked regardless (`engineering.py:678`) because `status` is not enforced |
| C13 | `test_agent` is active but `provider: none`, never sent to a model | Implemented | `agents/test_agent.md:5`; `testing.py` never calls `_agent_configuration` |
| C14 | `pr_reviewer` and `ci_monitor` are `planned`; their boundaries are not implemented | Implemented | `agents/pr_reviewer.md:3`, `agents/ci_monitor.md:3`; no code references either id |
| C15 | Three model-output validators as described | Implemented | `planning.py:216`, `242`, `engineering.py:511` |
| C16 | Patches go through `git apply --check` before `git apply` | Implemented | `engineering.py:603-616` |
| C17 | Branch names come from `_branch_name`, never the model | Implemented | `engineering.py:496-499`, called at `engineering.py:668` |
| C18 | Gate strings are `shlex.split`, no shell, bounded timeout, tokens stripped | Implemented | `testing.py:216`, `221-229`, `52`, `198-203` |
| C19 | `test-execute` catches `ValueError` and writes in a `finally` | Implemented | `testing.py:446`, `448-450` |
| C20 | Coverage read from `coverage.json`/`coverage.xml` in preference to stdout; disagreeing totals fail | Implemented | `testing.py:284-298` |
| C21 | `repo-info.yml` holds "checkout `path`, `default_branch`, `architecture_docs`, `quality_gates`, `policy`" | Partial / misleading | The first four are read (`engineering.py:29`, `473`, `569`, `testing.py:480`). `policy` is read by **no code** — see G1. `pr_triage` is read (`triage.py:276`) and is not listed |
| C22 | Only `PyYAML` is a runtime dependency | Implemented | `pyproject.toml:10-12` |
| C23 | GitHub access via `gh` with `GH_TOKEN` from a mounted file; never logged or written into an artifact | Implemented | `github.py:23-37`; no artifact write includes the token |
| C24 | `engineer-execute` applies patches and stops; open-PR items bypass Ollama and Git | Implemented | `engineering.py:654-665`, `704` |
| C25 | Worktree path, `finally` teardown, `.git` metadata caveat | Implemented | `testing.py:66-74`, `502-507`, `108-116`, `161-176` |
| C26 | Absent gate is `skipped`; no `test` gate blocks; `passed` requires a passing `check` | Implemented | `testing.py:316-318`, `480-482`, `327-332` |
| C27 | `git diff --binary HEAD` and `git ls-files -z` must not go through `_git_output` | Implemented | `testing.py:42-49`, used at `testing.py:140`, `173` |
| C28 | Ruff rules are `E,F,I,B,UP` | Stale | `pyproject.toml:45` selects `E,F,I,B,UP,SIM,ISC` |
| C29 | `tests/test_cli.py` is "the single test module covering every source module" | Stale | `tests/test_triage.py` (381 lines) also exists |

---

## 3. README.md

| # | Claim | Status | Evidence |
| --- | --- | --- | --- |
| R-1 | "The long-running controller is read-only: it never modifies repositories, creates branches, creates pull requests, or merges code" | Partial | True of repository *contents*. With `--apply` the controller approves a PR and enables auto-merge (`triage.py:698-716`) and posts comments (`triage.py:694-696`), which causes GitHub to merge. The README's own triage section is absent, so this reads as broader than it is |
| R-2 | The 7-row command table is the pipeline | Stale | Omits `pr-triage`, `agents`, `health`, `version` (`cli.py:386-399`) |
| R-3 | Markdown companions: team-lead report, architect plan, dispatch, handoff, test report | Partial | Correct as far as it goes; omits `latest-pr-triage.md` (`triage.py:921`) |
| R-4 | "The PR-review, publish, and CI-monitor stages … are not implemented yet" | Implemented (accurate) | No code references `pr_reviewer` or `ci_monitor`; no publish subcommand |
| R-5 | "Five roles are active" | Stale / wrong | Four front matters say `active`. `senior_software_engineer.md:3` says `planned` |
| R-6 | `status` is descriptive and not enforced | Implemented (accurate) | `planning.py:35-84` never reads `status`; `cli.py:65` only echoes it |
| R-7 | Planning stage reads the latest passed inventory, validates architect and critic coverage | Implemented | `planning.py:308-346` |
| R-8 | Dispatch known limitation: `assigned` never advances, `already_assigned` forever | Implemented (accurate) | Only write is `engineering.py:228`; `assigned` ∉ `_TERMINAL_ACTIVE_STATUSES` (`engineering.py:20`) |
| R-9 | Handoff copies the item and metadata, does not clone/mount/modify; missing metadata blocks | Implemented | `engineering.py:339-350` |
| R-10 | `repo-info.yml` "maps each slug to its future isolated workspace" | Divergent | It names an existing, long-lived shared checkout. `_prepare_workspace` requires `.git` to already be present and the tree clean (`engineering.py:380-397`). Nothing isolates or provisions it |
| R-11 | Preflight writes `ready_for_coding` "only after a valid isolated checkout exists" | Divergent | Same point: the checkout is pre-provisioned and shared, not isolated (`engineering.py:426`) |
| R-12 | Execution routes existing PRs without Ollama/Git; remediation verifies clean + default branch + matches `origin/<branch>`, then creates a deterministic branch | Implemented | `engineering.py:654-669`, `472-493` |
| R-13 | Execution records base commit, branch, model, changed paths, SHA-256 of patches; never token or patch contents | Implemented | `engineering.py:590-599`, `705-723` |
| R-14 | Gate order, `check` semantics, `shlex`, timeout, token strip, coverage backstop, `finally` persistence | Implemented | `testing.py:28`, `327-332`, `206-244`, `276-308`, `403-427` |
| R-15 | "A future publisher must refuse every status other than `passed`…" | Not implemented | Correctly framed as future; recorded here so the row is not mistaken for a control |
| R-16 | "The image runs as UID/GID 1000" | Partial | `Dockerfile:42-56` creates and uses UID/GID 1000, but `compose.yml:7` and `:53` override the runtime user to `99:100` |
| R-17 | Read-only root filesystem, `cap_drop: ALL`, no Docker socket | Implemented | `compose.yml:29`, `35-36`; `Dockerfile:21-22` |
| R-18 | Open risk: gates execute repository code beside the write token and `/projects:rw` | Implemented (accurate) | `compose.yml:71-72`; `testing.py:198-203` removes token *variables* only |

---

## 4. docs/overview.md (statement of intent)

| # | Claim | Status | Evidence |
| --- | --- | --- | --- |
| O1 | Repos to monitor are listed in `config/repo-info.yml` | Stale / wrong | The monitored list is `AGENT_CONFIG` = `config/repos.yml`, parsed at `cli.py:119-130` and read at `cli.py:312-319`. `repo-info.yml` is `AGENT_REPOSITORY_INFO` (`engineering.py:24`) |
| O2 | Monitor for PRs from workflow processes: security findings, Dependabot bumps, failed CI jobs | Partial | Open PRs and Dependabot / code-scanning / secret-scanning alerts are collected (`cli.py:187-193`). **Failed CI is not a source** — no workflow-run or check-run query exists anywhere |
| O3 | Review the findings | Implemented | `planning.py:294` architect + critic, both output-validated |
| O4 | Fix | Partial | `engineer-execute` obtains a validated patch set and applies it to a branch (`engineering.py:693-704`). Nothing commits |
| O5 | Test | Implemented | `testing.py:430` |
| O6 | Verify | Partial | Critic checks architect (`planning.py:337`); test executor checks the engineer against operator-configured gates. No independent diff review — `pr_reviewer` has no code |
| O7 | Submit a PR and commit | Not implemented | No publish subcommand (`cli.py:386-399`); no `git commit`/`push`/`pr create` call in `src/` |
| O8 | A set of agents handling specific jobs | Implemented | Seven definitions in `agents/`, globbed at `cli.py:48` |
| O9 | Team lead assigns to **one or more** agents | Divergent | Exactly one item, globally, and a second is refused (`engineering.py:180-189`). Deliberate safety property; still a divergence from stated intent |
| O10 | Container has `/projects` where it can **clone** the repos | Not implemented | No `clone` call in `src/`. `engineering.py:383` requires an existing `.git` and blocks otherwise. Checkouts are pre-provisioned |
| O11 | Automated process running in a container | Partial | Only the inventory is scheduled (`cli.py:377-379`). Every other stage is manual, and the dispatcher deadlocks on `active-work-item.json` after one item |
| O12 | Major architectural changes escalate for a human decision | Not implemented | `disposition` is validated only as a string (`planning.py:225`); anything outside `_ELIGIBLE_DISPOSITIONS` is dropped by a bare `continue` (`engineering.py:107-108`) with no report and no queue. Flow A's escalation (`triage.py:720`) covers Dependabot PRs only |
| O13 | Minor changes documented and committed, noting behavior changes | Partial | `architecture_documents_to_update` is part of the engineer contract (`engineering.py:518`, `716`) and `architecture_docs` is read (`engineering.py:569`), but nothing commits, so nothing is recorded durably |
| O14 | Agents check each other's work | Partial | Critic → architect and test executor → engineer exist. No reviewer of the diff, and no reviewer of the test executor's own verdict |

---

## 5. docs/intent-vs-implementation.md (superseded)

Its findings were re-tested rather than inherited. Where it is still right, that is recorded; where
`pr-triage` changed the answer, that is recorded too.

| # | Its claim | Verdict now | Evidence |
| --- | --- | --- | --- |
| I1 | `policy` block in `repo-info.yml` is never read by any code | **Confirmed** | `config/repo-info.example.yml:16-20` defines it; no read of `create_draft_prs`, `never_merge`, `require_architect_critic`, or `address_severities` exists in `src/`. (`docs/20260815-status.md` B3 says it is echoed at `triage.py:1009` — that is imprecise: `triage.py:1009` echoes the `_triage_policy()` result derived from the separate `pr_triage:` key, not this block) |
| I2 | `status:` front matter is unenforced | **Confirmed** | `planning.py:35-84`; `senior_software_engineer.md:3` is `planned` and is invoked at `engineering.py:678` |
| I3 | `disposition` is validated only as a string | **Confirmed** | `planning.py:225` (`isinstance(entry.get("disposition"), str)`); cited in the old doc as `planning.py:224` |
| I4 | Non-eligible dispositions are silently skipped | **Confirmed** | `engineering.py:107-108` |
| I5 | `active-work-item.json` has no lifecycle past `assigned` | **Confirmed** | Single write at `engineering.py:228`; single read at `engineering.py:125`; no other module references the path |
| I6 | Publisher, `pr_reviewer`, `ci_monitor` absent | **Confirmed** | `cli.py:386-399`; no code references either agent id |
| I7 | No failed-CI trigger source | **Confirmed** | No `workflow_run`, `check-runs`, or `actions/runs` query in `src/` |
| I8 | `overview.md` names the wrong config file | **Confirmed** | See O1 |
| I9 | `overview.md` describes cloning that does not happen | **Confirmed** | See O10 |
| I10 | R2b "trigger on Dependabot version bumps — Built" | Still correct, for a different reason | At the time this meant "PRs appear in the inventory". It is now genuinely built as a routing flow: `triage.py:501-615` |
| I11 | R12 "Escalate — **Missing**. No mechanism exists at all." | **Stale** | An escalation mechanism now exists for Flow A: `_ROUTE_ESCALATE` (`triage.py:28`), recorded escalations (`triage.py:720`), a dedicated `latest-escalations.json` (`triage.py:922`), and Telegram delivery (`triage.py:768`). It does **not** cover the architect/dispatch path, so O12 remains not implemented. The blanket "no mechanism at all" is now wrong |
| I12 | "Six of fourteen are fully built" | Stale | Written before `pr-triage`; the counts and the R2/R12 rows no longer hold |

---

## 6. docs/remainingwork.md

| # | Claim | Status | Evidence |
| --- | --- | --- | --- |
| W1 | §1 "Test executor — next to build" | Stale | Built: `testing.py:430` |
| W2 | §1 gate list `bootstrap, format, lint, tests, coverage, security` | Stale | `check` was added as a seventh gate and is the one that decides `passed` (`testing.py:28`, `327-332`) |
| W3 | §1 outputs "Status: passed, failed, or blocked" | Stale | `passed_partial` also exists (`testing.py:332`) |
| W4 | §1 disposable worktree under `/projects/.repo-agent-worktrees/<repo>/<run-id>`, always removed | Implemented | `testing.py:66-74`, `506-507` |
| W5 | §2 PR reviewer (LLM diff review, `pr-review.json/.md`) | Not implemented | No code; `agents/pr_reviewer.md:3` is `planned` |
| W6 | §3 Publisher (commit, push, draft PR, `publication-report.json`) | Not implemented | No subcommand, no git commit/push call |
| W7 | §4 CI monitor (bounded polling, stale-SHA restart) | Not implemented | No code; `agents/ci_monitor.md:3` is `planned` |
| W8 | §5 Team lead becomes a consolidator across all stage reports | Not implemented | `_team_lead_report` (`cli.py:206`) renders the inventory only; it reads no other stage artifact |
| W9 | Dispatcher "advance the active item on each later daemon cycle; only select the next after the prior is terminal" | Not implemented | The first half exists (`engineering.py:181`); nothing advances the item |
| W10 | Dispatcher "skip items already completed, active, blocked, or associated with an open PR workflow" | Not implemented | Selection considers disposition only (`engineering.py:100-109`) |

---

## 7. docs/use-case-design.md

| # | Claim | Status | Evidence |
| --- | --- | --- | --- |
| U1 | Inventory must add `mergeable`, `mergeStateStatus`, `statusCheckRollup` | Implemented | `cli.py:33-37`, with a degrading field-set fallback for a read-only token (`cli.py:157-186`); triage re-reads them live via GraphQL (`triage.py:83-145`) |
| U2 | Do not parse the PR title; read Dependabot's `updated-dependencies:` commit trailer | Implemented | `_dependency_updates` (`triage.py:206`); the title is only copied into the report (`triage.py:625`) |
| U3 | Conflict → comment `@dependabot rebase`, requeue | Implemented | `triage.py:532-553`; comment body is a literal in the GraphQL document (`triage.py:147-153`), not configurable |
| U4 | Required checks failing / absent → escalate | Implemented | `triage.py:558-573` |
| U5 | Checks green and within policy → approve + enable auto-merge | Implemented | `triage.py:606-615`, acted at `triage.py:698-716` |
| U6 | PR authored by this pipeline → report only, never auto-merge | Implemented | `triage.py:471-476` |
| U7 | Escalation route is "escalate **by email**" | Divergent | Delivery is Telegram (`triage.py:81`, `736-842`). No SMTP code exists anywhere in `src/`. `docs/handoff.md` records the change to Telegram on 2026-08-14; this document was not updated |
| U8 | Auto-merge policy is a 3×3 matrix over `dependency-type` × severity | Divergent | Collapsed to a severity comparison only, with the reasoning stated in the code comment at `triage.py:49-59`: patch/minor merge, every major escalates, unrecognised ranks above major (`triage.py:52-59`, `523-530`). `dependency-type` is recorded in the report (`triage.py:511`) but never used in a decision |
| U9 | An empty check list is not success | Implemented | `_required_check_state` requires each context present, from the expected app id, and `SUCCESS` (`triage.py:398-424`); `missing` escalates (`triage.py:558`) |
| U10 | GitHub auto-merge is not a gate by itself; verify the rollup first | Implemented | Rollup is evaluated at `triage.py:555-573` before the auto-merge route is reachable at `triage.py:592` |
| U11 | The active work item needs a `waiting` state with retry count and next-check timestamp | Not implemented, and Divergent in effect | `active-work-item.json` gained nothing. The rebase counter lives in the triage artifact instead and is carried forward only while the head commit is unchanged (`triage.py:635-665`); the "next check" is simply the next manual run |
| U12 | Bounded rebase attempts, then escalate | Implemented | `triage.py:538-545`, limit from `pr_triage.max_rebase_attempts` (default 3, `triage.py:41`) |
| U13 | Flow B routed by architect disposition `remediate` / `escalate` / `decline` | Not implemented | Only `approve`/`approved`/`remediate` are recognised (`engineering.py:19`); `escalate` and `decline` have no meaning anywhere |
| U14 | Repositories without CI escalate by default | Implemented | Zero contexts → `missing` → escalate (`triage.py:417`, `558-565`) |
| U15 | Local gates are a per-repository opt-in in `repo-info.yml` giving the test executor a role in Flow A | Partial | `quality_gates` is per-repository (`testing.py:480`), but Flow A never invokes the test executor — `triage.py` runs no gate and reads no gate config |
| U16 | Escalation is "email via SMTP, credentials held by the controller only" | Not implemented | See U7 |
| U17 | `repo-agent decide --item … --approve/--reject/--defer --note` writes a state artifact the next dispatch consults | Not implemented | Not among the parser choices (`cli.py:386-399`); no pending-decision artifact is written or read |
| U18 | "Nothing produced by Flow B is ever auto-merged" — self-exclusion | Partial | Holds inside Flow A (`triage.py:471`). It does **not** hold at work-item generation: `planning.work_items` (`planning.py:148-213`) applies no author or branch-prefix filter, so a pipeline-authored PR would be planned and dispatched as fresh work |
| U19 | Sequencing item 2, "inventory fields" | Implemented | See U1 |
| U20 | Sequencing items 1, 5, 6, 7, 8 (`active-work-item` lifecycle, escalation vocabulary + `decide` + SMTP, workspace/gate isolation, publisher, failed-CI + chaining) | Not implemented | See U11, U13, U16, U17, W6, O2, O11 |

---

## 8. docs/workspace-and-escalation-design.md

The document opens with "Nothing here is implemented yet." That is still accurate for every proposal
below. Two of its recorded decisions have since been superseded elsewhere.

| # | Claim | Status | Evidence |
| --- | --- | --- | --- |
| X1 | §1 One disposable clone per work item under `/projects/work/<slug>/<run-id>` | Not implemented | No clone anywhere; the shared configured `path` and its clean/default-branch precondition remain (`engineering.py:380-397`, `472-493`) |
| X2 | §1 "What this deletes" — `_checkout_branch`, `_apply_pending`, `_copy_untracked`, `_worktree_destination`, `_git_capture` | Not implemented | All five still exist and are live: `testing.py:161`, `119`, `138`, `127`, `42` |
| X3 | §1 Remove `path` from `repo-info.yml` | Not implemented | Still required (`engineering.py:29-31`) |
| X4 | §2 Gate subprocesses under a second UID; state dir `0700`; egress denied | Not implemented | `_run_gate` passes no `user=` (`testing.py:221-229`); gates inherit the stage's UID and mounts |
| X5 | §2 Push the tested SHA, not a branch name | Not implemented | There is no push at all |
| X6 | §3 Exclude agent-authored PRs from work-item generation, as a prerequisite for the publisher | Not implemented | `planning.work_items` has no filter (`planning.py:165-183`). The `repo-agent/` check exists only in triage (`triage.py:471`) |
| X7 | §4.1 Closed disposition vocabulary; an unrecognised value blocks the plan | Not implemented | `planning.py:225` accepts any string; `engineering.py:107` skips silently |
| X8 | §4.3 `repo-agent decide` and `latest-pending-decisions.json` | Not implemented | See U17 |
| X9 | §4.4 SMTP credentials to the controller only | Superseded, not implemented | No SMTP code. The channel is Telegram (`triage.py:736-842`), configured by env + a mounted token file, and `compose.yml` sets none of those variables today |
| X10 | Decisions table: "Notification channel — SMTP email" | Stale | Contradicted by `docs/handoff.md` ("Escalations deliver via Telegram … changed late on 2026-08-14") and by the code |
| X11 | §5 Container topology table, controller row "Secrets: read token, SMTP" | Stale | No SMTP secret exists; the controller's declared secrets are the read token only (`compose.yml:26`) |
| X12 | §6 Begin honouring `policy` | Not implemented | See I1 |

---

## 9. Findings not traceable to a single doc

| # | Finding | Evidence |
| --- | --- | --- |
| G1 | The `policy:` block in `config/repo-info.example.yml:16-20` is read by no code and enforces nothing | No reference to any of its four keys in `src/` |
| G2 | `config/repo-info.example.yml:3` uses `path: /work/REPOSITORY`, which cannot pass `_workspace_path` under the shipped `ENGINEER_REPOSITORY_ROOT=/projects` | `engineering.py:32-39`, `compose.yml:62` — copying the example verbatim produces a blocked handoff |
| G3 | `compose.yml:19-20` sets `OLLAMA_ARCHITECT_MODEL` and `OLLAMA_CRITIC_MODEL`; no code reads them | Only `OLLAMA_BASE_URL` is read (`cli.py:78`, `planning.py:105`). Models come from agent front matter (`planning.py:59`) |
| G4 | `compose.yml` sets no `TELEGRAM_*` variables, so escalation delivery is `unconfigured` as shipped | `triage.py:738-744`, `800-809` |
| G5 | No compose service invokes `pr-triage`, so Flow A does not run unless a human runs it | `compose.yml:45`, `85-90` |
| G6 | `agents/team_lead.md` instructs "Clean up any runs in data/runs that are more than 7 days old"; no retention code exists | No `unlink`/`rmtree` over `runs/` in `src/`; the only `rmtree` is the worktree teardown (`testing.py:343`) |
| G7 | `pr-triage` can use a separate token file, which is the least-privilege split the handoff doc says is unresolved | `triage.py:1040-1044` (`PR_TRIAGE_TOKEN_FILE`, falling back to `GITHUB_TOKEN_FILE`). The identity question is a policy decision, not a code gap |
| G8 | Nothing consumes a `passed` test report | No module reads `latest-test-report.json` |

---

## 10. Where two documents contradict each other

| Contradiction | Which is right |
| --- | --- |
| Notification channel: `docs/use-case-design.md` §Escalation and `docs/workspace-and-escalation-design.md` §4.4 / decisions table say **SMTP email**; `docs/handoff.md` and `docs/20260815-status.md` B9 say **Telegram** | Telegram. `triage.py:81`, `736-842`. There is no SMTP code in the repository |
| Agent roster: `CLAUDE.md` and `README.md` say `senior_software_engineer` is active / "five roles are active"; `agents/senior_software_engineer.md:3` says `planned` | The front matter is the artifact `repo-agent agents` reports, so the docs are wrong. The deeper point is that neither value controls anything (`status` is unenforced) |
| Escalation existence: `docs/intent-vs-implementation.md` R12 says "no mechanism at all"; `docs/20260815-status.md` says Flow A is done including escalation | The status doc. `triage.py:720`, `922`, `768`. The reconciliation doc predates `pr-triage` |
| Workspace model: `docs/use-case-design.md` says Flow B is a "disposable clone"; `docs/intent-vs-implementation.md` R11 records a decision to keep pre-provisioned checkouts; `docs/workspace-and-escalation-design.md` §1 proposes disposable clones again | The code has pre-provisioned checkouts (`engineering.py:383`). Which way it should go is an open decision, recorded in `docs/20260815-status.md` step 5 |
| `docs/20260815-status.md` B3 says `policy` is read "outside a triage report echo (`triage.py:1009`)" | Imprecise. `triage.py:1009` echoes `_triage_policy()`, built from the separate `pr_triage:` key (`triage.py:276`). The `policy:` block is read nowhere at all |

---

## 11. Not verified

Stated plainly rather than guessed.

- **Runtime behavior.** Nothing was executed. Every row above is static reading of `383d28d`. In
  particular, no GitHub API response, Ollama response, or gate execution was observed.
- **`uv.lock`.** Referenced by `Dockerfile:48` and `Makefile:12` but not present in the staged tree,
  so its contents and staleness were not checked.
- **Anything outside this repository.** Claims in `docs/handoff.md` and `docs/20260815-status.md`
  about `college_planner`, `bourbonbook`, `financial_analysis`, `schwinn_stationary_bike`,
  `shared-workflows`, `TNO-Portal`, and `giftmatcher` — rulesets, required checks, per-repo
  `make check` targets, CodeQL setup, PR numbers, run IDs — were out of scope and are neither
  confirmed nor refuted here.
- **The canonical app ID `15368`** for `ci / Test and build` (`triage.py:37-39`) is asserted by
  `docs/handoff.md` as verified live. It was not re-verified.
- **`.github/workflows/publish-image.yml`** was read only far enough to confirm it publishes to GHCR
  on pushes to `main`; its tag-resolution logic was not audited.
