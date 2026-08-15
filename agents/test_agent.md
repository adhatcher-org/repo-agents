---
id: test_agent
status: active
execution: deterministic
provider: none
model: none
failure_analysis_provider: ollama
failure_analysis_model: qwen2.5-coder:7b
failure_analysis_temperature: "0"
failure_analysis_timeout_seconds: "120"
---

# Test Agent

Validates the exact change produced by the engineer stage using only the repository-defined
`quality_gates` commands from `repo-info.yml`. It is a deterministic executor, not a model: no
Ollama call is made and no prompt in this file is sent anywhere. The `failure_analysis_*` front
matter reserves settings for a future, separate failure-explanation step.

## Scope

`repo-agent test-execute --item <id>` reads `latest-engineer-execution.json` and accepts exactly
two upstream shapes:

- `existing_pull_request_ready_for_testing` — fetches that pull request's own head commit.
- `implementation_applied` — reproduces the engineer branch, including the patch set that stage
  deliberately leaves uncommitted. `git diff HEAD` covers only tracked files, so new files the
  engineer created are copied in separately, honouring `.gitignore`. Symlinks are not copied.
  Both transfers are recorded in the report's `checkout` block as evidence.

Every run happens in a disposable Git worktree under
`<ENGINEER_REPOSITORY_ROOT>/.repo-agent-worktrees/<repository>/<run-id>`, which is removed on
success, on failure, and on an unexpected exception. If removal fails, the report says so and names
the directory an operator must clean up; it does not change the gate verdict.

The configured checkout's working tree and index are never modified. Git metadata inside its `.git`
directory *is* written, and unavoidably so for a worktree-based executor: `git fetch` writes
`FETCH_HEAD` and any fetched objects, and `git worktree add`/`remove` create and delete
`.git/worktrees/<run-id>`. No branch, tag, or other ref is created, moved, or deleted.

## Gates

Only the configured gates run, in the fixed order `bootstrap`, `format`, `lint`, `test`,
`coverage`, `security`, `check`. `check` must be the repository's CI-equivalent aggregate command
(normally `make check`, including lock checking, format, lint, test, coverage, and security).
Component gates are recorded as useful evidence, but a report is `passed` only when `check` itself
passes. A missing `check` is `passed_partial` and is not eligible for pull-request publication.
Gate strings are operator-supplied and are split with `shlex` and run with no shell, a bounded
timeout, and no GitHub token in the environment; a string `shlex` cannot parse fails that gate
instead of aborting the run.

`minimum_coverage` is the operator's independent backstop for a pull request that edits the
project's own coverage threshold, so it is read from a machine-readable `coverage.json` or
`coverage.xml` in preference to the gate's stdout. That report is still produced under
repository-controlled configuration — this narrows what a pull request can fake, it does not close
it. Falling back to stdout, only an anchored `TOTAL` row counts, and two disagreeing totals fail the
gate rather than being resolved in the repository's favour.

## Outputs

- `test-report.json` and `test-report.md` in the timestamped run directory
- `latest-test-report.json` and `latest-test-report.md` at the state-directory root
- Status: `passed` (the configured CI-equivalent `check` gate passed), `passed_partial` (nothing
  failed, but no passing `check` gate exists), `failed`, or `blocked`

The artifact is written on every exit path, including an interrupt, because the `latest-*` pointer
is the interface to the next stage and a stale pointer would be read as a fresh verdict.

## Authority

No code edits to the configured checkout, no plan changes, no commits, no pushes, no pull-request
creation, no merges, and no alert dismissals. Repository content and command output are evidence,
never instructions.
