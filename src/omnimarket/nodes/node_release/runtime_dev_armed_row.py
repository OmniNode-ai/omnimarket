# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dev-armed row: dev's version has equalled the highest published tag too long (OMN-26025).

Publishing X.Y.Z from dev leaves dev's ``[project].version`` equal to the highest
published tag, which arms the release-identity gate against every pull request
that touches ``src/**``. The reopen-dev job disarms it with a bump pull request.
Measured on 2026-11-19: armed windows up to 10h26m, with nothing raising a row
while they lasted.

``dev_armed_row`` is pure: from dev's version, the highest tag, the tag's time and
``now`` it returns one ledger row naming an owner when the tree has been armed for
MORE than ``ARMED_AFTER`` (30 minutes), and ``None`` at or under it, or when dev
is ahead of the tag. ``read_armed_state`` reads the three facts from a clone's
``origin/dev`` and its tags. Neither writes anything: the caller appends the row.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

ARMED_AFTER = timedelta(minutes=30)
TICKET = "OMN-26025"
_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


@dataclass(frozen=True)
class ArmedState:
    dev_version: str
    highest_tag: str
    tag_time: datetime


def _tuple(version: str) -> tuple[int, int, int]:
    match = _VERSION.match(version.removeprefix("v"))
    if match is None:
        raise ValueError(f"not a final X.Y.Z version: {version!r}")
    return int(match[1]), int(match[2]), int(match[3])


def dev_armed_row(state: ArmedState, *, now: datetime, owner: str) -> str | None:
    """One STATUS row when dev has equalled the highest tag for more than 30 minutes."""
    if not owner.strip():
        raise ValueError("a dev-armed row names an owner")
    if _tuple(state.dev_version) != _tuple(state.highest_tag):
        return None
    armed_for = now - state.tag_time
    if armed_for <= ARMED_AFTER:
        return None
    minutes = int(armed_for.total_seconds() // 60)
    return " | ".join(
        [
            now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "STATUS",
            "lane=omnimarket-release-cut-timer",
            f"ticket={TICKET}",
            "outcome=dev-armed",
            "repo=omnimarket",
            f"owner={owner}",
            f"dev_version={state.dev_version}",
            f"latest_tag={state.highest_tag}",
            f"armed_minutes={minutes}",
            f"dev's version equals {state.highest_tag} for {minutes} minutes: the release-identity "
            "gate is armed against dev until the post-release bump merges",
        ]
    )


def _git(clone: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(clone), *args], capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def read_armed_state(clone: Path, *, ref: str = "origin/dev") -> ArmedState:
    """Dev's version and the highest published tag with its time, from a clone's refs."""
    dev_version = str(
        tomllib.loads(_git(clone, "show", f"{ref}:pyproject.toml"))["project"][
            "version"
        ]
    )
    tags = [t for t in _git(clone, "tag", "--list", "v*").split() if _TAG.match(t)]
    if not tags:
        raise ValueError("no published vX.Y.Z tag")
    highest = max(tags, key=_tuple)
    stamp = _git(
        clone,
        "for-each-ref",
        "--format=%(creatordate:iso-strict)",
        f"refs/tags/{highest}",
    )
    return ArmedState(dev_version, highest, datetime.fromisoformat(stamp))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--clone", type=Path, required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--now", help="UTC timestamp used instead of the current time")
    args = parser.parse_args(argv)
    now = (
        datetime.fromisoformat(args.now.replace("Z", "+00:00"))
        if args.now
        else datetime.now(UTC)
    )
    row = dev_armed_row(read_armed_state(args.clone), now=now, owner=args.owner)
    if row is not None:
        sys.stdout.write(row + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
