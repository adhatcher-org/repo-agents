---
id: test_agent
status: planned
execution: deterministic
provider: none
model: none
failure_analysis_provider: ollama
failure_analysis_model: qwen2.5-coder:7b
failure_analysis_temperature: "0"
failure_analysis_timeout_seconds: "120"
---

# Test Agent

Will validate the exact implementation commit using the repository-defined test, coverage, lint, formatting, and security commands.

## Outputs

- Structured command, exit-code, coverage, and failure evidence

## Authority

No code edits, plan changes, PR creation, or merge authority.
