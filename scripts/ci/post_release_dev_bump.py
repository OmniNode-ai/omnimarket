#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Post-release dev version bump — the release train's own disarm step (OMN-13912).

WHY THIS EXISTS
---------------
The OMN-13412 release-identity gate (omnibase_core's ``check-release-identity``
hook) says: if a diff touches ``src/**``, ``[project].version`` MUST be strictly
greater than the highest *published* ``vX.Y.Z`` tag. Publishing X.Y.Z from dev HEAD
therefore **arms that gate against dev itself**: the very next packaged-source PR
goes red, and so does every one after it, until somebody unrelated bumps.

**Principle: the event that arms a gate must perform its own disarm in the same
flow.** The release train arms it, so the release train disarms it, not "whoever
trips the gate next" (the OMN-13910 incident: ~2h armed after each of two releases).

WHAT THIS DOES
--------------
Pure decision + a narrow write:

* ``decide(dev_version, released_version)`` returns ``bump`` to
  ``released + 1 patch`` whenever dev is not already strictly ahead of the
  released version, and ``noop`` when it already is.
* ``noop`` makes the step idempotent: a re-run, a second dispatch of the same tag
  or a hand bump all converge on "nothing to do", never a second bump PR.
* ``--apply`` rewrites ONLY ``[project].version`` in ``pyproject.toml``. It is
  table-scoped on purpose: a bare ``^version = `` regex would also rewrite a
  ``version`` key under some ``[tool.*]`` table.

Stdlib-only: it runs in the release job before any project install.

PROVENANCE (OMN-18010)
----------------------
Ported from ``omnibase_infra/scripts/ci/post_release_dev_bump.py``. The
``.github/actions/reopen-dev`` action calls it in ``--released`` mode for both
release paths: release-cut.yml (a manual cut) and auto-tag-on-merge.yml.

THE CUT'S CHANGELOG (OMN-26500)
-------------------------------
The train stages a ``CHANGELOG.md`` entry in the cut (the ticket-contract
prerequisite admits a cut only with it); the removed-prose test forbids it on a
tree. The decision reports ``remove_changelog``: true when ``CHANGELOG.md`` is on
the tree and the released tag's commit added it, and ``--apply`` deletes it. One
the cut did not add stays; a rerun reports false.

Usage::

    # decide only; prints a JSON decision, exit 0
    python3 scripts/ci/post_release_dev_bump.py --released v0.4.19

    # decide and rewrite pyproject.toml when the decision is `bump`
    python3 scripts/ci/post_release_dev_bump.py --released v0.4.19 --apply

Exit codes:
    0 — decision rendered (``bump`` applied when ``--apply``, or ``noop``)
    2 — configuration error (missing/malformed version, non-final released tag)
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_PYPROJECT = _REPO_ROOT / "pyproject.toml"

#: Final releases only. A pre-release/rc tag must never drive a dev bump: rc
#: builds are not what the release-identity gate compares against, and bumping
#: dev off an rc would skip a real patch number.
_FINAL_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

#: TOML table header, e.g. ``[project]`` or ``[tool.uv.sources]``.
_TABLE_HEADER = re.compile(r"^\s*\[([^\]]+)\]\s*$")

#: A ``version = "..."`` assignment, captured so only the value is replaced.
_VERSION_ASSIGN = re.compile(r"^(\s*version\s*=\s*)(['\"])([^'\"]*)(['\"])(.*)$")

ACTION_BUMP = "bump"
ACTION_NOOP = "noop"


class BumpConfigError(Exception):
    """Raised for operator-facing misuse (missing/malformed/non-final version)."""


@dataclass(frozen=True)
class BumpDecision:
    """The whole decision, renderable as JSON for the workflow step summary."""

    action: str
    released_version: str
    dev_version: str
    target_version: str
    reason: str


def parse_final_version(raw: str, *, label: str) -> tuple[int, int, int]:
    """Parse a strict ``X.Y.Z`` version, tolerating a leading ``v``.

    Anything that is not a final three-part release — ``0.38``, ``0.38.11rc1``,
    ``0.38.11.post1``, the empty string — is a configuration error, not a
    silently-skipped input.
    """
    candidate = raw.strip()
    if candidate.startswith("v"):
        candidate = candidate[1:]
    match = _FINAL_SEMVER.match(candidate)
    if match is None:
        raise BumpConfigError(
            f"{label} must be a final X.Y.Z version (got {raw!r}); "
            "pre-release/rc tags never drive a dev bump"
        )
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def next_patch(version: str) -> str:
    """Return ``X.Y.(Z+1)`` for a final ``X.Y.Z`` version."""
    major, minor, patch = parse_final_version(version, label="version")
    return f"{major}.{minor}.{patch + 1}"


def decide(dev_version: str, released_version: str) -> BumpDecision:
    """Decide whether dev needs the post-release disarm bump.

    ``bump`` whenever dev is NOT already strictly ahead of the released version
    — that covers both the armed case (dev == released, the v0.38.10/v0.38.11
    incidents) and the pathological behind case (dev < released, e.g. a release
    cut from a branch other than dev). ``noop`` only when dev is already ahead,
    which is what the release-identity gate itself demands.
    """
    dev_tuple = parse_final_version(dev_version, label="dev version")
    released_tuple = parse_final_version(released_version, label="released version")
    target = next_patch(released_version)

    if dev_tuple > released_tuple:
        return BumpDecision(
            action=ACTION_NOOP,
            released_version=".".join(str(p) for p in released_tuple),
            dev_version=".".join(str(p) for p in dev_tuple),
            target_version=".".join(str(p) for p in dev_tuple),
            reason=(
                f"dev {dev_version} is already ahead of published "
                f"{released_version}; release-identity is disarmed, nothing to do"
            ),
        )

    return BumpDecision(
        action=ACTION_BUMP,
        released_version=".".join(str(p) for p in released_tuple),
        dev_version=".".join(str(p) for p in dev_tuple),
        target_version=target,
        reason=(
            f"dev {dev_version} is not ahead of published {released_version}; "
            f"release-identity is ARMED against dev — bump to {target}"
        ),
    )


def read_project_version(pyproject: Path) -> str:
    """Read ``[project].version`` from ``pyproject.toml``."""
    with pyproject.open("rb") as handle:
        data = tomllib.load(handle)
    raw = data.get("project", {}).get("version")
    if raw is None:
        raise BumpConfigError(f"{pyproject} has no [project].version")
    return str(raw)


def rewrite_project_version(text: str, new_version: str) -> str:
    """Return ``text`` with ONLY ``[project].version`` set to ``new_version``.

    Table-scoped by construction: a ``version`` key under any other table (a
    ``[tool.*]`` block, a dependency-group table) is left byte-identical. A
    whole-file regex would not have that property, and silently corrupting an
    unrelated pin is a worse failure than not bumping at all.
    """
    lines = text.splitlines(keepends=True)
    current_table: str | None = None
    replaced = False

    for index, line in enumerate(lines):
        header = _TABLE_HEADER.match(line)
        if header is not None:
            current_table = header.group(1).strip()
            continue
        if current_table != "project":
            continue
        assign = _VERSION_ASSIGN.match(line)
        if assign is None:
            continue
        prefix, open_q, _old, close_q, suffix = assign.groups()
        newline = "\n" if line.endswith("\n") else ""
        lines[index] = (
            f"{prefix}{open_q}{new_version}{close_q}{suffix.rstrip()}{newline}"
        )
        replaced = True
        break

    if not replaced:
        raise BumpConfigError(
            "no [project].version assignment found to rewrite — refusing to "
            "guess where the version lives"
        )
    return "".join(lines)


def apply_decision(pyproject: Path, decision: BumpDecision) -> bool:
    """Write the bump when the decision says ``bump``. Returns True if written."""
    if decision.action != ACTION_BUMP:
        return False
    original = pyproject.read_text(encoding="utf-8")
    pyproject.write_text(
        rewrite_project_version(original, decision.target_version), encoding="utf-8"
    )
    return True


CHANGELOG_PATH = "CHANGELOG.md"
_GIT_TIMEOUT_SECONDS = 60


def release_cut_added_changelog(root: Path, tag: str) -> bool:
    """Whether ``tag``'s commit added a root ``CHANGELOG.md`` over its first parent.

    A tag, a parent or a git repository that cannot be read answers ``False``: an
    unreadable cut never removes a file.
    """
    listing = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "diff",
            "--name-only",
            "--diff-filter=A",
            "--no-renames",
            f"{tag}^1",
            f"{tag}^{{commit}}",
            "--",
            CHANGELOG_PATH,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=_GIT_TIMEOUT_SECONDS,
    )
    return listing.returncode == 0 and listing.stdout.strip() == CHANGELOG_PATH


def remove_cut_changelog_file(root: Path, tag: str, *, apply: bool) -> bool:
    """Whether ``CHANGELOG.md`` is on the tree and ``tag``'s cut added it.

    ``apply`` deletes the file when the answer is ``True``; staging that deletion
    is the caller's.
    """
    changelog = root / CHANGELOG_PATH
    if not changelog.is_file() or not release_cut_added_changelog(root, tag):
        return False
    if apply:
        changelog.unlink()
    return True


def remove_cut_changelog(root: Path, decision: BumpDecision, apply: bool) -> bool:
    """Whether the cut's ``CHANGELOG.md`` goes (deleted when ``apply``)."""
    return remove_cut_changelog_file(root, f"v{decision.released_version}", apply=apply)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Decide (and optionally apply) the post-release dev version bump "
            "that disarms the OMN-13412 release-identity gate."
        )
    )
    parser.add_argument(
        "--released",
        required=True,
        help="the version just published, e.g. v0.4.19 or 0.4.19",
    )
    parser.add_argument(
        "--pyproject",
        type=Path,
        default=_DEFAULT_PYPROJECT,
        help="path to the pyproject.toml carrying [project].version",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="rewrite [project].version when the decision is `bump`",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        dev_version = read_project_version(args.pyproject)
        decision = decide(dev_version, args.released)
        applied = apply_decision(args.pyproject, decision) if args.apply else False
        removal = remove_cut_changelog(args.pyproject.parent, decision, args.apply)
    except BumpConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    payload = asdict(decision)
    payload["applied"] = applied
    payload["remove_changelog"] = removal
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
