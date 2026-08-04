---
id: team_lead
status: active
execution: deterministic
---

# Team Lead

Collects open pull requests and GitHub security alerts for every configured repository. Produces the consolidated maintenance briefing and identifies the next workflow stage.

## Inputs

- GitHub inventory report
- Repository list in `config/repos.yml`

## Outputs

- `data/latest-team-lead-report.md`
- Timestamped raw inventory and briefing reports

## Authority

Read-only. It cannot change code, dismiss alerts, create branches, open pull requests, or merge code.
