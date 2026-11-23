# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Release relock: a release commit locks released sibling wheels (OMN-26166).

RULING 2026-11-20T11:37:47Z: stability is based on released packages. Dev keeps
the dev-tag locks of RULING 2026-10-16 (internal siblings are consumed from the
dev merge tag); a *release commit* locks ``omnibase-infra``, ``omnibase-core``
and ``omnibase-compat`` from the package index instead.

The ``release`` job of ``.github/workflows/release-cut.yml`` is the one step
that builds a release commit (the tag names it, ``sync-main`` fast-forwards main
to it). It runs, in order::

    strip_pyproject(root)             # drop the three [tool.uv.sources] pins
    uv lock                           # resolve them from the index
    git checkout -- pyproject.toml    # restore the dev-tag pins byte for byte
    verify(root)                      # the lock reads registry for all three

and commits ``uv.lock`` plus the marker file (``write_marker``), so ``pyproject.toml`` (the only
file the Internal Sibling Pins check reads) is identical to dev and the check's
finding codes for dev stand. The reopen-dev job checks out ``dev`` and never
calls this module, so dev and the reopen-dev commit keep their dev-tag locks.

``verify`` is also the test of the pair rule: with the marker present the three
siblings must read ``registry``; without it none of them may.
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

SIBLINGS: frozenset[str] = frozenset(
    {"omnibase-infra", "omnibase-core", "omnibase-compat"}
)
MARKER_NAME = ".release-commit.json"

CODE_GIT_ON_RELEASE = "git_source_on_release"
CODE_REGISTRY_OFF_RELEASE = "registry_source_off_release"

_SOURCE_LINE = re.compile(r"^(?P<name>[A-Za-z0-9_.-]+)\s*=\s*\{")


@dataclass(frozen=True)
class Finding:
    code: str
    package: str


def locked_source_kinds(lock_text: str) -> dict[str, str]:
    """Map each locked package name to ``git``, ``registry`` or another kind."""
    kinds: dict[str, str] = {}
    for package in tomllib.loads(lock_text).get("package", []):
        source = package.get("source", {})
        kinds[package["name"]] = next(iter(source), "none")
    return kinds


def check_lock(lock_text: str, *, release_commit: bool) -> list[Finding]:
    """Findings for ``lock_text`` on a release commit or on any other commit."""
    findings: list[Finding] = []
    kinds = locked_source_kinds(lock_text)
    for name in sorted(SIBLINGS):
        kind = kinds.get(name)
        if kind is None:
            continue
        if release_commit and kind == "git":
            findings.append(Finding(CODE_GIT_ON_RELEASE, name))
        elif not release_commit and kind == "registry":
            findings.append(Finding(CODE_REGISTRY_OFF_RELEASE, name))
    return findings


def strip_sources(pyproject_text: str) -> str:
    """Drop the three siblings' lines from ``[tool.uv.sources]``, nothing else."""
    out: list[str] = []
    in_sources = False
    for line in pyproject_text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("["):
            in_sources = stripped == "[tool.uv.sources]"
        elif in_sources:
            match = _SOURCE_LINE.match(stripped)
            if match and match.group("name") in SIBLINGS:
                continue
        out.append(line)
    return "".join(out)


def marker_text(version: str) -> str:
    return (
        json.dumps(
            {"release_commit": True, "version": version, "relocked": sorted(SIBLINGS)},
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def is_release_commit(root: Path) -> bool:
    return (root / MARKER_NAME).is_file()


def strip_pyproject(root: Path) -> None:
    path = root / "pyproject.toml"
    path.write_text(strip_sources(path.read_text()))


def write_marker(root: Path, version: str) -> None:
    (root / MARKER_NAME).write_text(marker_text(version))


def verify(root: Path) -> int:
    """Return 1 and name each finding on stderr when the lock breaks the pair rule."""
    release = is_release_commit(root)
    findings = check_lock((root / "uv.lock").read_text(), release_commit=release)
    kind = "release" if release else "non-release"
    for finding in findings:
        sys.stderr.write(f"{finding.code}: {finding.package} ({kind} commit)\n")
    return 1 if findings else 0
