#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Release-identity gate: no importable-surface change merges onto a published
version without a bump (OMN-16344).

omnimarket is a published PyPI package. If packaged source under ``src/``
changes but ``pyproject.toml``'s ``project.version`` is NOT bumped past the most
recently *published* version (the latest ``vX.Y.Z`` git tag), then two distinct
code states ship under the SAME version string.

Why this repo needed the gate: omnimarket's ``dev`` sat at 0.4.8 — byte-identical
in version string to the published v0.4.8 tag — while carrying seven commits of
real ``src/`` changes. A release could not be cut without a manual hand-bump
first, and until that bump every consumer resolving "omnimarket 0.4.8" could
have received either code state. The sibling repos close this gap structurally
rather than by convention: omnibase_infra with its ``check_release_identity.py``
(OMN-13412) and omnibase_core with the port (OMN-13411). This is the omnimarket
port, and it is what keeps ``dev`` pre-bumped ahead of the last tag the way core
and infra already are — the divergence this ticket exists to end.

What it enforces
----------------
When the diff under inspection touches packaged source (``src/**``) AND any
published tag exists, ``pyproject.toml``'s ``project.version`` MUST be strictly
greater than the highest published version (latest ``v*`` / bare-semver tag).

"Published" means published ON A LINEAGE THIS TREE DESCENDS FROM -- the tags
reachable from the evaluated commit, not every tag that happens to exist when
the job runs (OMN-18443, see ``_published_tags``). Both halves of the
comparison then come from the same commit, so a release cut by a peer PR while
this one sat in CI cannot retroactively refuse a tree that was correctly
versioned when it was computed.

A docs-only / tests-only / CI-only diff (no ``src/**`` change) is exempt: the
published wheel is unaffected, so no bump is required.

Modes
-----
* ``--base <ref>``    Compare the working tree against ``<ref>`` (e.g. ``origin/dev``)
                      to decide whether packaged source changed. CI passes the PR
                      base. If omitted, the gate assumes source MAY have changed
                      and always enforces the version-ahead invariant.
* ``--changed-file``  Explicit changed-file list (repeatable / newline list on
                      stdin via ``-``). Overrides ``--base`` diffing.
* ``--staged``        Inspect the staged index with Git's NUL-delimited path
                      output. The local pre-commit hook uses this mode.

Usage::

    uv run python scripts/check_release_identity.py --base origin/dev
    uv run python scripts/check_release_identity.py   # strict: always require ahead

Exit codes:
    0 — version is correctly ahead of the latest published tag (or exempt diff)
    1 — packaged source changed without bumping past the latest published version
    2 — configuration error (no pyproject version, malformed version/tag)
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tomllib
from pathlib import Path

from packaging.version import InvalidVersion, Version

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
# Packaged-source prefixes whose change requires a version bump.
_PACKAGED_PREFIXES = ("src/",)


def _read_pyproject_version() -> Version:
    with _PYPROJECT.open("rb") as fh:
        data = tomllib.load(fh)
    raw = data.get("project", {}).get("version")
    if not raw:
        raise ValueError(f"no project.version in {_PYPROJECT}")
    try:
        return Version(str(raw))
    except InvalidVersion as exc:
        raise ValueError(f"malformed project.version {raw!r}: {exc}") from exc


def _git(args: list[str]) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise ValueError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def _staged_files() -> list[str]:
    """Return staged paths without losing whitespace or newline characters."""
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "-z"],
        cwd=_REPO_ROOT,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        stderr = os.fsdecode(result.stderr).strip()
        raise ValueError(stderr or "git diff --cached --name-only -z failed")
    return [path for path in os.fsdecode(result.stdout).split("\0") if path]


def _repo_is_shallow() -> bool:
    """Return True when this checkout may be missing commit ancestry."""
    return _git(["rev-parse", "--is-shallow-repository"]).strip().lower() == "true"


def _published_tags(anchor: str = "HEAD") -> list[str]:
    """Return the published tags THIS TREE DESCENDS FROM (OMN-18443).

    The gate compares a VERSION READ FROM A TREE against a SET OF PUBLISHED
    RELEASES, and those two facts must come from the same clock. They did not.

    On a ``pull_request`` event GitHub hands the runner ``refs/pull/N/merge`` --
    the merge commit it computed when the PR was last synchronized -- so the
    ``pyproject.toml`` this gate reads is pinned at TRIGGER time. The same
    checkout step then fetches ``+refs/tags/*:refs/tags/*`` at RUN time. Reading
    the published set with ``git tag --list`` therefore compared a trigger-time
    tree against a run-time tag list, and refused correctly-versioned trees
    whenever a peer PR released in between.

    Measured on omnimarket#2601 (run 35107791574, job 104902570663,
    2026-09-16T17:27Z): the checked-out tree was
    ``Merge edc2efc8 into aa51cad2``, declaring 0.4.107, and the highest release
    reachable from it is v0.4.106 -- correctly versioned. ``git tag --list`` in
    that same job returned v0.4.107 and v0.4.108, both cut from merges the tree
    does not contain, and the gate failed it. That cost omnimarket#2591 two full
    CI cycles in one review and omnimarket#2601 three of six.

    Anchoring on ``git tag --merged`` restores the one-clock comparison, and it
    does not weaken the invariant. A release cut on a lineage this tree does not
    contain cannot be aliased BY this tree: the branch never authored that
    version, so git's three-way merge takes ``dev``'s newer value on the way in,
    and the release is cut from whatever ``dev`` then holds. A release the tree
    DOES descend from is still compared, so the aliasing this gate exists to
    refuse is still refused (see the positive controls in
    ``tests/scripts/test_check_release_identity.py``).

    Fail-CLOSED on unknowable ancestry: ``git tag --merged`` needs the tagged
    commits' ancestry to be present, and a shallow clone can omit it and return
    FEWER tags -- the permissive direction, and the same shape as the OMN-17240
    empty-tag-set defect. When ancestry cannot be trusted, or the anchor cannot
    be resolved at all, this falls back to the full tag list, which is the
    strictly stricter answer.

    Args:
        anchor: The commit whose reachable tags count as published.

    Returns:
        Raw tag lines, exactly as ``git tag`` emits them.
    """
    if _repo_is_shallow():
        return _git(["tag", "--list"]).splitlines()
    try:
        return _git(["tag", "--merged", anchor]).splitlines()
    except ValueError:
        # An unresolvable anchor (e.g. a tree with no commits) must not be a
        # pass. Fall back to the superset the legacy gate used.
        return _git(["tag", "--list"]).splitlines()


def _latest_published_version() -> Version | None:
    """Return the highest published semver tag, or None if there are no tags."""
    tags = _published_tags()
    if not tags:
        return None
    best: Version | None = None
    for line in tags:
        tag = line.strip()
        candidate = tag[1:] if tag.startswith("v") else tag
        try:
            ver = Version(candidate)
        except InvalidVersion:
            continue
        if best is None or ver > best:
            best = ver
    return best


def _packaged_source_changed(base: str | None, explicit: list[str]) -> bool:
    """Decide whether packaged source changed relative to base / explicit list."""
    if explicit:
        files = explicit
    elif base:
        diff = _git(["diff", "--name-only", f"{base}...HEAD"])
        files = [f for f in diff.splitlines() if f.strip()]
        if not files:
            # No commits of our own: look for uncommitted edits, still anchored on
            # the MERGE BASE (OMN-18058). Never the two-dot ``git diff <base>``:
            # that form describes the difference between two trees, so on a stale
            # base it reports every packaged-source file a PEER landed on the base
            # branch as this branch's change, arming this version gate against a
            # branch that touched no packaged source at all.
            merge_base = _git(["merge-base", base, "HEAD"])
            if merge_base:
                diff = _git(["diff", "--name-only", merge_base])
                files = [f for f in diff.splitlines() if f.strip()]
    else:
        # No base and no explicit list: cannot prove the diff is exempt — enforce.
        return True
    return any(f.startswith(prefix) for f in files for prefix in _PACKAGED_PREFIXES)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    selector = parser.add_mutually_exclusive_group()
    selector.add_argument(
        "--base",
        default=None,
        help="Git ref to diff against (e.g. origin/dev) to detect src/ changes.",
    )
    selector.add_argument(
        "--changed-file",
        dest="changed_files",
        action="append",
        default=[],
        help="Explicit changed file (repeatable). Overrides --base diffing.",
    )
    selector.add_argument(
        "--staged",
        action="store_true",
        help="Inspect staged paths from Git's NUL-delimited index diff.",
    )
    args = parser.parse_args(argv)

    explicit = list(args.changed_files)
    if explicit == ["-"]:
        explicit = [ln.strip() for ln in sys.stdin.read().splitlines() if ln.strip()]

    try:
        pyproject_version = _read_pyproject_version()
        if args.staged:
            explicit = _staged_files()
        latest = _latest_published_version()
        if latest is None:
            print("OK: no published tag yet — release-identity bump not required.")
            return 0
        changed = _packaged_source_changed(args.base, explicit)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if not changed:
        print(
            "OK: no packaged src/** change in this diff — version bump not required "
            f"(pyproject {pyproject_version}, latest published {latest})."
        )
        return 0

    if pyproject_version > latest:
        print(f"OK: version {pyproject_version} is ahead of latest published {latest}.")
        return 0

    print(
        "FAIL: packaged source changed but pyproject version "
        f"{pyproject_version} is NOT ahead of the latest published version "
        f"{latest} (OMN-16344 release-identity gate).",
        file=sys.stderr,
    )
    print(
        "Merging code consumers import onto an already-published version aliases "
        "two code states under one wheel version. Bump project.version in "
        f"pyproject.toml past {latest} (e.g. "
        f"{Version(f'{latest.major}.{latest.minor}.{latest.micro + 1}')}).",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
