---
id: team_lead
status: active
execution: deterministic
provider: none
model: none
---

# Team Lead

Collects open pull requests and GitHub security alerts for every configured repository. Produces the consolidated maintenance briefing and, after an architect-and-critic-approved plan, dispatches exactly one eligible item to the senior software engineer handoff.

## Inputs

- GitHub inventory report
- Repository list in `config/repos.yml`

## Outputs

- `data/latest-team-lead-report.md`
- Timestamped raw inventory and briefing reports
- `data/active-work-item.json` after a successful one-item dispatch
- `data/latest-team-lead-dispatch.json` and timestamped dispatch reports

## Authority

Read-only handoff authority only. It selects the first plan-order item with an explicit `approve`,
`approved`, or `remediate` disposition, and records the engineer handoff. It never runs the
engineer, changes code, dismisses alerts, creates branches, opens pull requests, or merges code.
It refuses to assign another item while `active-work-item.json` is nonterminal.
Clean up any runs in data/runs that are more than 7 days old.
