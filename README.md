# Repo Agent staged repository-maintenance pipeline

This image runs a read-only GitHub inventory immediately and every 24 hours, and provides two
independent sets of commands on top of it: a deterministic Dependabot pull-request triage flow
(GitHub API and policy only), and a staged plan/implement/verify chain for one approved maintenance
item at a time. It contains Python 3.13, Node 22, Git, GitHub CLI, and an Ollama health probe.

The long-running controller is read-only with respect to repository contents: it never modifies
repositories, creates branches, or merges code. With `pr-triage --apply` it can approve a pull
request, enable GitHub's own auto-merge, and post a `@dependabot rebase` comment — it never calls a
merge directly. The write-capable stages live in a separate, profile-only service that is excluded
from `docker compose up` and must be invoked deliberately. That service can create a local branch,
apply patches to a checkout, and execute repository code; it still never commits, pushes, opens pull
requests, merges, or dismisses alerts.

There is no publishing stage. The chain ends at a tested, uncommitted branch that a human finishes.

## Command surface

These are all the commands the CLI accepts. Two flags exist: `--item` (required by the four
item-scoped stages) and `--apply` (used only by `pr-triage`).

| Command | Artifact | Service | Writes to a repository |
| --- | --- | --- | --- |
| `version` | — | either | no |
| `health` | — | either | no |
| `agents` | — | either | no |
| `run-once` / `daemon` | `latest-inventory.json` | controller | no |
| `pr-triage [--apply]` | `latest-pr-triage.json`, `latest-escalations.json` | controller | with `--apply`: approves, enables auto-merge, comments |
| `plan-once` | `latest-architect-plan.json` | controller | no |
| `dispatch-once` | `active-work-item.json` | controller | no |
| `engineer-handoff --item` | `latest-engineer-handoff.json` | controller | no |
| `engineer-preflight --item` | `latest-engineer-preflight.json` | engineer | no |
| `engineer-execute --item` | `latest-engineer-execution.json` | engineer | branch + working tree |
| `test-execute --item` | `latest-test-report.json` | engineer | disposable worktree only |

Each staged command reads the previous stage's artifact from the state directory and writes both a
timestamped artifact under `data/runs/<UTC>/` and a `latest-*.json` pointer at the state-directory
root. Stages never talk to each other in-process — the pointer file *is* the interface, and every
stage re-validates what it reads.

Stages that produce operator-facing evidence also write a Markdown companion beside the JSON —
`latest-team-lead-report.md`, `latest-pr-triage.md`, `latest-architect-plan.md`,
`latest-team-lead-dispatch.md`, `latest-engineer-handoff.md`, and `latest-test-report.md`. The JSON is
the machine interface; the Markdown is for review. `engineer-preflight` and `engineer-execute` write
JSON only.

A stage refuses to run unless the upstream artifact carries the exact expected status *and* its
work-item ID matches the `--item` argument. Failures are never raised to the caller: a stage records
`status: "blocked"` with an error string, still writes its artifact, and exits 1.

Only the inventory runs on a schedule. Everything else — including `pr-triage` — is a manual
invocation. Nothing chains the stages.

## Python dependency management

This is a `uv` project. `pyproject.toml` is the dependency declaration and `uv.lock` is the committed,
reproducible resolution. Use `uv lock` after a deliberate dependency change, `uv sync` for a local
environment, and `uv lock --check` in validation. The Dockerfile uses the pinned `uv` image and
`uv sync --locked`, so an image build fails if `uv.lock` is absent or stale.

## Agent definitions

Each role has an individual Markdown definition in `agents/`. The front matter declares its ID,
activation status, execution type, and — for model-backed roles — provider, model, temperature, and
timeout. The image includes these files at `/app/agents`; the running stack mounts them live at
`/agents`, so they can be edited without rebuilding. Inspect the effective roster with:

```bash
docker exec repo-agent repo-agent agents
```

Front-matter `status` as it currently stands:

| Role | `status` | `provider` | Invoked by |
| --- | --- | --- | --- |
| `team_lead` | active | none | `run-once`, `dispatch-once` (deterministic; body is documentation) |
| `senior_architect` | active | ollama | `plan-once` |
| `senior_architect_critic` | active | ollama | `plan-once` |
| `test_agent` | active | none | `test-execute` (deterministic; body is documentation) |
| `senior_software_engineer` | planned | ollama | `engineer-execute` — **invoked despite `planned`** |
| `pr_reviewer` | planned | ollama | nothing |
| `ci_monitor` | planned | none | nothing |

The `status` field is descriptive metadata reported by `repo-agent agents`. It is **not enforced**:
the loader validates provider, model, temperature, and timeout, but does not refuse a definition
marked `planned`. `senior_software_engineer` is the live proof — it is marked `planned` and is called
by `engineer-execute` anyway. Treat the roster listing as documentation of intent, not as a guarantee
about what can execute.

`pr_reviewer` and `ci_monitor` have definition files but no code; their stages do not exist.

## Pull-request triage (Flow A)

`pr-triage` routes open Dependabot pull requests from live GitHub state and policy alone. No clone,
no Ollama, no gate execution, and no merge call. It takes the repository list from the latest passed
inventory and then re-queries GitHub per repository.

```bash
docker exec repo-agent repo-agent pr-triage            # dry run: decides and records, acts on nothing
docker exec repo-agent repo-agent pr-triage --apply    # permits approve / enable auto-merge / comment
```

Routing, in order:

| Condition | Route |
| --- | --- |
| Head branch starts with `repo-agent/` | `report_only` — the pipeline never auto-merges its own work |
| Not authored by Dependabot | `report_only` |
| Dependabot-opened but a commit is not Dependabot's own, or not GitHub-signed | `escalate` |
| Draft | `report_only` |
| Base is not the repository's default branch | `escalate` |
| No readable `updated-dependencies:` commit trailer | `escalate` |
| Highest member of the update is major, or an unrecognised update type | `escalate` |
| Conflicted or behind base, rebase attempts remaining | `comment_rebase` (`@dependabot rebase`) |
| Conflicted or behind base, attempts exhausted | `escalate` |
| A required check is absent from the rollup | `escalate` |
| A required check failed | `escalate` |
| Checks pending or mergeability unknown, head newer than `PR_TRIAGE_PENDING_HOURS` | `requeue` |
| Same, but head older than that window | `escalate` |
| Auto-merge disabled on the repository, or the configured merge method is not allowed | `escalate` |
| Otherwise | `approve_and_enable_auto_merge` |

Two properties that must not be weakened:

- **An empty check list is not success.** The stage asserts each required context is *present*, from
  the expected app ID, and green. A repository with no CI reports zero contexts and escalates.
- **The agent never merges.** It enables GitHub's auto-merge carrying the expected head commit, so a
  pull request that moved between the read and the write is refused; only then does it record the
  approval. GitHub performs the merge when required checks pass.

Update severity comes from Dependabot's own `updated-dependencies:` commit trailer, never from the
pull-request title. A grouped update is judged at its most severe member: one major escalates the
whole group. Patch and minor may merge; every major escalates, regardless of dependency type.

Per-repository overrides live under `pr_triage:` in `repo-info.yml`:

```yaml
repositories:
  - slug: OWNER/REPOSITORY
    pr_triage:
      required_checks:
        - context: "ci / Test and build"
          app_id: 15368
      merge_method: squash        # squash | merge | rebase
      max_rebase_attempts: 3      # 1-10
```

Absent that block, the defaults are the canonical `ci / Test and build` check at app ID 15368, squash
merge, and three rebase attempts. A malformed `pr_triage` block fails that repository rather than
triaging it against no gate.

Rebase attempts are counted forward from the stage's own previous report and reset whenever the head
commit changes.

### Escalation delivery

Escalations are always recorded in `latest-pr-triage.json` and in a dedicated
`latest-escalations.json`. Delivery is a best-effort Telegram message and never gates the record:

| Variable | Meaning |
| --- | --- |
| `TELEGRAM_CHAT_ID` | destination chat |
| `TELEGRAM_BOT_TOKEN_FILE` | path to a mounted file holding the bot token |
| `TELEGRAM_TIMEOUT_SECONDS` | request timeout, default 10 |

Messages are plain text with no parse mode, because a pull-request title is attacker-influenceable.
At most 20 are sent per run; the remainder are counted in the report as `not_notified`. Without
`--apply`, notification reports `dry_run` and sends nothing. Without both variables set it reports
`unconfigured`, and the escalation record is unaffected.

`compose.yml` does not currently set any `TELEGRAM_*` variable, so as shipped the stack records
escalations and sends nothing. Add them to the **controller** service only, with the token as a file
mount — never to the engineer service, and never in `.env` or the image.

## Architect and critic planning stage (Flow B)

The planning stage is read-only and deliberately separate from the daily inventory daemon. It reads
the latest successful inventory, creates a structured plan with the architect model, and requires the
critic model to validate coverage of every open PR, security alert, and unavailable security source.
It writes both JSON and Markdown reports under `data/runs/` and updates `latest-architect-plan.json`
and `latest-architect-plan.md`.

The architect and critic must each declare `provider: ollama`, a non-empty `model`, a `temperature`
in [0, 2], and a positive `timeout_seconds`. The application records the selected definitions and
model settings in every plan artifact, and rejects incomplete architect or critic output: the plan's
item IDs must exactly match the inventory's, and the critic's verdict must be `approved` or
`changes_requested` with exact item coverage.

```bash
docker exec repo-agent repo-agent plan-once
```

`approved`, `changes_requested`, and `no_work` are completed planning outcomes. `blocked` means the
inventory was not successful, a model was not configured, Ollama failed, or either model did not
return the required complete JSON contract. This stage never changes repositories, documents,
branches, pull requests, or alerts.

Note that `disposition` on a plan item is validated only as "is a string". There is no closed
vocabulary, and no `escalate` or `decline` value is understood downstream — see the dispatcher below.

## Team-lead dispatch

After a plan and critic verdict are both `approved`, the team lead can assign exactly one item to the
senior-engineer handoff:

```bash
docker exec repo-agent repo-agent dispatch-once
```

The dispatcher evaluates architect-plan items in their declared order and only accepts explicit
`approve`, `approved`, or `remediate` dispositions. **Any other disposition is skipped silently** —
there is no report of what was passed over and why. It records its selection in
`data/active-work-item.json` with `status: "assigned"`, calls the read-only engineer handoff, and
writes JSON and Markdown dispatch reports. The daily inventory daemon does not dispatch work
automatically.

**Known limitation.** `dispatch-once` refuses to assign a second item while `active-work-item.json`
holds a non-terminal status, and no stage updates that file after the dispatcher writes it. `assigned`
is not terminal, so every later `dispatch-once` reports `already_assigned` until the file is edited or
removed by hand. Treat the pipeline as single-shot per plan and clear `data/active-work-item.json`
deliberately between items.

## Senior software engineer handoff

The handoff command requires an exact work-item ID from the latest **approved** architect plan. It
copies only that item, its acceptance criteria, and its editable repository execution metadata into
`engineer-handoff.json` and `engineer-handoff.md`. It does not clone, mount, or modify a repository;
missing repository metadata is a deliberate blocker.

```bash
docker exec repo-agent repo-agent engineer-handoff \
  --item adhatcher-org/bourbonbook:pr:53
```

Repository metadata is read from `AGENT_REPOSITORY_INFO` (default `/config/repo-info.yml`). Keep it on
the existing read-only `/config` mount. The daily controller does not mount repository code; the
separate, profile-only engineer service receives the approved `/projects` mount only when an execution
stage is run.

## Isolated engineer service

`repo-agent-engineer` is a profile-only, one-shot service. It is excluded from a normal
`docker compose up -d`, receives the write-capable `se-gh-token` directly (not the controller's whole
secrets directory), and receives the configured repository root at `/projects`. Its default command
runs `engineer-execute` against `ENGINEER_ITEM_ID`; every other stage is run by overriding the
entrypoint.

The repository checkout is **pre-provisioned, not cloned.** Nothing in this project clones anything.
Each `repo-info.yml` entry names an existing checkout via `path`, which must resolve under
`ENGINEER_REPOSITORY_ROOT` (`/projects`). For Bourbonbook, that is `/projects/bourbonbook`, backed by
`/mnt/user/Aaron_NAS/projects/bourbonbook` on Unraid.

> The shipped `config/repo-info.example.yml` shows `path: /work/REPOSITORY`, which will be rejected
> under the default `/projects` root. Use a path under `/projects`.

The preflight verifies that the path is a Git checkout and has no uncommitted changes; a branch
protects committed work but cannot protect uncommitted changes from a branch switch or an automated
edit.

```bash
docker compose --profile engineer run --rm --no-deps \
  -e ENGINEER_ITEM_ID='adhatcher-org/bourbonbook:pr:53' \
  --entrypoint repo-agent repo-agent-engineer \
  engineer-preflight --item 'adhatcher-org/bourbonbook:pr:53'
```

The preflight does not clone or change a repository. It fails safely if the item is absent, the
handoff does not match, the configured checkout is unavailable, or it has uncommitted changes. It
writes `latest-engineer-preflight.json` with `ready_for_coding` only after a valid checkout is
verified.

## Senior engineer execution

After a successful preflight, run the same profile-only service again with the exact same item ID.

For an existing GitHub pull-request item, it records that pull request and its architect decision as
`existing_pull_request_ready_for_testing`, without invoking Ollama, inspecting Git, creating a branch,
or changing files. This sends the PR's actual diff to the later test stage rather than attempting to
recreate a Dependabot change from a generated patch.

For remediation items, it re-checks that the named checkout is clean, on its configured default
branch, and exactly matches the local `origin/<default-branch>` ref. Only then does it create a
deterministic `repo-agent/engineer-*` branch — the name is generated, never chosen by the model. The
mounted `senior_software_engineer.md` definition returns a strictly validated JSON patch contract:
exact field set, one single-file `diff --git` per patch whose header matches its declared path, no
renames or copies, no absolute/`..`/`.git` paths, and `files_to_change` equal to the patch path set.
Git checks the whole patch set with `git apply --check` before applying it.

```bash
docker compose --profile engineer run --rm --no-deps \
  -e ENGINEER_ITEM_ID='OWNER/REPOSITORY:pr:NUMBER' \
  repo-agent-engineer
```

The execution report is written to `latest-engineer-execution.json`. It records the base commit,
branch, agent model/definition, changed paths, and SHA-256 digests of accepted patches — but never the
token or patch contents. Execution deliberately does not run tests, commit, push, create a pull
request, merge, or dismiss alerts.

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
only, new files the engineer created are copied into the worktree separately, respecting `.gitignore`;
symlinks are listed rather than followed. The report's `checkout` block names every file transferred
either way. Any other upstream status is a blocker.

Only configured gates run, in the order `bootstrap`, `format`, `lint`, `test`, `coverage`, `security`,
`check`. `check` is the CI-equivalent aggregate command (normally `make check`), so it must cover lock
checking, formatting, linting, tests, coverage, and security. A repository with no `test` gate is a
blocker. Each gate's command, exit code, and truncated output are recorded; a missing `check` is
reported as `skipped` and yields only `passed_partial`, never a publication-eligible pass. Gate
commands come from operator configuration, so they are split with `shlex` and run with no shell, a
bounded timeout (`TEST_GATE_TIMEOUT_SECONDS`, default 1800), and no GitHub token variable in their
environment; a command `shlex` cannot parse fails that gate rather than the run.

`minimum_coverage` is the operator's independent backstop against a pull request that edits the
project's own coverage threshold, so the percentage is read from a machine-readable `coverage.json` or
`coverage.xml` in preference to the gate's stdout. That file is still generated under
repository-controlled configuration, so this narrows what a pull request can fake rather than closing
it. When only stdout is available, one anchored `TOTAL` row is accepted and two disagreeing rows fail
the gate.

Reports are written to `test-report.json`/`test-report.md` in the run directory and to
`latest-test-report.json`/`latest-test-report.md`, on every exit path including an interrupt — a stale
pointer would otherwise be read as the current verdict. Status is `passed` only when the exact
CI-equivalent `check` command passed; `passed_partial` means no gate failed but required evidence is
absent, followed by `failed` or `blocked`. The disposable worktree is always removed, and a removal
failure is called out in the report for operator cleanup. The stage never changes the configured
checkout's working tree or index, commits, pushes, creates pull requests, merges, or dismisses alerts;
it does write `.git` metadata there (`FETCH_HEAD`, fetched objects, `.git/worktrees/<run-id>`),
without touching any ref.

Nothing consumes a `passed` test report. There is no reviewer and no publisher.

## Run through Unraid Compose

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

The external network in `compose.yml` must be shared by the Ollama container. The `ollama` hostname
must resolve on that network.

The controller runs its initial inventory immediately and repeats it every 24 hours. Change
`AGENT_RUN_INTERVAL_SECONDS` for testing; its minimum is 60 seconds. The daemon runs the inventory
only — it does not triage, plan, or dispatch.

`compose.yml` also sets `OLLAMA_ARCHITECT_MODEL` and `OLLAMA_CRITIC_MODEL`. No code reads them; model
selection comes from each agent definition's front matter.

## Configure GitHub inventory

Create the token file. Do not use `.env` for this secret:

```bash
mkdir -p /mnt/user/Aaron_NAS/projects/repo-agent/secrets
printf '%s' 'YOUR_FINE_GRAINED_GITHUB_TOKEN' > /mnt/user/Aaron_NAS/projects/repo-agent/secrets/github-token
chmod 600 /mnt/user/Aaron_NAS/projects/repo-agent/secrets/github-token
```

The token needs read access to repository metadata, pull requests, Dependabot alerts, code-scanning
alerts, and secret-scanning alerts for every configured repository. `pr-triage --apply` additionally
needs approve, auto-merge-enable, and comment authority; supply that through `PR_TRIAGE_TOKEN_FILE`
rather than widening the controller's inventory token.

Copy `config/repos.example.json` to the mounted runtime location as `repos.yml`. It uses JSON because
JSON is valid YAML and needs no additional runtime dependency:

```json
{
  "repositories": [
    {"slug": "OWNER/REPOSITORY"}
  ]
}
```

`repos.yml` says **what to monitor**. `repo-info.yml` says **how to work on it** — checkout `path`,
`default_branch`, `architecture_docs`, `quality_gates`, and `pr_triage`. Two files keyed by the same
slug, for two different purposes.

> The `policy:` block in `config/repo-info.example.yml` (`create_draft_prs`, `never_merge`,
> `require_architect_critic`, `address_severities`) is read by no code. It enforces nothing. It is
> retained in the example as a placeholder for work that has not been done.

Start through the Unraid Docker Compose UI. The controller remains running and writes
`data/latest-inventory.json`, `data/latest-team-lead-report.md`, and timestamped raw inventory plus
team-lead reports in `data/runs/`.

## Security posture

- The image builds a UID/GID `1000` `agent` user and runs as it by default, with a read-only root
  filesystem, dropped Linux capabilities, and no Docker socket. `compose.yml` overrides the runtime
  user to `99:100` to match Unraid's share ownership.
- The GitHub token is supplied from a read-only file mount; never put it in the image, Compose file,
  `.env`, or repository config.
- Model output and operator configuration are both treated as untrusted input. Every Ollama response
  is validated against a hand-written contract before it can affect anything, and no configured or
  model-supplied string is ever handed to a shell.
- Every GraphQL call sends a fixed document with values passed as separate fields, never spliced into
  the query text.

### Open risk: gate commands execute repository code

`test-execute` runs the configured `quality_gates` against the code under test. For an
`existing_pull_request_ready_for_testing` item that code is, by design, written by the pull request's
author — which is the Dependabot and fork case this pipeline exists to service.

Those commands run inside `repo-agent-engineer`, the service that also holds the write-capable
`se-gh-token` and a read-write `/projects` mount of every configured checkout, and that can reach the
state directory. `test-execute` strips `GH_TOKEN`, `GITHUB_TOKEN`, and `GH_ENTERPRISE_TOKEN` from the
gate environment, but the token is also present as a *file* mount, and `cap_drop: ALL` does not help
because no privilege escalation is required.

The defensive work in `test-execute` — treating gate stdout as hostile, refusing to derive coverage
from it alone — reduces what a hostile gate can fake in a report. It does not contain a gate that
simply writes to the mounts it can already reach. Before pointing this stage at a third-party pull
request, gates should run with no secret mounts, no state directory, no `/projects`, and no network —
with only the disposable worktree available. This is not implemented.

## Not implemented

Named in the design documents; no code exists. See `docs/implementation-status.md` for the
claim-by-claim reconciliation.

- Publisher (commit, push, open a draft PR) — the payoff step. Nothing consumes a passing test report.
- `pr_reviewer` — no stage reads a diff with judgement.
- `ci_monitor` — nothing polls checks after a publication.
- `repo-agent decide` and a pending-decision queue.
- A closed `disposition` vocabulary, and any Flow-B escalation. Flow A's escalation covers Dependabot
  pull requests only.
- `active-work-item.json` lifecycle, including the `waiting` state.
- Enforcement of the `policy` block and of agent `status`.
- Failed CI as a work-item source.
- Per-item disposable clones; checkouts are pre-provisioned.
- Gate isolation.
- `data/runs` retention.
- Unattended chaining of the stages.
