# Code-quality bot remediation plan

Remediation for the `github-code-quality[bot]` review on
[PR #11](https://github.com/adhatcher-org/repo-agents/pull/11) (submitted 2026-08-14, state
`COMMENTED`, no blocking verdict). That review is the only bot output on this repository: PRs #1–#10
carry zero review comments, and `gh api .../code-scanning/alerts` returns nothing, so there are no
CodeQL/Dependabot alerts and no open issues to fold in.

Both findings landed on `main` with the PR merge and are still present in the working tree.

## Findings

| # | Location | Bot rule | Bot message |
| --- | --- | --- | --- |
| F1 | `src/repo_agent/testing.py:335-338` | Empty except | "'except' clause does nothing but pass and there is no explanatory comment." |
| F2 | `src/repo_agent/testing.py:350-351` | Implicit string concatenation in a list | "Implicit string concatenation. Maybe missing a comma?" |

Both are real style defects, not false positives — `ruff` agrees with the bot on each, under rules
this project does not currently select (see Step 3).

### F1 — silent `except ... : pass` in `_remove_worktree`

```python
def _remove_worktree(workspace: Path, worktree: Path) -> bool:
    """Remove the disposable worktree unconditionally without masking the gate results."""
    for arguments in (["worktree", "remove", "--force", str(worktree)], ["worktree", "prune"]):
        try:
            _git_output(workspace, arguments)
        except (OSError, RuntimeError):
            pass
```

The swallow is **deliberate and correct**, and the exception set is right: `_git_output`
(`engineering.py:457`) converts `CalledProcessError` into `RuntimeError` and lets `OSError` through
from `subprocess.run`, so those two cover every way `git worktree remove/prune` can fail. The
authority on success is the filesystem check two lines below (`worktree.exists()` → `shutil.rmtree`
fallback → the returned bool), not git's exit status — and that bool is what
`test-execute` reports as `worktree_removed`, including the operator-cleanup warning in the Markdown
when it is `False`. Nothing is masked.

What the bot is right about is that a bare `pass` does not say any of that at the call site. This is
a readability fix only; **no behavior changes.**

### F2 — unparenthesized implicit concatenation inside a list literal

```python
    lines = [
        "# Test execution report",
        "",
        f"- Status: **{report['status']}**",
        "- Mode: disposable worktree only; nothing committed, pushed, published, merged, "
        "or dismissed.",
        f"- Engineer execution: `{report['execution_path']}`",
```

There is no missing comma — the two fragments are one intentionally wrapped line under the 100-column
limit. But in a list of report lines, a wrap that looks exactly like a typo'd element is a genuine
hazard: a future edit that adds the comma silently splits one Markdown bullet into two, and the
report is the operator-facing evidence artifact. This is also a readability fix; **no behavior
changes** and the rendered Markdown is byte-identical.

The repository already has the correct pattern for this — `engineering.py:306-311` wraps a four-line
string in explicit parentheses inside a list, and neither the bot nor ruff flags it. F2 should be
made to match that.

## Plan

### Step 1 — Fix F1 (`src/repo_agent/testing.py`)

Replace the bare `try`/`except`/`pass` with `contextlib.suppress`, plus one comment stating why the
failure is ignored. `suppress` states the intent in code; the comment states the safety boundary, in
the same tone as the module's docstrings.

```python
    for arguments in (["worktree", "remove", "--force", str(worktree)], ["worktree", "prune"]):
        # Git's exit status is not the authority here: the filesystem check below decides, and the
        # rmtree fallback plus the returned bool report the real outcome to the operator.
        with contextlib.suppress(OSError, RuntimeError):
            _git_output(workspace, arguments)
```

Add `import contextlib` to the module's stdlib import block (`I` keeps it ordered).

### Step 2 — Fix F2 (`src/repo_agent/testing.py`)

Wrap the fragments in explicit parentheses, matching `engineering.py:306-311`:

```python
    lines = [
        ...,
        (
            "- Mode: disposable worktree only; nothing committed, pushed, published, merged, "
            "or dismissed."
        ),
        ...,
    ]
```

Leave the surrounding f-string elements alone; only this element changes.

### Step 3 — Close the gap that let both land

`pyproject.toml` selects `["E", "F", "I", "B", "UP"]`, which is why `make check` passed while the bot
flagged the file. Ruff already ships the exact two rules:

- `SIM105` (`suppressible-exception`) → F1
- `ISC004` (`implicit-string-concatenation-in-collection-literal`) → F2

Enable the full `SIM` and `ISC` families:

```toml
select = ["E", "F", "I", "B", "UP", "SIM", "ISC"]
```

Measured before proposing it, both families cost nothing beyond the two known findings:

- `ruff check --select SIM --statistics .` → 1 error, all of it `SIM105` (F1).
- `ruff check --select ISC --statistics .` → 1 error, all of it `ISC004` (F2).
- `ruff format --check` under the proposed select set → "21 files already formatted", so the usual
  `ISC001`/formatter conflict does not apply to this codebase.

After Steps 1 and 2, `make lint` is clean with the wider set, and the same class of finding fails CI
locally instead of arriving as a post-merge bot comment.

Deliberately **out of scope**: `TRY` (108 `TRY003`, 29 `TRY004`, 16 `TRY301`). That family objects to
this codebase's core convention of raising `RuntimeError` with a descriptive message inside the
try-blocks that stages catch and convert into `status: "blocked"`. Adopting it would be an
architecture change, not a lint fix, and nothing has flagged it.

### Step 4 — Verify

```bash
make check
```

That is lock-check + format-check + lint + test + coverage + security — the same target CI runs.
Expectations:

- `tests/test_cli.py` needs **no changes**. Both fixes are behavior-preserving, and the existing
  coverage of the touched code stays valid:
  `test_remove_worktree_falls_back_to_deleting_the_directory` (`tests/test_cli.py:1938`) exercises
  the suppressed-failure path directly by passing a missing workspace, and the `worktree_removed`
  assertions at lines 1565, 1622, 1656, and 2237 cover the success path.
- Coverage stays above the 90% `fail_under`; no new branches are introduced (`contextlib.suppress`
  replaces an existing handler one-for-one).
- If any test asserting the Markdown "Mode:" bullet were to fail, that would mean the parenthesized
  string changed the output — it must not, and that is the regression signal to watch.

### Step 5 — Land it

Branch off `main`, single commit, small diff (two hunks in `src/repo_agent/testing.py`, one import,
one line in `pyproject.toml`), PR against `adhatcher-org/repo-agents`. Confirm on the new PR that
`github-code-quality[bot]` returns no comments.

Note: `agents/team_lead.md` is currently modified in the working tree and is unrelated to this
remediation — keep it out of the commit.

## Risk

Low. No stage boundary, artifact schema, status value, or gate semantics is touched. The one thing to
protect is F2's rendered output: the parentheses must not introduce a comma, or one Markdown bullet in
the test-execution report becomes two.
