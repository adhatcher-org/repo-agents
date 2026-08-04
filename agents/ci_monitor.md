---
id: ci_monitor
status: planned
execution: deterministic
---

# CI Monitor

Will monitor the required GitHub Actions checks for a created draft pull request and report the final check state with failed-job evidence when applicable.

## Authority

Read-only with respect to GitHub Actions. Cannot rerun, dismiss, approve, or merge checks.
