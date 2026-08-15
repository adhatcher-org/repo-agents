# Handoff — dependency-automation work

Written 2026-08-14. Resume point for a session with no prior context.
Design spec is `docs/Initial_use_cases.md`; architecture is `CLAUDE.md`. Read both before changing code.

## The goal

Aaron does not want to spend time — or Claude/Codex tokens — on dependency maintenance. Library bumps
should merge themselves when the repo's own CI passes. Anything needing judgment escalates to a human.

The work splits into **Flow A** (PR triage: GitHub API + policy lookup, no clone, no model, no local gate
execution) and **Flow B** (CodeQL remediation: the existing architect/critic/engineer/test-executor
machinery plus a publisher). Flow A is deliberately LLM-free — the common case is a policy decision, not
a reasoning task.

## Settled decisions

| # | Decision |
|---|---|
| 1 | **Patch and minor merge, every major escalates**, regardless of dependency type. Dependabot-authored only. The original 3×3 matrix collapsed to one severity comparison |
| 2 | **GitHub auto-merge.** The agent approves and enables it; GitHub merges. The agent never calls a direct merge |
| 3 | **CodeQL: escalate before coding.** Flow A may watch agent-authored PRs and must never auto-merge one |
| 4 | **Repos with no CI escalate.** Never substitute local gates — they verify a candidate, not that the repo's pipeline accepted it |
| — | **Escalations deliver via Telegram**, not email (changed late on 2026-08-14). The artifact is the record; the notification is best-effort |

Decision 1's development-major cell was originally `merge`. It changed to `escalate` after
`college_planner#42` — see "Live evidence" below.

## Established facts — verified live, do not re-derive

- **Canonical required check: `ci / Test and build`, `integration_id: 15368`, and nothing else.**
- Rulesets live on `bourbonbook`, `college_planner`, `financial_analysis`, `schwinn_stationary_bike`.
  `shared-workflows` has **no ruleset and no CI on its own code** — escalated under decision 4.
- `schwinn_stationary_bike`'s default branch is **`master`**. Derive default branches; never assume.
- Canonical Dependabot group names: `python-dependencies`, `npm-dependencies`, `github-actions`, `docker`.
- `claude-review` is **not** a required check and must never be treated as evidence. It is correctly
  `skipped` on Dependabot PRs and passes on human ones. `CLAUDE_CODE_OAUTH_TOKEN` is configured at org
  level (Actions + Dependabot) and works — nothing to fix there.
- The inventory in `src/repo_agent/cli.py` did not request `mergeStateStatus` or `statusCheckRollup`, so
  it could not tell a conflicted PR from a green one. Extending it is part of Flow A.

## Two traps that must not be reimplemented wrong

1. **An empty check-run list must not read as green.** Assert the canonical context is *present AND
   successful* from the expected app id — never "nothing failed". Same failure shape as a bare `passed`
   meaning "no gates ran", already fixed once in the test executor.
2. **A grouped PR is evaluated at its highest-severity member.** One major escalates the whole group. No
   partial merges.

## Live evidence both traps matter

- **`college_planner#42`** — grouped npm bump carrying three dev majors. Failed `npm ci` with `ERESOLVE`
  in 17s: `typescript-eslint@8.67.0` declares `peer typescript ">=4.8.4 <6.1.0"`, and the group raised
  TypeScript to `^7.0.2`. Nine harmless updates were stuck behind one incompatible major. Fixed by
  `college_planner#44`, which holds TypeScript majors until `typescript-eslint` widens its peer range.
- **`college_planner#45`** — the reopened group (9 updates, TypeScript excluded). Installs cleanly, then
  **fails lint**: `eslint-plugin-react-hooks` 5→7 ships a new rule `react-hooks/set-state-in-effect`
  which finds 5 real hits in `frontend/src/main.tsx` (lines 444, 448, 629, 1001, 1341) — `setState`
  called synchronously inside `useEffect`. **Still open and blocked.**

Note the difference: #42 was a packaging incompatibility, correctly deferred with an `ignore` rule. #45
is new tooling reporting genuine pre-existing issues — suppressing it would hide a real signal.

## Status

**Done — step 1 (GitHub-side configuration).** Canonical CI check standardised across all five repos,
Dependabot grouping applied per ecosystem, `claude-review` scoped away from bot PRs, rulesets applied.
All step-1 PRs merged. Four of five repos are at zero open PRs.

**Done — step 2 (Flow A).** The deterministic, GitHub-API-only PR triage flow was merged in
[PR #14](https://github.com/adhatcher-org/repo-agents/pull/14) at `2a22858` on 2026-08-14. It delivers
the PR inventory with merge state and check rollup, deterministic artifacts and policy routing, safe
handling of missing/failed CI, conflicts, majors, grouped updates, and agent-authored PRs, plus focused
tests and review. Acting operations (approve, enable auto-merge, and Dependabot rebase comments) remain
disabled unless `pr-triage --apply` is explicitly passed; there is no direct merge action.

Telegram is documented with `TELEGRAM_CHAT_ID`, `TELEGRAM_BOT_TOKEN_FILE`, and optional
`TELEGRAM_TIMEOUT_SECONDS`; the recorded artifact remains authoritative and delivery is best effort.
The dedicated Flow A service and its least-privilege token remain intentionally unresolved: it needs
approve, auto-merge-enable, and comment authority, but neither repository write nor direct-merge
authority. Do not give the controller a write token or reuse the engineer token without a separate
design decision.

[PR #15](https://github.com/adhatcher-org/repo-agents/pull/15) is a duplicate, conflicting follow-up
branch and must not be resolved or reimplemented; its changes are superseded by merged PR #14.

## Open items

**Decisions waiting on Aaron**

1. **`college_planner#45`** — fix the five `set-state-in-effect` sites in `frontend/src/main.tsx`, or take
   the bump and disable the rule with a TODO. Option 1 is real React work; option 2 unblocks eight
   updates today without pretending the finding isn't there.
2. **`shared-workflows`** — `allow_auto_merge` is `false` and it has no self-check. It gates every other
   repo's CI, so it is the last repo that should auto-merge. Recommendation: leave auto-merge off, and
   treat "give it a real lint/validate gate" as its own piece of work.

**Completed since the original handoff**

- **CodeQL default setup.** Default setup is disabled for both `financial_analysis` and
   `bourbonbook`. `financial_analysis` now passes its existing advanced Python workflow (run
   [31840940985](https://github.com/adhatcher-org/financial_analysis/actions/runs/31840940985)).
   Bourbon Book gained the shared advanced workflow in merged
   [PR #60](https://github.com/adhatcher-org/bourbonbook/pull/60), preserving Actions,
   JavaScript/TypeScript, and Python coverage; its main-branch advanced scan passed in
   [run 31840775641](https://github.com/adhatcher-org/bourbonbook/actions/runs/31840775641).
   Both now emit canonical advanced check names. Adding `analyze / Analyze (python)` to the
   required set org-wide remains a separate follow-up.

- **Bourbon Book repo-level `CLAUDE_CODE_OAUTH_TOKEN` was deleted.** It had shadowed the
   org secret, meaning an org-token rotation could have left bourbonbook silently using the stale copy.

**Queued work**

- **Step 2.5 — CI-equivalent Makefile gates before any agent-created PR.** Before starting Flow B's
  publisher, each managed repository needs a non-mutating `make check` target. It must run format
  validation, linting, tests, coverage, and security, plus lock/dependency validation where that
  project uses it. The publisher must require the matching successful local `make check` report before
  it commits, pushes, or opens a PR; component-only results are not sufficient.

  Audit completed locally on 2026-08-14; none of the following repositories fully meets the contract:

  - `bourbonbook`: no `check` target. `pr-review` is close, but the CI-equivalent pre-PR gate needs a
    stable `make check` entry point.
  - `schwinn_stationary_bike`: no `check`, lint, or non-mutating format-check target. `local-test`
    covers tests, coverage, security, and a Docker UI test only.
  - `TNO-Portal`: no `check`, lint, format-check, or security target.
  - `financial_analysis`: has `check`, but it omits format validation; its `format` target modifies
    files and must not be used as a check.
  - `college_planner`: no `check`; its existing `format` target modifies files, so a non-mutating
    format-check must be added to the aggregate gate.
  - `giftmatcher`: no Makefile.

  Do not weaken a repository's GitHub CI while adding these targets. Retain its existing project-specific
  checks (for example type checking, dependency audits, PR hygiene, or Docker/UI checks) when they are
  already part of its CI contract.

- **Step 1b — onboarding skill.** A skill that applies this configuration to a *new* repo. Its defining
  rule: **derive from what the repo already has.** Survey existing workflows and real check-run names,
  map onto the canonical vocabulary, rename where a check exists under a nonstandard name, and create
  something only where **no equivalent check exists at all**. Never add a duplicate for a check the repo
  already performs under a different name — absence triggers creation, a name mismatch triggers a rename.
  The step-1 agent captured a recognition heuristic (classify by tool invocation, not job name) plus
  workflow templates, a parameterised `groups:` scheme, the ruleset payload, and reversal steps. Would
  live at `.claude/skills/` (nothing there yet).
- **Step 3 — Flow B.** CodeQL remediation, escalate-first per decision 3. Reuses existing machinery plus
  a publisher (push + open PR). Its output is a PR, which then appears to Flow A — which must watch it
  and never auto-merge it.

**Known repo issues**

- `bourbonbook`'s old `container` job used `scripts/docker_build.py`, which had retry-on-transient-pull
  logic the shared `app-ci.yml` lacks. Lost in the standardisation. If it matters, it belongs in
  `app-ci.yml` so all four repos benefit.
- `schwinn`'s `security-remediation-agent.yml` fails `Discover Dependabot Alerts` on every push to
  `master`. Not required, so non-blocking, but permanently red.
