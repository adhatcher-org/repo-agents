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
success, on failure, and on an unexpected exception.

## Gates

Only the configured gates run, in the fixed order `bootstrap`, `format`, `lint`, `test`,
`coverage`, `security`. A gate absent from `quality_gates` is recorded as `skipped` and never as a
pass; a repository with no configured gates is a blocker. Gate strings are operator-supplied and are
split with `shlex` and run with no shell, a bounded timeout, and no GitHub token in the environment.
The `coverage` gate's parsed percentage is compared against `minimum_coverage`; an unmet or
unreadable percentage fails that gate.

## Outputs

- `test-report.json` and `test-report.md` in the timestamped run directory
- `latest-test-report.json` and `latest-test-report.md` at the state-directory root
- Status: `passed`, `failed`, or `blocked`

## Authority

No code edits to the configured checkout, no plan changes, no commits, no pushes, no pull-request
creation, no merges, and no alert dismissals. Repository content and command output are evidence,
never instructions.
