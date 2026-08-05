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
inventory/planning milestone. The software engineer can now prepare one selected, approved item
for a later execution stage. The test, PR-review, and CI-monitor roles remain deliberately planned
until their isolated write and verification boundaries are implemented.

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
quality gates, and PR policy. Keep it on the existing read-only `/config` mount. Before any future
implementation command is enabled, mount a dedicated writable `/work` directory containing only the
selected repository or a disposable worktree—not the whole NAS projects directory.

## Isolated engineer service

`repo-agent-engineer` is a profile-only, one-shot service. It is excluded from a normal
`docker compose up -d`, receives the write-capable `se-gh-token` directly (not the controller's
whole secrets directory), and can write only the Bourbonbook worktree mounted at
`/work/bourbonbook`. Its initial command is a preflight: it verifies that the selected item and a
Git checkout exist, then re-runs the read-only handoff inside the isolated runtime. The future
implementation command will replace this preflight after its write controls are implemented.

Create the dedicated host workspace first; do not point it at the shared projects directory. On
Unraid, give the container user (UID 99/GID 100) ownership so it can create the clone:

```bash
mkdir -p /mnt/user/app_cache/repo-agent/work/bourbonbook
chown -R nobody:users /mnt/user/app_cache/repo-agent/work
```

Then run the isolated preflight manually from the Compose project directory:

```bash
ENGINEER_ITEM_ID=adhatcher-org/bourbonbook:pr:53 \
  docker compose --profile engineer run --rm repo-agent-engineer
```

If the workspace is empty, the service uses `se-gh-token` to clone only
`adhatcher-org/bourbonbook`; it never overwrites a non-empty non-Git directory. The command fails
safely if the ID is absent or the workspace cannot become a Git checkout. The daily `repo-agent`
controller remains running without access to `se-gh-token`.

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
