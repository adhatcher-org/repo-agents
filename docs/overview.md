# Overview

This is the statement of intent for the project. It describes the goal, not the current build. The
"Where this stands" section at the bottom marks the difference, and
`docs/implementation-status.md` carries the claim-by-claim evidence.

## Intent

The goal of this repo is to create a set of agents that can monitor several repos in GitHub —
listed in `config/repos.yml` — for any PRs created by workflow processes/jobs, such as security
findings, Dependabot bumps, and failed CI jobs; review the findings, fix, test, verify, submit a PR
and commit, so I don't have to. There should be a set of agents to handle specific jobs. A team lead
that is in charge of monitoring GitHub for changes/failures/PRs, and based on what needs to be done,
assigns tasks to one or more agents to complete the task.

The goal is for this to be an automated process that runs inside a Docker container. The container
has a `/projects` folder where it can work on the repos, make changes, run tests, and then commit the
code from.

Issues involving major architectural changes are to be brought to my attention for me to make a
decision. Minor changes should be documented and committed, noting any changes in application
architecture/behavior based on the changes implemented (moved secrets into a vault vs. keeping them
in a local secret file...).

TL;DR: I don't want to have to deal with testing and merging PRs that are bumping versions of Python
libraries, dealing with failed CI jobs because the version of x/y/z thing is no longer valid, or deal
with simple security-related issues where vulnerabilities have been discovered that can easily be
addressed. This should all be done automatically by a set of agents that are able to handle tasks,
check each other's work, and resolve these issues.

## Two corrections to the original text

Both were factual errors, not changes of intent.

1. **The monitored repository list is `config/repos.yml` (`AGENT_CONFIG`), not
   `config/repo-info.yml`.** They are two files keyed by the same slug for two different purposes.
   `repos.yml` is *what to monitor* — a JSON list of slugs, read at `cli.py:119`. `repo-info.yml`
   (`AGENT_REPOSITORY_INFO`) is *how to work on it* — checkout path, default branch, architecture
   docs, quality gates, and triage policy, read at `engineering.py:43`.

2. **Nothing clones.** `/projects` holds **pre-provisioned checkouts**, one per repository, named by
   `path` in `repo-info.yml`. `engineer-preflight` requires that checkout to already exist, to be a
   Git repository, and to be clean (`engineering.py:380-397`); it does not create it. There is no
   `git clone` anywhere in `src/`.

   This is a deliberate choice recorded in `docs/intent-vs-implementation.md` R11: it keeps clone and
   credential handling out of the write-capable service.
   `docs/workspace-and-escalation-design.md` §1 proposes moving to a disposable per-item clone under
   `/projects/work/<slug>/<run-id>`, which would restore the original intent. That proposal is not
   implemented.

## Where this stands

The intent above is the target. What exists is roughly the first half of it.

**Working today.** Monitoring of the configured repositories on a 24-hour cycle, covering open pull
requests plus Dependabot, code-scanning, and secret-scanning alerts. Deterministic Dependabot pull
request triage: patch and minor bumps with every required check present and green are approved and
handed to GitHub's own auto-merge; conflicts get a `@dependabot rebase` comment; majors, missing or
failed checks, unparseable dependency metadata, and unexpected authorship escalate with a Telegram
notification. Separately, an architect-and-critic planning pass over the inventory, a one-item-at-a-
time dispatch, and a deterministic test executor that runs each repository's own quality gates in a
disposable worktree.

That covers the first sentence of the TL;DR — routine library bumps — for repositories that have a
required CI check. It is invoked manually, not on a schedule.

**Not working today.**

- **Nothing commits or opens a pull request.** The remediation chain ends at a tested, uncommitted
  branch in the checkout. This is the "so I don't have to" step, and it is the single largest gap.
- **Failed CI jobs are not a trigger.** They are named in the TL;DR as a first-class input. No
  workflow-run or check-run query exists as a source of new work; the check rollup read during triage
  judges an existing pull request, it does not discover a broken build.
- **There is no escalation path for major architectural changes.** Flow A escalates Dependabot pull
  requests. The planning side does not: `disposition` is validated only as a string, three values are
  actionable, and every other value — including an architect saying "this needs Aaron" — is skipped
  with no report and no queue.
- **"One or more agents" is one agent, serialized.** The dispatcher assigns exactly one item globally
  and refuses a second while one is in flight. This is a deliberate safety property (no concurrent
  write-capable containers, no overlapping Ollama jobs), and it is a real divergence from the intent
  above. It needs an explicit decision either way.
- **It is not unattended.** The daemon runs the inventory and nothing else. Triage, planning,
  dispatch, engineering, and testing are each a manual command. Compounding this, the dispatcher's
  `active-work-item.json` is written once and never advanced, so a second item cannot be assigned
  until that file is cleared by hand.
- **Nothing documents behavior or architecture changes durably.** The engineer contract carries an
  `architecture_documents_to_update` field and `repo-info.yml` lists `architecture_docs`, so the shape
  is there; nothing commits, so nothing is recorded.
