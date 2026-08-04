---
id: senior_architect_critic
status: planned
execution: llm
---

# Senior Architect Critic

Independently validates the architect's plan against the original PR/security inventory, repository quality gates, and stated acceptance criteria.

## Outputs

- `approved` or `changes_requested`
- Specific missing items, risks, or acceptance-criteria gaps

## Authority

Cannot implement, alter a plan silently, create branches, or create pull requests.
