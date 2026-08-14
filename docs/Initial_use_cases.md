# Initial use cases

1. PR created by dependbot in ready to review state and no merge conflicts. (Example: [https://github.com/adhatcher-org/college_planner/pulls](https://github.com/adhatcher-org/college_planner/pulls))
2. PR created by dependbot in ready to review state with merge conflicts
3. codeql findings (Example: [https://github.com/adhatcher-org/financial_analysis/security/code-scanning/10](https://github.com/adhatcher-org/financial_analysis/security/code-scanning/10))
4. Security findings. [https://github.com/adhatcher-org/frontend_api/security/dependabot/20](https://github.com/adhatcher-org/frontend_api/security/dependabot/20)
5. An empty `check-run` list must not read as green

**Ultimate goal**:  I don't want to have to deal with maintenance activitites if they don't impact the application. So things like library bumps should be merged automatically, as long as the tests are successful.

## Analysis

Three of the four cases need no clone, no LLM, and no local test execution.

`Case 1` is a policy decision: is this Dependabot, is CI green, is the bump within tolerance → merge. `Case 2` is a comment: @dependabot rebase. `Case 4` is usually a version bump, and often Dependabot has already opened the PR for it, collapsing it into case 1 or 2.

Only `case 3` — CodeQL — needs to understand code, generate a fix, and verify it locally.

That inverts the current build. The architect, critic, engineer handoff/preflight/execute, and test executor are all machinery for case 3. Dependabot PRs arrive weekly per repo; CodeQL findings arrive occasionally. The system was built for the rarest case first, and the common case has almost nothing.

## Two flows

### Flow A — PR triage. GitHub API and policy only. No workspace, no model, no gate execution

1. List open PRs, keep bot-authored dependency ones
2. Read mergeStateStatus and statusCheckRollup
3. Route:

   * conflicted → comment @dependabot rebase, requeue for the next cycle
   * checks failing → escalate, or hand to Flow B if the failure is a fixable stale-version pattern
   * checks green, bump within policy → approve and enable GitHub auto-merge
   * checks green, outside policy (major bump) → escalate with a summary

This covers cases 1, 2, and most of 4. It's a few hundred lines and no Ollama.

### Flow B — code remediation. Clone, model, local gates, push, open PR. This is case 3, and case 4 when no PR exists

The output of Flow B is a PR — which then appears to Flow A. The rule there isn't "hide our own PRs" but "Flow A may watch and report on them, and must never auto-merge them." Agent-authored code gets human review; Dependabot's doesn't need it.

### What each case needs that doesn't exist

|Case|Missing|
|---|---|
|1|mergeStateStatus + statusCheckRollup in the inventory; semver delta; approve; auto-merge|
|2|conflict detection; ability to comment on a PR; a requeue/wait state|
|3|publisher (push + open PR); everything else largely exists|
|4|alert→PR correlation; mechanical bump path|

The inventory currently selects number,title,author,headRefName,baseRefName,isDraft,reviewDecision,url — no mergeable state and no check status, so it cannot distinguish case 1 from case 2 or tell whether CI passed.

## One trap worth naming now

An empty check-run list must not read as green. A repo with no CI would return zero checks, and "nothing failed" would auto-merge it. That's the same failure shape as a bare passed meaning "no gates ran" — which we already had to fix once in the test executor. Flow A needs an explicit "required checks present and successful" test, not an absence-of-failure test.

## What survives from what's built

**Keep**: the artifact/status contract, the validation discipline, the inventory foundation, the disposable-workspace design, escalation, and the test executor — Flow B genuinely needs it.

**Reposition**: architect and critic are Flow B tools. Running two LLM calls to decide whether a patch-level Dependabot bump with green CI should merge adds risk and latency to a decision that's a policy lookup.

**Build new**: Flow A more or less entirely.

## Decisions (settled 2026-08-14)

1. **Auto-merge scope** — use the following matrix; escalate majors by email. Dependabot-authored only.

| |patch|minor|major|
|---|---|---|---|
|direct:development (ruff, pytest)|merge|merge|merge|
|direct:production|merge|merge|escalate|
|indirect|merge|merge|escalate|

2. **Who merges** — **GitHub auto-merge.** The agent approves and enables auto-merge; GitHub performs the
   merge once required checks pass and the ruleset is satisfied. The agent never calls a direct merge.
   This keeps the merge decision enforced by GitHub rather than by agent code.

3. **Case 3 ambition (CodeQL)** — **escalate before coding.** Flow B reports the alert and a suggested
   approach first; it does not open a fix PR unprompted. A wrong "fix" to a security finding is worse
   than none. When Flow B does eventually author a PR, Flow A may watch and report on it and must never
   auto-merge it — agent-authored code gets human review.

4. **Repos with no CI** — **escalate.** Never fall back to local gates as a substitute for required
   checks in Flow A. Local gates verify a candidate; they are not evidence that the repository's own
   pipeline accepted the change.

## Observed state at decision time (adhatcher-org/college_planner, 2026-08-14)

Baseline for the work, and a correction to the assumption that drove it:

- 11 open PRs (10 Dependabot, 1 human). **Zero merge conflicts** — every PR reports `MERGEABLE`.
- 9 of 11 are `UNSTABLE`. The real gates (`ci / Test and build`, `CodeQL`, `analyze / Analyze (python)`,
  `analyze / Analyze (javascript-typescript)`) pass on all of them. The sole red check is `claude-review`.
- `claude-review` fails inside `anthropics/claude-code-action@v1` with both `claude_code_oauth_token`
  and `ANTHROPIC_API_KEY` empty in the step environment. PR #26 passed the same job, so it is
  credential-flaky rather than uniformly broken.
- **No branch protection and no rulesets on `main`.** `allow_auto_merge` is already `true`; the repo is
  public and the org is on the free plan.

Consequences: the conflict cascade is a predicted failure mode, not the current blocker. The current
blocker is a Claude-powered review job — which is also the token spend this work exists to eliminate.
And with no required checks configured, GitHub auto-merge has nothing to wait on, which is exactly the
empty-check-run trap named above.

## Sequenced plan

1. **GitHub-side configuration** — scope `claude-review` away from bot dependency PRs, extend Dependabot
   `groups:` across every ecosystem, add a ruleset on `main` with the real gates as required checks.
   Config only; no agent code. Expected to clear most of the backlog on its own.
### Step 1 outcome (2026-08-14)

**Canonical required-check set: `ci / Test and build`, `integration_id: 15368`, and only that.** Verified
live on three rulesets. Flow A asserts on this exact string.

| repo | default branch | ruleset | required context |
|---|---|---|---|
| college_planner | `main` | created | `ci / Test and build` |
| financial_analysis | `main` | created (repaired a pre-existing deadlock) | `ci / Test and build` |
| schwinn_stationary_bike | `master` | normalized | `ci / Test and build` |
| bourbonbook | `main` | **pre-existing, requires `quality`** — stale, blocks all PRs | pending PR #57 |
| shared-workflows | `main` | none — no CI on its own code | escalated per decision 4 |

`claude-review` is excluded from the required set deliberately, and is unreliable regardless (intermittent
credential failure). `CodeQL` is excluded because the aggregate check is emitted by the
`github-advanced-security` app, not by our workflows.

**The naming rule holds for CI and is not yet achievable for CodeQL.** CodeQL check names depend on which
setup emits them: advanced setup (a `codeql.yml` calling the shared reusable workflow from job `analyze`)
produces the canonical `analyze / Analyze (python)`; GitHub's *default* setup produces `Analyze (python)`,
and those strings are GitHub's and cannot be renamed. bourbonbook and shared-workflows use default setup;
financial_analysis has both enabled, which makes its CodeQL fail on every run
(`analyses from advanced configurations cannot be processed when the default setup is enabled`).
Canonicalizing CodeQL therefore requires disabling default setup per repo — a security-configuration
decision, deliberately not taken unilaterally, and the reason CodeQL is not yet a required check.

**Grouping interacts with decision 1.** A catch-all Dependabot group can put a development patch and a
production major in one PR. Flow A must evaluate a grouped PR at its **highest-severity member**, so a
group containing a production major escalates entirely. The alternative — splitting development from
production — doubles lockfile-touching PRs per ecosystem and reintroduces the conflict cascade in
miniature. Collapse was chosen knowingly.

1b. **Onboarding skill** — a skill that applies this same configuration to a new repository, so the
   standard is reproducible rather than a one-time manual pass. It must *derive* from what the repo
   already has: survey existing workflows and check-run names, map them onto the canonical vocabulary,
   rename where a check exists under a nonstandard name, and create a workflow or step **only where no
   equivalent check exists at all**. It must never duplicate a check the repo already performs under a
   different name. Authored from step 1's survey output, since that is where the canonical vocabulary
   and the ruleset shape are established.

2. **Flow A** — PR triage against the GitHub API and the policy matrix. No workspace, no model, no gate
   execution. Depends on step 1 for the required-check names and group names it must assert on.
3. **Flow B** — CodeQL remediation, escalate-first per decision 3. Reuses the existing architect/critic/
   engineer/test-executor machinery plus a publisher.
