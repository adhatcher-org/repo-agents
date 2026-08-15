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

**In review — step 2.5, the repo-agent half.**
[PR #17](https://github.com/adhatcher-org/repo-agents/pull/17) makes a `passed` test report mean the
repository's CI-equivalent aggregate actually ran. `check` joins the gate order; `passed` is reserved for
a run where that gate itself passed, and everything else that did not fail is `passed_partial`, which is
explicitly not eligible for publication.

This **inverts** the prior rule rather than extending it. Before, any skipped gate downgraded the run;
now a passing `check` is the single positive signal, because the aggregate is what CI actually executes
and the component gates are corroborating evidence rather than a substitute. `pr_reviewer` may no longer
create a pull request without a matching `latest-test-report.json` at `status: passed` carrying a passing
`check` for that exact repository, work item and tested commit. `test_agent` documents the new order and
semantics, and `config/repo-info.example.yml` gains `check: make check`.

The same PR removes a dead `_REBASE_COMMENT` constant in `triage.py` (reported by
`github-code-quality` on #16). It was never read — the body is a literal inside `_COMMENT_MUTATION`'s
GraphQL string — and it was deleted rather than wired up, so the comment body stays non-configurable and
cannot be influenced by config or untrusted input.

### Do not open pull requests from `agent/flow-a-pr-triage`

That branch has now produced three: #14 (merged), [#15](https://github.com/adhatcher-org/repo-agents/pull/15)
and [#16](https://github.com/adhatcher-org/repo-agents/pull/16), both closed as duplicates. **#14 was
squash-merged**, so the branch's original commits look unmerged even though their content landed in
`2a22858`. Merging `main` back in produces `add/add` conflicts on `triage.py`, `tests/test_triage.py` and
`docs/handoff.md` — git treating main's squashed copies and the branch's originals as unrelated files.

Do not resolve those conflicts; that is hand-merging already-merged content. If something genuinely new
is on that branch, cherry-pick just that commit onto current `main`, which is how #17 was produced — it
applied cleanly, confirming the new work was independent of the #14 content all along. **Deleting the
branch would stop this recurring.**

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

- **Step 2.5 — the per-repository half.** The enforcement side is done in PR #17 above: `repo-agent` now
  refuses to treat a report as `passed` without a passing `check` gate. What remains is giving each
  managed repository the `make check` target that gate invokes. Until a repo has one, its runs report
  `passed_partial` and are correctly ineligible for publication — the contract fails closed, so this is
  not urgent, but Flow B's publisher is blocked on it.

  Each target must be **non-mutating** and run format validation, linting, tests, coverage, and security,
  plus lock/dependency validation where the project uses it.

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

**Local environment — blocks running `make check` here**

`.venv` in this checkout is being corrupted repeatedly by a file-sync tool. `make check` fails with
`ModuleNotFoundError: No module named 'repo_agent'` even though the editable-install `.pth` file exists
and points at a valid path — `src` never reaches `sys.path`. The tell is duplicate files with numeric
suffixes: `__editable__.repo_agent-0.1.0 2.pth`, `... 3.pth`, matching the `backend/.coverage 2` / `3`
files seen in the college_planner checkout.

Workaround is `rm -rf .venv && make install`. **This is not a one-off** — it recurred within minutes
during the 2026-08-14 session, so the sync is active. The repo lives under `~/Documents`, which iCloud
Drive syncs by default; excluding it or moving the checkout outside the synced tree is the real fix.

This matters beyond convenience: step 2.5 makes agent-created PRs depend on a *local* `make check`
result, so a corrupted venv will make the publisher fail or, worse, report a misleading gate outcome.

**Known repo issues**

- `bourbonbook`'s old `container` job used `scripts/docker_build.py`, which had retry-on-transient-pull
  logic the shared `app-ci.yml` lacks. Lost in the standardisation. If it matters, it belongs in
  `app-ci.yml` so all four repos benefit.
- `schwinn`'s `security-remediation-agent.yml` fails `Discover Dependabot Alerts` on every push to
  `master`. Not required, so non-blocking, but permanently red.
