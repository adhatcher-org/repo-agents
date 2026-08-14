"""Reach GitHub through the `gh` CLI only, with the token read from a mounted file and never logged."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from subprocess import CalledProcessError, run
from typing import Any

_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")


def repository_parts(repository: object) -> tuple[str, str]:
    """Reject any slug that could smuggle a flag or a path before it reaches an argument list."""
    if not isinstance(repository, str) or not _SLUG.match(repository):
        raise RuntimeError(f"repository slug is not owner/name: {repository!r}")
    owner, _, name = repository.partition("/")
    return owner, name


def _github_environment(variable: str = "GITHUB_TOKEN_FILE") -> dict[str, str]:
    """Read a GitHub token from a mounted secret file without logging it."""
    token_file = Path(os.environ.get(variable, "/run/secrets/github-token"))
    try:
        token = token_file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(
            f"GitHub token file is unavailable: {token_file}. "
            "Create it as a read-only mounted secret."
        ) from exc
    if not token:
        raise RuntimeError(f"GitHub token file is empty: {token_file}")
    environment = os.environ.copy()
    environment["GH_TOKEN"] = token
    return environment


def _gh_json(arguments: list[str], environment: dict[str, str]) -> Any:
    """Call GitHub CLI and decode a JSON response without exposing tokens."""
    try:
        completed = run(
            ["gh", *arguments],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
    except CalledProcessError as exc:
        message = exc.stderr.strip() or exc.stdout.strip() or "unknown gh failure"
        raise RuntimeError(message) from exc
    decoder = json.JSONDecoder()
    position = 0
    values: list[Any] = []
    output = completed.stdout
    while position < len(output):
        while position < len(output) and output[position].isspace():
            position += 1
        if position == len(output):
            break
        try:
            value, position = decoder.raw_decode(output, position)
        except json.JSONDecodeError as exc:
            raise RuntimeError("GitHub CLI returned invalid JSON") from exc
        values.append(value)
    if not values:
        raise RuntimeError("GitHub CLI returned an empty response")
    return values[0] if len(values) == 1 else values


def _graphql(document: str, variables: dict[str, str], environment: dict[str, str]) -> Any:
    """Send a fixed GraphQL document with values passed as raw fields, never spliced into it."""
    arguments = ["api", "graphql", "-f", f"query={document}"]
    for name, value in sorted(variables.items()):
        if not isinstance(value, str):
            raise RuntimeError(f"GraphQL variable {name} must be a string")
        arguments.extend(["-f", f"{name}={value}"])
    payload = _gh_json(arguments, environment)
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise RuntimeError("GitHub GraphQL response did not contain a data object")
    return payload["data"]
