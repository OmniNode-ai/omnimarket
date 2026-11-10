# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The liveness reader that runs the register completeness command against a checkout of the PR head (OMN-23529).

The command (the overlay's reader.argv, onex-automation-liveness-check where deployed) reads the private register and every
registry checkout and prints one ``undeclared_source: <repository>/<path>: <detail>`` line per installer source no entry
declares. This adapter first proves the repository's checkout is at the PR head, takes the paths the head changes against the
overlay's base ref, runs the command, and keeps the undeclared sources of this repository that the head changes: a source
already undeclared on the base is not this head's to hold. Anything that stops the read raises LivenessReadError.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.nodes.node_pr_landing_liveness_read_effect.handlers.liveness_read_errors import (
    LivenessReadError,
)
from omnimarket.nodes.node_pr_landing_liveness_read_effect.overlay import (
    ModelLivenessReadOverlay,
)

UNDECLARED_PREFIX = "undeclared_source: "
NOT_SCANNED_PREFIX = "repository_not_scanned: "
# Exit codes of the completeness command: 0 agree, 1 findings, 2 the check could not run.
_READ_EXITS = (0, 1)


class CommandLivenessReader:
    """ProtocolLivenessReader over the register completeness command."""

    def __init__(
        self, overlay: ModelLivenessReadOverlay, environ: Mapping[str, str]
    ) -> None:
        self._overlay = overlay
        self._environ = environ

    def _env_path(self, name: str) -> Path:
        value = self._environ.get(name, "").strip()
        if not value:
            raise LivenessReadError(f"{name} names no directory")
        return Path(value)

    def _git(self, checkout: Path, *args: str) -> str:
        try:
            done = subprocess.run(
                ["git", "-C", str(checkout), *args],
                capture_output=True,
                text=True,
                check=False,
                timeout=self._overlay.timeouts.git_seconds,
                env=scrub_git_location_env(os.environ),
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise LivenessReadError(
                f"git {args[0]} in {checkout.name}: {type(exc).__name__}"
            ) from exc
        if done.returncode != 0:
            first = (done.stderr.strip().splitlines() or [""])[0]
            raise LivenessReadError(
                f"git {args[0]} in {checkout.name} exited {done.returncode}: {first}"
            )
        return done.stdout

    def read(self, repository: str, head_sha: str) -> tuple[str, ...]:
        checkout_cfg = self._overlay.checkout
        internal = self._env_path(checkout_cfg.internal_root_env)
        registry = self._env_path(checkout_cfg.registry_root_env)
        name = repository.split("/")[-1]
        checkout = registry / name
        if not checkout.is_dir():
            raise LivenessReadError(
                f"no checkout of {repository} under the registry root"
            )
        at = self._git(checkout, "rev-parse", "HEAD").strip()
        if at != head_sha:
            raise LivenessReadError(
                f"the checkout of {repository} is at {at}, not the head {head_sha}"
            )
        changed = {
            line.strip()
            for line in self._git(
                checkout, "diff", "--name-only", f"{checkout_cfg.base_ref}...HEAD"
            ).splitlines()
            if line.strip()
        }
        argv = [
            *self._overlay.reader.argv,
            "--internal-root",
            str(internal),
            "--registry-root",
            str(registry),
        ]
        try:
            done = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                check=False,
                timeout=self._overlay.timeouts.read_seconds,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise LivenessReadError(
                f"the liveness reader did not answer: {type(exc).__name__}"
            ) from exc
        if done.returncode not in _READ_EXITS:
            first = (done.stderr.strip().splitlines() or [""])[0]
            raise LivenessReadError(
                f"the liveness reader exited {done.returncode}: {first}"
            )
        found: set[str] = set()
        for line in done.stdout.splitlines():
            if (
                line.startswith(NOT_SCANNED_PREFIX)
                and line[len(NOT_SCANNED_PREFIX) :].split(": ", 1)[0] == name
            ):
                # a scan that left this repository out found nothing in it: the read was not made
                raise LivenessReadError(
                    f"the liveness reader did not scan {repository}"
                )
            if not line.startswith(UNDECLARED_PREFIX):
                continue
            subject = line[len(UNDECLARED_PREFIX) :].split(": ", 1)[0]
            owner, _, path = subject.partition("/")
            if owner == name and path in changed:
                found.add(path)
        return tuple(sorted(found))


__all__ = ["NOT_SCANNED_PREFIX", "UNDECLARED_PREFIX", "CommandLivenessReader"]
