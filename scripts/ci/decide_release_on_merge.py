#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Release-on-merge decision step (OMN-18010).

WHY THIS EXISTS
---------------
A release in this org is a manual, ticket-driven step: somebody has to notice
that dev moved, decide to cut, and push a tag by hand. Measured consequence —
``omnimarket#2304`` merged 2026-09-05 and sat unreleased with its release ticket
in Backlog; ``#2334`` did the same; a PRD scoring pass found five landed-but-not-
deployed items at once. "Done" was being measured at merge, and nothing audited
the distance between dev and the last tag.

``.github/workflows/release-on-merge.yml`` closes that by releasing on the push
to ``dev`` that a squash merge produces. This module is that workflow's whole
decision surface, deliberately separated from the YAML so the decision is unit
testable rather than only observable by merging something.

WHY A PUSH TO ``dev`` IS A SAFE RELEASE TRIGGER
-----------------------------------------------
Every repo in this registry is squash-only with no merge queue, so a squash
merge to ``dev`` produces exactly one push whose head is a merge commit GitHub
only created after every required context reported success. The green-ness is
established by construction; this script does not re-derive it.

WHAT IT DECIDES
---------------
Given the push range, the tag list and ``[project].version``:

* ``skip`` when the head commit is the release train's own bump push (the
  ``[release-on-merge]`` subject marker) or when no packaged path changed. Each
  skip carries a machine-readable ``skip_reason`` — a silent skip is
  indistinguishable from a broken trigger.
* ``needs_bump`` when ``[project].version`` is not strictly ahead of the highest
  published tag. In the steady state this never fires: the OMN-16344
  release-identity gate (``scripts/check_release_identity.py``) already refuses
  any ``src/**`` PR whose version is not ahead, so dev's version IS the next
  release version and the run simply tags it. ``needs_bump`` is the recovery
  path for a drifted repo — a merge that touched only ``uv.lock`` while dev sat
  level with the published tag, say.
* otherwise ``version`` — the version to tag at ``github.sha``.

A malformed or non-final ``[project].version`` is exit 2, never a skip: a repo
whose version string cannot be read is a repo whose releases cannot be trusted,
and reporting that as "nothing to do" is how the original problem was invisible
for a week.

Deliberately stdlib-only. It runs before any project install, so it must not
need the project's dependency closure to be importable.

Usage::

    python3 scripts/ci/decide_release_on_merge.py \
        --before "$BEFORE_SHA" --after "$AFTER_SHA"

Exit codes:
    0 — decision rendered (see the JSON on stdout)
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
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_PYPROJECT = _REPO_ROOT / "pyproject.toml"

#: Final releases only — an rc/post/dev version must never drive an automatic
#: release, and must not be silently ignored either.
_FINAL_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

#: ``vX.Y.Z`` tags. Anything else in the tag namespace is not a package release.
_RELEASE_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")

#: The release train stamps this into the subject of every commit it pushes to
#: dev itself, so its own push cannot re-enter the trigger. Without it, the
#: post-release "open dev for the next version" commit touches pyproject.toml,
#: re-triggers the workflow, and releases an empty version — once per merge,
#: forever.
SELF_PUSH_MARKER = "[release-on-merge]"

#: A change under these roots can alter the published artifact, so it is what
#: makes a merge releasable. Kept in sync with the workflow's ``paths:`` filter;
#: the filter is the cheap pre-check and this is the authoritative re-derivation
#: (a single push can carry several commits, so the filter alone is not enough).
PACKAGED_PREFIXES: tuple[str, ...] = ("src/",)
PACKAGED_FILES: tuple[str, ...] = ("pyproject.toml", "uv.lock")

SKIP_SELF_PUSH = "self_push"
SKIP_NO_PACKAGED_CHANGE = "no_packaged_change"


class ReleaseDecisionError(Exception):
    """Operator-facing misuse: unreadable version, unusable repository."""


@dataclass(frozen=True)
class ReleaseDecision:
    """The whole decision, renderable as JSON for the workflow step summary."""

    skip: bool
    skip_reason: str
    version: str
    needs_bump: bool
    dev_version: str
    latest_tag: str
    reason: str


def parse_final_version(raw: str, *, label: str) -> tuple[int, int, int]:
    """Parse a strict ``X.Y.Z`` version, tolerating a leading ``v``."""
    candidate = raw.strip()
    if candidate.startswith("v"):
        candidate = candidate[1:]
    match = _FINAL_SEMVER.match(candidate)
    if match is None:
        raise ReleaseDecisionError(
            f"{label} must be a final X.Y.Z version (got {raw!r}); "
            "a pre-release/rc/post version never drives an automatic release"
        )
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def next_patch(version: str) -> str:
    """Return ``X.Y.(Z+1)`` for a final ``X.Y.Z`` version."""
    major, minor, patch = parse_final_version(version, label="version")
    return f"{major}.{minor}.{patch + 1}"


def highest_published(tags: Iterable[str]) -> str:
    """Return the highest ``vX.Y.Z`` tag, or ``''`` when the repo has none.

    Ordered numerically, not lexically: ``v0.4.9`` sorts BEFORE ``v0.4.18``,
    which a string sort gets backwards and which would make the gate compare
    against the wrong release.
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


def is_packaged_path(path: str) -> bool:
    """True when a change to ``path`` can alter the published artifact."""
    normalized = path.strip()
    if not normalized:
        return False
    if normalized in PACKAGED_FILES:
        return True
    return any(normalized.startswith(prefix) for prefix in PACKAGED_PREFIXES)


def decide(
    *,
    dev_version: str,
    tags: Sequence[str],
    changed_paths: Sequence[str],
    head_subject: str,
) -> ReleaseDecision:
    """Decide whether this push to dev releases, and at what version.

    The version is resolved BEFORE the skip checks so that a malformed
    ``[project].version`` raises rather than being masked by a skip — a repo
    that cannot state its own version is a repo whose next release is already
    broken, and the whole point of this ticket is that such states stopped being
    reported at all.
    """
    dev_tuple = parse_final_version(dev_version, label="[project].version")
    dev_normalized = ".".join(str(part) for part in dev_tuple)

    latest_tag = highest_published(tags)
    if latest_tag:
        latest_tuple = parse_final_version(latest_tag, label="latest published tag")
        needs_bump = dev_tuple <= latest_tuple
        target = dev_normalized if not needs_bump else next_patch(latest_tag)
    else:
        # No published tag at all: dev's version is trivially ahead of nothing.
        needs_bump = False
        target = dev_normalized

    def _skip(reason: str, detail: str) -> ReleaseDecision:
        return ReleaseDecision(
            skip=True,
            skip_reason=reason,
            version="",
            needs_bump=False,
            dev_version=dev_normalized,
            latest_tag=latest_tag,
            reason=detail,
        )

    if SELF_PUSH_MARKER in head_subject:
        return _skip(
            SKIP_SELF_PUSH,
            f"head commit subject carries {SELF_PUSH_MARKER}; this is the release "
            "train's own push to dev, not a source merge",
        )

    if not any(is_packaged_path(path) for path in changed_paths):
        return _skip(
            SKIP_NO_PACKAGED_CHANGE,
            "no packaged path changed in this push range "
            f"({len(changed_paths)} path(s) examined); nothing to release",
        )

    # NO explicit "does v<target> already exist?" check, deliberately. It would
    # be dead code: the target is EITHER dev's version when dev is strictly
    # above the highest published tag (so v<target> cannot be a tag, or it would
    # be the highest) OR next_patch(highest) (so v<target> cannot be a tag
    # either, for the same reason). The no-double-release property comes from
    # that invariant plus the self-push guard above, and
    # ``test_a_rerun_after_the_release_landed_asks_for_a_bump_not_a_second_release``
    # pins it. An unreachable branch here would read as protection while
    # protecting nothing, which is the failure class this ticket is about.
    if needs_bump:
        return ReleaseDecision(
            skip=False,
            skip_reason="",
            version=target,
            needs_bump=True,
            dev_version=dev_normalized,
            latest_tag=latest_tag,
            reason=(
                f"dev {dev_normalized} is not ahead of published {latest_tag}; the "
                f"release-identity gate is ARMED against dev — dev must be bumped "
                f"to {target} before this merge can be released"
            ),
        )

    return ReleaseDecision(
        skip=False,
        skip_reason="",
        version=target,
        needs_bump=False,
        dev_version=dev_normalized,
        latest_tag=latest_tag,
        reason=(
            f"dev {dev_normalized} is ahead of published {latest_tag or '(none)'}; "
            f"release v{target} at the merge commit"
        ),
    )


def read_project_version(pyproject: Path) -> str:
    """Read ``[project].version`` from ``pyproject.toml``."""
    try:
        with pyproject.open("rb") as handle:
            data = tomllib.load(handle)
    except OSError as exc:
        raise ReleaseDecisionError(f"cannot read {pyproject}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ReleaseDecisionError(f"{pyproject} is not valid TOML: {exc}") from exc
    raw = data.get("project", {}).get("version")
    if raw is None:
        raise ReleaseDecisionError(f"{pyproject} has no [project].version")
    return str(raw)


def _git(repo_root: Path, *args: str) -> str:
    """Run git in ``repo_root`` and return stdout, raising on failure.

    stderr is deliberately captured and re-raised in the message rather than
    discarded: a sweep that swallows stderr reports a clean bill of health for a
    command that never ran.
    """
    completed = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ReleaseDecisionError(
            f"git {' '.join(args)} failed (exit {completed.returncode}): "
            f"{completed.stderr.strip() or '(no stderr)'}"
        )
    return completed.stdout


def _is_resolvable(repo_root: Path, ref: str) -> bool:
    if not ref or set(ref) == {"0"}:
        return False
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "rev-parse",
            "--verify",
            "--quiet",
            f"{ref}^{{commit}}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode == 0


def collect_changed_paths(repo_root: Path, before: str, after: str) -> list[str]:
    """Re-derive the changed set for the push range.

    ``github.event.before`` is the all-zero SHA for a newly created ref and can
    also point at a commit this checkout does not have. Both fall back to the
    head commit's own diff rather than to "assume everything changed", because
    the safe direction here is to release less, not more.
    """
    if _is_resolvable(repo_root, before):
        raw = _git(repo_root, "diff", "--name-only", f"{before}..{after}")
    else:
        raw = _git(repo_root, "show", "--pretty=format:", "--name-only", after)
    return [line for line in (item.strip() for item in raw.splitlines()) if line]


def collect_tags(repo_root: Path) -> list[str]:
    """List ``v*`` tags. An empty list is a legitimate first-release state."""
    raw = _git(repo_root, "tag", "--list", "v*")
    return [line for line in (item.strip() for item in raw.splitlines()) if line]


def collect_head_subject(repo_root: Path, after: str) -> str:
    """Return the subject line of the pushed head commit."""
    return _git(repo_root, "log", "-1", "--format=%s", after).strip()


def _write_github_output(decision: ReleaseDecision, output_path: str | None) -> None:
    if not output_path:
        return
    with open(output_path, "a", encoding="utf-8") as handle:
        handle.write(f"skip={'true' if decision.skip else 'false'}\n")
        handle.write(f"skip_reason={decision.skip_reason}\n")
        handle.write(f"version={decision.version}\n")
        handle.write(f"needs_bump={'true' if decision.needs_bump else 'false'}\n")
        handle.write(f"latest_tag={decision.latest_tag}\n")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Decide whether a push to dev should cut a release, and at what "
            "version (OMN-18010)."
        )
    )
    parser.add_argument(
        "--before",
        default="",
        help="github.event.before — the ref's previous head (may be the zero SHA)",
    )
    parser.add_argument(
        "--after",
        required=True,
        help="github.sha — the pushed head commit",
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
            changed_paths=collect_changed_paths(
                args.repo_root, args.before, args.after
            ),
            head_subject=collect_head_subject(args.repo_root, args.after),
        )
    except ReleaseDecisionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(asdict(decision), indent=2))
    _write_github_output(decision, args.github_output)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
