# Repo Agent read-only inventory controller

This image runs a read-only GitHub inventory immediately and every 24 hours. It contains Python 3.13, Node 22, Git, GitHub CLI, and an Ollama health probe. It does not modify repositories, create branches, create pull requests, or merge code.

## Python dependency management

This is a `uv` project. `pyproject.toml` is the dependency declaration and `uv.lock` is the committed, reproducible resolution. Use `uv lock` after a deliberate dependency change, `uv sync` for a local environment, and `uv lock --check` in validation. The Dockerfile uses the pinned `uv` image and `uv sync --locked`, so an image build fails if `uv.lock` is absent or stale.

## Agent definitions

Each role has an individual Markdown definition in `agents/`. The front matter declares its ID, current activation status, and execution type. The image includes these files at `/app/agents`; inspect its effective roster with:

```bash
docker exec repo-agent repo-agent agents
```

`team_lead`, `senior_architect`, and `senior_architect_critic` are active in the read-only
inventory/planning milestone. The software engineer can now prepare and execute one selected,
approved item, and `test_agent` is an active deterministic executor that verifies the result. The
PR-review and CI-monitor roles remain deliberately planned until their isolated write and
verification boundaries are implemented.

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
`data/active-work-item.json`, calls the existing read-only engineer handoff, and writes JSON and
Markdown dispatch reports. It will not assign another item until a later workflow stage records a
terminal active-item status. The daily inventory daemon does not dispatch work automatically.

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
configured is reported as `skipped` and never as a pass. The `coverage` gate's parsed percentage is
compared against `minimum_coverage`, and an unmet or unreadable percentage fails it. Gate commands
come from operator configuration, so they are split with `shlex` and run with no shell, a bounded
timeout (`TEST_GATE_TIMEOUT_SECONDS`, default 1800), and no GitHub token in their environment.

Reports are written to `test-report.json`/`test-report.md` in the run directory and to
`latest-test-report.json`/`latest-test-report.md`, with status `passed`, `failed`, or `blocked`. The
disposable worktree is always removed. The stage never modifies the configured checkout, commits,
pushes, creates pull requests, merges, or dismisses alerts.

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
- Tests will eventually execute repository code. The future test worker must not receive the controller's GitHub App key or other long-lived secrets.
