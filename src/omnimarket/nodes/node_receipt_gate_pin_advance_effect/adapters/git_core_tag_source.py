# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The source repository's dev tags read from its clone with git alone (OMN-20134).

One ``for-each-ref`` lists the dev-merge tags with their commits and one
``cat-file --batch-check`` reads the gate file's blob id at each, so the read
costs no GitHub request. A tag whose commit predates the gate file is left out.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Final

from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.models.model_receipt_gate_pin_advance import (
    ModelCoreDevTag,
)
from omnimarket.nodes.node_receipt_gate_pin_advance_effect.protocols import (
    PinAdvanceError,
)

_DEV_TAG: Final = re.compile(r"^dev-\d{14}-[0-9a-f]{12}$")
_OID: Final = re.compile(r"^[0-9a-f]{40}$")
_GIT_TIMEOUT_S: Final = 120


class GitCloneCoreTagSource:
    """Dev tags and gate blobs from a clone of the source repository."""

    def __init__(self, clone: Path, workflow_path: str, *, fetch_tags: bool) -> None:
        self._clone = clone
        self._workflow_path = workflow_path
        self._fetch_tags = fetch_tags

    def _git(self, *args: str, stdin: str | None = None) -> str:
        env = scrub_git_location_env(os.environ)
        try:
            done = subprocess.run(
                ["git", "-C", str(self._clone), *args],
                input=stdin,
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT_S,
                check=False,
                env=env,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PinAdvanceError(f"git {args[0]} in {self._clone}: {exc}") from exc
        if done.returncode != 0:
            raise PinAdvanceError(
                f"git {args[0]} in {self._clone} exited {done.returncode}: "
                f"{done.stderr.strip()[:300]}"
            )
        return done.stdout

    def dev_tags(self, *, required_tag: str) -> tuple[ModelCoreDevTag, ...]:
        if self._fetch_tags:
            self._git("fetch", "--quiet", "--tags", "origin")
        listing = self._git(
            "for-each-ref",
            "--format=%(refname:strip=2) %(objectname) %(*objectname)",
            "refs/tags/dev-*",
        )
        commits: dict[str, str] = {}
        for line in listing.splitlines():
            parts = line.split()
            if len(parts) < 2 or not _DEV_TAG.match(parts[0]):
                continue
            commit = parts[2] if len(parts) > 2 else parts[1]
            if _OID.match(commit):
                commits[parts[0]] = commit
        if required_tag not in commits:
            raise PinAdvanceError(
                f"{required_tag} is not a dev tag of the clone at {self._clone}"
            )
        specs = [f"{commit}:{self._workflow_path}" for commit in commits.values()]
        answers = self._git("cat-file", "--batch-check", stdin="\n".join(specs) + "\n")
        tags: list[ModelCoreDevTag] = []
        for (tag, commit), answer in zip(
            commits.items(), answers.splitlines(), strict=True
        ):
            blob = answer.split()
            if len(blob) == 3 and blob[1] == "blob" and _OID.match(blob[0]):
                tags.append(
                    ModelCoreDevTag(
                        tag=tag, commit_sha=commit, receipt_gate_blob=blob[0]
                    )
                )
        if not any(t.tag == required_tag for t in tags):
            raise PinAdvanceError(
                f"{required_tag} holds no {self._workflow_path}; nothing to advance to"
            )
        return tuple(tags)
