#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Post-release dev version bump — the release train's own disarm step (OMN-13912).

WHY THIS EXISTS
---------------
The OMN-13412 release-identity gate (``scripts/check_release_identity.py``) says:
if a diff touches packaged source (``src/**``), ``[project].version`` MUST be
strictly greater than the highest *published* ``vX.Y.Z`` tag. Publishing X.Y.Z
from dev HEAD therefore **arms that gate against dev itself** — dev's version is
now exactly equal to the published version, so the very next packaged-source PR
goes red, and stays red for every PR after it, until somebody unrelated notices
and bumps.

That is not hypothetical. Measured twice in the current release series:

* v0.38.10 tagged at dev HEAD 2026-08-26T01:38:23Z; dev stayed at 0.38.10 until
  ``a07fefde4`` (OMN-16536, an unrelated feature PR) bumped it to 0.38.11 at
  2026-08-26T03:44:31Z — a ~2h06m armed window.
* v0.38.11 tagged at dev HEAD 2026-08-28T00:49:31Z; dev stayed at 0.38.11 until
  ``93c42ada4`` (OMN-16769, again unrelated) bumped it to 0.38.12 at
  2026-08-28T02:27:16Z — a ~1h38m armed window.

**Principle: the event that arms a gate must perform its own disarm in the same
flow.** The release train arms it, so the release train disarms it. Leaving the
bump to "whoever trips the gate next" is how a release turns into a dev-wide
wedge, which is exactly the OMN-13910 incident this ticket was opened for.

WHAT THIS DOES
--------------
Pure decision + a narrow write:

* ``decide(dev_version, released_version)`` returns ``bump`` to
  ``released + 1 patch`` whenever dev is not already strictly ahead of the
  released version, and ``noop`` when it already is.
* ``noop`` is what makes the whole step idempotent: a re-run of the release, a
  second dispatch of the same tag, or a human who already bumped dev by hand all
  converge on "nothing to do" instead of opening a second bump PR.
* ``--apply`` rewrites ONLY ``[project].version`` in ``pyproject.toml``. It is
  table-scoped on purpose: a bare ``^version = `` regex would also rewrite a
  ``version`` key under some ``[tool.*]`` table.

Deliberately stdlib-only: it runs inside the release job before any project
install, so it must not need the project's dependency closure to be importable.

PROVENANCE (OMN-18010)
----------------------
Ported into omnimarket from ``omnibase_infra/scripts/ci/post_release_dev_bump.py``
— the only repo in the registry that had it — because release-on-merge makes the
armed-gate window recur on EVERY merge rather than once per hand-cut release.
The port is byte-faithful apart from ``_REPO_ROOT`` and the ``--set`` mode added
below; the omnibase_infra incident history above is quoted as the measured
evidence for the behaviour, not as a claim about omnimarket's own history.

THE ``--set`` MODE (OMN-18010)
------------------------------
``--released`` answers "the train just published X, where should dev be?".
``--set`` answers "put dev at exactly this version", which is what the
release-on-merge ``arm-dev`` job needs: when dev has drifted level with the
highest published tag, the merge cannot be released until dev names the version
it would be released AS. Same ``bump``/``noop`` idempotency, same table-scoped
rewrite, same refusal of non-final versions.

Usage::

    # decide only; prints a JSON decision, exit 0
    python3 scripts/ci/post_release_dev_bump.py --released v0.4.19

    # decide and rewrite pyproject.toml when the decision is `bump`
    python3 scripts/ci/post_release_dev_bump.py --released v0.4.19 --apply

    # set dev to an exact version (release-on-merge arm-dev path)
    python3 scripts/ci/post_release_dev_bump.py --set 0.4.19 --apply

Exit codes:
    0 — decision rendered (``bump`` applied when ``--apply``, or ``noop``)
    2 — configuration error (missing/malformed version, non-final released tag)
"""

from __future__ import annotations

import argparse
import json
import re
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


def decide_target(dev_version: str, target_version: str) -> BumpDecision:
    """Decide whether dev must move to an EXACT target version (OMN-18010).

    Used by the release-on-merge ``arm-dev`` job. ``noop`` when dev already
    states that version, so a re-run of the same drifted merge converges instead
    of opening a second bump PR. Unlike :func:`decide` this does not compute the
    target — the caller already resolved it from the tag list — so a dev version
    ABOVE the target is still a ``noop``: moving dev backwards would re-arm the
    release-identity gate this whole flow exists to keep disarmed.
    """
    dev_tuple = parse_final_version(dev_version, label="dev version")
    target_tuple = parse_final_version(target_version, label="target version")
    dev_normalized = ".".join(str(part) for part in dev_tuple)
    target_normalized = ".".join(str(part) for part in target_tuple)

    if dev_tuple >= target_tuple:
        return BumpDecision(
            action=ACTION_NOOP,
            released_version="",
            dev_version=dev_normalized,
            target_version=dev_normalized,
            reason=(
                f"dev {dev_normalized} already states {target_normalized} or later; "
                "nothing to set"
            ),
        )

    return BumpDecision(
        action=ACTION_BUMP,
        released_version="",
        dev_version=dev_normalized,
        target_version=target_normalized,
        reason=(
            f"dev {dev_normalized} is behind the requested target "
            f"{target_normalized}; set [project].version to {target_normalized}"
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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Decide (and optionally apply) the post-release dev version bump "
            "that disarms the OMN-13412 release-identity gate."
        )
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--released",
        help="the version just published, e.g. v0.4.19 or 0.4.19",
    )
    mode.add_argument(
        "--set",
        dest="set_version",
        help="set [project].version to exactly this final X.Y.Z version",
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
        if args.set_version is not None:
            decision = decide_target(dev_version, args.set_version)
        else:
            decision = decide(dev_version, args.released)
        applied = apply_decision(args.pyproject, decision) if args.apply else False
    except BumpConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    payload = asdict(decision)
    payload["applied"] = applied
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
