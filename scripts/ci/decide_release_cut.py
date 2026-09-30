#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Release-cut decision step (OMN-18010 follow-up: explicit-trigger releases).

WHY THIS EXISTS
---------------
``release-on-merge.yml`` published a release on every push to ``dev`` that
touched packaged source. Measured cost: on 2026-09-27, HEAD~4 on ``dev`` was
``chore(release): open dev for v0.4.259 [release-on-merge]`` and HEAD~2 was the
same shape for v0.4.258 — every other commit on the branch was the release
train's own bump, not a change anyone made. Operator ruling, verbatim: "we
should publish a release on the market when we have something that's ready to
use it not until then there's no fucking need because nobody else is using
this except for us."

This script is the decision surface for the workflow that replaced it,
``.github/workflows/release-cut.yml``: a ``workflow_dispatch``-only trigger, so
a release happens when a human (or the ``omni:release-cut`` skill, on the
human's behalf) explicitly asks for one, never as a side effect of a merge.

WHAT IT DECIDES
---------------
Given ``[project].version`` on the checked-out ``dev`` and the published tag
set:

* ``ready`` when the version is strictly ahead of the highest published
  ``vX.Y.Z`` tag reachable from ``HEAD`` (or no tag has been published yet) —
  the OMN-16344 release-identity gate already enforces this on every
  ``src/**`` PR, so in the steady state dev's version already qualifies.
* NOT ``ready`` otherwise. There is no auto-bump-and-retry here, deliberately:
  the caller is an explicit human action, not a merge the train must not drop,
  so the correct response to a version that is not ahead is to say so and stop
  — the operator bumps ``[project].version`` on dev and re-dispatches.

A malformed or non-final ``[project].version`` is exit 2, never treated as
"not ready": a repo whose version string cannot be read is a repo whose
release-readiness cannot be assessed at all.

Deliberately stdlib-only. It runs before any project install, so it must not
need the project's dependency closure to be importable.

Usage::

    python3 scripts/ci/decide_release_cut.py

Exit codes:
    0 — decision rendered (see the JSON on stdout; ``ready`` may be false)
    2 — configuration error (missing/malformed/non-final [project].version, or
        an unusable git repository)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_PYPROJECT = _REPO_ROOT / "pyproject.toml"

#: Final releases only — an rc/post/dev version is never release-ready, and
#: must not be silently treated as if it were.
_FINAL_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

#: ``vX.Y.Z`` tags. Anything else in the tag namespace is not a package release.
_RELEASE_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


class ReleaseCutError(Exception):
    """Operator-facing misuse: unreadable version, unusable repository."""


@dataclass(frozen=True)
class CutDecision:
    """The whole decision, renderable as JSON for the workflow step summary."""

    ready: bool
    version: str
    latest_tag: str
    reason: str


def parse_final_version(raw: str, *, label: str) -> tuple[int, int, int]:
    """Parse a strict ``X.Y.Z`` version, tolerating a leading ``v``."""
    candidate = raw.strip()
    if candidate.startswith("v"):
        candidate = candidate[1:]
    match = _FINAL_SEMVER.match(candidate)
    if match is None:
        raise ReleaseCutError(
            f"{label} must be a final X.Y.Z version (got {raw!r}); "
            "a pre-release/rc/post version is never release-ready"
        )
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def next_patch(version: str) -> str:
    """Return ``X.Y.(Z+1)`` for a final ``X.Y.Z`` version."""
    major, minor, patch = parse_final_version(version, label="version")
    return f"{major}.{minor}.{patch + 1}"


def highest_published(tags: list[str]) -> str:
    """Return the highest ``vX.Y.Z`` tag, or ``''`` when the repo has none.

    Ordered numerically, not lexically: ``v0.4.9`` sorts BEFORE ``v0.4.18``,
    which a string sort gets backwards.
    """
    parsed: list[tuple[tuple[int, int, int], str]] = []
    for tag in tags:
        match = _RELEASE_TAG.match(tag.strip())
        if match is None:
            continue
        key = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
        parsed.append((key, tag.strip()))
    if not parsed:
        return ""
    return max(parsed)[1]


def decide(*, dev_version: str, tags: list[str]) -> CutDecision:
    """Decide whether dev's current version is ready to cut a release at."""
    dev_tuple = parse_final_version(dev_version, label="[project].version")
    dev_normalized = ".".join(str(part) for part in dev_tuple)

    latest_tag = highest_published(tags)
    if not latest_tag:
        return CutDecision(
            ready=True,
            version=dev_normalized,
            latest_tag="",
            reason=f"no published tag exists yet; {dev_normalized} is release-ready",
        )

    latest_tuple = parse_final_version(latest_tag, label="latest published tag")
    if dev_tuple > latest_tuple:
        return CutDecision(
            ready=True,
            version=dev_normalized,
            latest_tag=latest_tag,
            reason=f"{dev_normalized} is ahead of published {latest_tag}; ready to cut",
        )

    suggested = next_patch(latest_tag)
    return CutDecision(
        ready=False,
        version=dev_normalized,
        latest_tag=latest_tag,
        reason=(
            f"[project].version {dev_normalized} is not ahead of the highest "
            f"published tag {latest_tag}; bump [project].version on dev (e.g. "
            f"to {suggested}) and re-dispatch — this run will not open a bump "
            "PR on your behalf"
        ),
    )


def read_project_version(pyproject: Path) -> str:
    """Read ``[project].version`` from ``pyproject.toml``."""
    try:
        with pyproject.open("rb") as handle:
            data = tomllib.load(handle)
    except OSError as exc:
        raise ReleaseCutError(f"cannot read {pyproject}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ReleaseCutError(f"{pyproject} is not valid TOML: {exc}") from exc
    raw = data.get("project", {}).get("version")
    if raw is None:
        raise ReleaseCutError(f"{pyproject} has no [project].version")
    return str(raw)


def _git(repo_root: Path, *args: str) -> str:
    """Run git in ``repo_root`` and return stdout, raising on failure.

    stderr is captured and re-raised in the message rather than discarded: a
    swallowed stderr reports a clean bill of health for a command that never
    ran (CLAUDE.md rule 16).
    """
    completed = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ReleaseCutError(
            f"git {' '.join(args)} failed (exit {completed.returncode}): "
            f"{completed.stderr.strip() or '(no stderr)'}"
        )
    return completed.stdout


def collect_tags(repo_root: Path) -> list[str]:
    """List ``v*`` tags. An empty list is a legitimate first-release state."""
    raw = _git(repo_root, "tag", "--list", "v*")
    return [line for line in (item.strip() for item in raw.splitlines()) if line]


def resolve_head_sha(repo_root: Path) -> str:
    """Return the full SHA of the checked-out HEAD."""
    return _git(repo_root, "rev-parse", "HEAD").strip()


def _write_github_output(decision: CutDecision, output_path: str | None) -> None:
    if not output_path:
        return
    with open(output_path, "a", encoding="utf-8") as handle:
        handle.write(f"ready={'true' if decision.ready else 'false'}\n")
        handle.write(f"version={decision.version}\n")
        handle.write(f"latest_tag={decision.latest_tag}\n")
        handle.write(f"reason={decision.reason}\n")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Decide whether dev's current [project].version is ready to cut a "
            "release at (OMN-18010 explicit-trigger follow-up)."
        )
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=_REPO_ROOT,
        help="repository to inspect",
    )
    parser.add_argument(
        "--pyproject",
        type=Path,
        default=None,
        help="path to the pyproject.toml carrying [project].version",
    )
    parser.add_argument(
        "--github-output",
        default=os.environ.get("GITHUB_OUTPUT"),
        help="file to append key=value step outputs to",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    pyproject = args.pyproject or (args.repo_root / "pyproject.toml")
    try:
        decision = decide(
            dev_version=read_project_version(pyproject),
            tags=collect_tags(args.repo_root),
        )
    except ReleaseCutError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(asdict(decision), indent=2))
    _write_github_output(decision, args.github_output)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
