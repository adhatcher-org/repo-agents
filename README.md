# Repo Agent staged repository-maintenance pipeline

This image runs a read-only GitHub inventory immediately and every 24 hours, and provides the later
staged commands that plan, implement, and verify one approved maintenance item at a time. It
contains Python 3.13, Node 22, Git, GitHub CLI, and an Ollama health probe.

The long-running controller is read-only: it never modifies repositories, creates branches, creates
pull requests, or merges code. The write-capable stages live in a separate, profile-only service
that is excluded from `docker compose up` and must be invoked deliberately. That service can create
a local branch, apply patches to a checkout, and execute repository code; it still never commits,
pushes, opens pull requests, merges, or dismisses alerts.

## Pipeline stages

Every stage is a subcommand of the same `repo-agent` CLI. Each reads the previous stage's artifact
from the state directory and writes both a timestamped artifact under `data/runs/<UTC>/` and a
`latest-*.json` pointer at the state-directory root. Stages never talk to each other in-process —
the pointer file *is* the interface, and every stage re-validates what it reads.

| Command | Artifact | Service | Writes to a repository |
| --- | --- | --- | --- |
| `run-once` / `daemon` | `latest-inventory.json` | controller | no |
| `plan-once` | `latest-architect-plan.json` | controller | no |
| `dispatch-once` | `active-work-item.json` | controller | no |
| `engineer-handoff` | `latest-engineer-handoff.json` | controller | no |
| `engineer-preflight` | `latest-engineer-preflight.json` | engineer | no |
| `engineer-execute` | `latest-engineer-execution.json` | engineer | branch + working tree |
| `test-execute` | `latest-test-report.json` | engineer | disposable worktree only |

Stages that produce operator-facing evidence also write a Markdown companion beside the JSON —
`latest-team-lead-report.md`, `latest-architect-plan.md`, `latest-team-lead-dispatch.md`,
`latest-engineer-handoff.md`, and `latest-test-report.md`. The JSON is the machine interface; the
Markdown is for review.

A stage refuses to run unless the upstream artifact carries the exact expected status *and* its
work-item ID matches the `--item` argument. Failures are never raised to the caller: a stage records
`status: "blocked"` with an error string, still writes its artifact, and exits 1.

The PR-review, publish, and CI-monitor stages in `docs/remainingwork.md` are not implemented yet.

## Python dependency management

This is a `uv` project. `pyproject.toml` is the dependency declaration and `uv.lock` is the committed, reproducible resolution. Use `uv lock` after a deliberate dependency change, `uv sync` for a local environment, and `uv lock --check` in validation. The Dockerfile uses the pinned `uv` image and `uv sync --locked`, so an image build fails if `uv.lock` is absent or stale.

## Agent definitions

Each role has an individual Markdown definition in `agents/`. The front matter declares its ID, current activation status, and execution type. The image includes these files at `/app/agents`; inspect its effective roster with:

```bash
docker exec repo-agent repo-agent agents
```

Five roles are active. `senior_architect`, `senior_architect_critic`, and
`senior_software_engineer` are Ollama roles: their Markdown body is the system prompt and their
front matter sets the model, temperature, and timeout. `team_lead` and `test_agent` are active but
deterministic — they declare `provider: none`, are never sent to a model, and their bodies document
the role's authority boundary rather than prompting it.

`pr_reviewer` and `ci_monitor` remain `status: planned`; their stages are not implemented.

The `status` field is currently descriptive metadata reported by `repo-agent agents`. It is not
enforced — `_agent_configuration` validates provider, model, temperature, and timeout, but does not
refuse a definition marked `planned`. Treat the roster listing as documentation of intent, not as a
guarantee about what can execute.

## Architect and critic planning stage

The active planning stage is read-only and deliberately separate from the daily inventory daemon.
It reads the latest successful inventory, creates a structured plan with the architect model, and
requires the critic model to validate coverage of every open PR, security alert, and unavailable
security source. It writes both JSON and Markdown reports under `data/runs/` and updates
`latest-architect-plan.json` and `latest-architect-plan.md`.

Every agent definition is a mounted, live configuration file. Its front matter declares the role's
provider, model, temperature, and timeout; its Markdown body is the prompt. Tune either without
rebuilding the image. The architect and critic must each declare `provider: ollama`, a non-empty
`model`, `temperature`, and `timeout_seconds`. The application records the selected definitions and
model settings in every plan artifact, while still rejecting incomplete architect or critic output.

Run the stage manually after reviewing the inventory:

```bash
docker exec repo-agent repo-agent plan-once
```

`approved`, `changes_requested`, and `no_work` are completed planning outcomes. `blocked` means
the inventory was not successful, a model was not configured, Ollama failed, or either model did
not return the required complete JSON contract. This stage never changes repositories, documents,
branches, pull requests, or alerts.

## Team-lead dispatch

After a plan and critic verdict are both `approved`, the team lead can assign exactly one item to
the senior-engineer handoff:

```bash
docker exec repo-agent repo-agent dispatch-once
```

The dispatcher evaluates architect-plan items in their declared order and only accepts explicit
`approve`, `approved`, or `remediate` dispositions. It records its selection in
`data/active-work-item.json` with `status: "assigned"`, calls the read-only engineer handoff, and
writes JSON and Markdown dispatch reports. The daily inventory daemon does not dispatch work
automatically.

**Known limitation.** `dispatch-once` refuses to assign a second item while `active-work-item.json`
holds a non-terminal status, and no stage currently updates that file after the dispatcher writes
it. `assigned` is not terminal, so every later `dispatch-once` reports `already_assigned` until the
file is edited or removed by hand. Advancing the active item as its stage reports complete — and
only selecting the next item once the prior one is terminal — is specified in
`docs/remainingwork.md` but not yet implemented. Until it is, treat the pipeline as single-shot per
plan and clear `data/active-work-item.json` deliberately between items.

## Senior software engineer handoff

The handoff command requires an exact work-item ID from the latest **approved** architect plan. It
copies only that item, its acceptance criteria, and its editable repository execution metadata into
`engineer-handoff.json` and `engineer-handoff.md`. It does not clone, mount, or modify a repository;
missing repository metadata is a deliberate blocker.

```bash
docker exec repo-agent repo-agent engineer-handoff \
  --item adhatcher-org/bourbonbook:pr:53
```

Repository metadata is read from `AGENT_REPOSITORY_INFO` (default
`/config/repo-info.yml`). It maps each slug to its future isolated workspace, architecture documents,
quality gates, and PR policy. Keep it on the existing read-only `/config` mount. The daily controller
does not mount repository code; the separate, profile-only engineer service receives the explicitly
approved `/projects` mount only when an execution stage is run.

## Isolated engineer service

`repo-agent-engineer` is a profile-only, one-shot service. It is excluded from a normal
`docker compose up -d`, receives the write-capable `se-gh-token` directly (not the controller's
whole secrets directory), and receives the configured repository root at `/projects`. The selected
repository is taken from the approved handoff and its editable `repo-info.yml` entry; its path must
remain contained under `/projects`.

For Bourbonbook, retain the configured path `/projects/bourbonbook`, backed by
`/mnt/user/Aaron_NAS/projects/bourbonbook` on Unraid. The preflight verifies that this is a Git
checkout and has no uncommitted changes; a branch protects committed work but cannot protect
uncommitted changes from a branch switch or an automated edit.

Then run the isolated preflight manually from the Compose project directory:

```bash
docker compose --profile engineer run --rm --no-deps \
  -e ENGINEER_ITEM_ID='adhatcher-org/bourbonbook:pr:53' \
  --entrypoint repo-agent repo-agent-engineer \
  engineer-preflight --item 'adhatcher-org/bourbonbook:pr:53'
```

The preflight does not clone or change a repository. It fails safely if the item is absent, the
handoff does not match, the configured checkout is unavailable, or it has uncommitted changes. It writes
`latest-engineer-preflight.json` with `ready_for_coding` only after a valid isolated checkout exists.

## Senior engineer execution

After a successful preflight, run the same profile-only service again with the exact same item ID.
For an existing GitHub pull-request item, it records that pull request and its architect decision as
`existing_pull_request_ready_for_testing`, without invoking Ollama, inspecting Git, creating a branch, or
changing files. This sends the PR's actual diff to the later test/review stages rather than attempting to
recreate a Dependabot change from a generated patch.

For remediation items, it re-checks that the named checkout is clean, on its configured default branch, and
exactly matches the local `origin/<default-branch>` ref. Only then does it create a deterministic
`repo-agent/engineer-*` branch. The mounted `senior_software_engineer.md` definition returns a strictly
validated JSON patch contract. Git checks every patch before applying it.

```bash
docker compose --profile engineer run --rm --no-deps \
  -e ENGINEER_ITEM_ID='OWNER/REPOSITORY:pr:NUMBER' \
  repo-agent-engineer
```

The execution report is written to `latest-engineer-execution.json`. It records the base commit, branch,
agent model/definition, changed paths, and SHA-256 digests of accepted patches—but never the token or patch
contents. Execution deliberately does not run tests, commit, push, create a pull request, merge, or dismiss
alerts. The test stage below validates the dirty feature branch before any publishing action.

The daily `repo-agent` controller remains running without access to `se-gh-token`.

## Test execution

`test-execute` is deterministic: it never calls Ollama. It reads `latest-engineer-execution.json`,
re-reads the repository's `quality_gates` from `repo-info.yml`, and runs them inside a disposable Git
worktree at `<ENGINEER_REPOSITORY_ROOT>/.repo-agent-worktrees/<repository>/<run-id>`.

```bash
docker compose --profile engineer run --rm --no-deps \
  --entrypoint repo-agent repo-agent-engineer \
  test-execute --item 'OWNER/REPOSITORY:pr:NUMBER'
```

For an `existing_pull_request_ready_for_testing` execution it fetches that pull request's own head
commit; for an `implementation_applied` execution it reproduces the engineer branch together with the
patch set that stage deliberately left uncommitted. Because `git diff HEAD` describes tracked files
only, new files the engineer created are copied into the worktree separately, respecting
`.gitignore`; symlinks are listed rather than followed. The report's `checkout` block names every
file transferred either way. Any other upstream status is a blocker.

Only configured gates run, in the order `bootstrap`, `format`, `lint`, `test`, `coverage`,
`security`. Each gate's command, exit code, and truncated output are recorded; a gate that is not
configured is reported as `skipped` and never as a pass, and a repository without a `test` gate is
refused outright. Gate commands come from operator configuration, so they are split with `shlex` and
run with no shell, a bounded timeout (`TEST_GATE_TIMEOUT_SECONDS`, default 1800), and no GitHub
token in their environment; a command `shlex` cannot parse fails that gate rather than the run.

`minimum_coverage` is the operator's independent backstop against a pull request that edits the
project's own coverage threshold, so the percentage is read from a machine-readable `coverage.json`
or `coverage.xml` in preference to the gate's stdout. That file is still generated under
repository-controlled configuration, so this narrows what a pull request can fake rather than
closing it. When only stdout is available, one anchored `TOTAL` row is accepted and two disagreeing
rows fail the gate.

Reports are written to `test-report.json`/`test-report.md` in the run directory and to
`latest-test-report.json`/`latest-test-report.md`, on every exit path including an interrupt — a
stale pointer would otherwise be read as the current verdict. Status is `passed` (all six gates ran
and passed), `passed_partial` (nothing failed but a gate was unconfigured), `failed`, or `blocked`.
The disposable worktree is always removed, and a removal failure is called out in the report for
operator cleanup. The stage never changes the configured checkout's working tree or index, commits,
pushes, creates pull requests, merges, or dismisses alerts; it does write `.git` metadata there
(`FETCH_HEAD`, fetched objects, `.git/worktrees/<run-id>`), without touching any ref.

## Run through Unraid Compose

After the first GitHub Actions release, Compose pulls the published image:

```bash
docker compose pull
docker compose up -d
```

For local image development, replace the `image:` line temporarily with a `build:` block:

```yaml
build:
  context: .
image: repo-agent:v1
pull_policy: build
```

The external network in `compose.yml` must be shared by the Ollama container. The `ollama` hostname must resolve on that network.

The controller runs its initial inventory immediately and repeats it every 24 hours. Change `AGENT_RUN_INTERVAL_SECONDS` for testing; its minimum is 60 seconds.

## Configure GitHub inventory

Create the token file. Do not use `.env` for this secret:

```bash
mkdir -p /mnt/user/Aaron_NAS/projects/repo-agent/secrets
printf '%s' 'YOUR_FINE_GRAINED_GITHUB_TOKEN' > /mnt/user/Aaron_NAS/projects/repo-agent/secrets/github-token
chmod 600 /mnt/user/Aaron_NAS/projects/repo-agent/secrets/github-token
```

The token needs read access to repository metadata, pull requests, Dependabot alerts, code-scanning alerts, and secret-scanning alerts for every configured repository. Prefer a GitHub App with short-lived tokens in the later write-enabled stage.

Copy `config/repos.example.json` to the mounted runtime location as `repos.yml`. It uses JSON because JSON is valid YAML and needs no additional runtime dependency:

```json
{
  "repositories": [
    {"slug": "OWNER/REPOSITORY"}
  ]
}
```

Start through the Unraid Docker Compose UI. The controller remains running and writes `data/latest-inventory.json`, `data/latest-team-lead-report.md`, and timestamped raw inventory plus team-lead reports in `data/runs/`.

## Security posture

- The image runs as UID/GID `1000`, has a read-only root filesystem, drops Linux capabilities, and needs no Docker socket.
- The GitHub token is supplied from a read-only `/run/secrets/github-token` mount; never put it in the image, Compose file, `.env`, or repository config.
- Model output and operator configuration are both treated as untrusted input. Every Ollama response
  is validated against a hand-written contract before it can affect anything, and no configured or
  model-supplied string is ever handed to a shell.

### Open risk: gate commands execute repository code

`test-execute` runs the configured `quality_gates` against the code under test. For an
`existing_pull_request_ready_for_testing` item that code is, by design, written by the pull
request's author — which is the Dependabot and fork case this pipeline exists to service.

Those commands currently run inside `repo-agent-engineer`, the service that also holds the
write-capable `se-gh-token` and a read-write `/projects` mount of every configured checkout, and
that can reach the state directory. `test-execute` strips `GH_TOKEN`, `GITHUB_TOKEN`, and
`GH_ENTERPRISE_TOKEN` from the gate environment, but the token is also present as a *file* mount,
and `cap_drop: ALL` does not help because no privilege escalation is required.

The defensive work in `test-execute` — treating gate stdout as hostile, refusing to derive coverage
from it alone — reduces what a hostile gate can fake in a report. It does not contain a gate that
simply writes to the mounts it can already reach. Before pointing this stage at a third-party pull
request, gates should run in a minimal isolated container with no secret mounts, no state
directory, no `/projects`, and no network — with only the disposable worktree bound in.
