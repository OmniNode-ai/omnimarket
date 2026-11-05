# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read-only host commands and the broker-stamped ledger mirror.

The deployment owns argv and paths. Command stdout is parsed locally and is
never returned as error text. In particular auth output cannot enter a receipt.
"""

import hashlib
import json
import re
import subprocess
from pathlib import Path

from omnimarket.models.host_checks import (
    ModelHostCheckPolicy,
    ModelHostCheckSnapshot,
    ModelHostFact,
    ModelInstallClaim,
)


def _hash10(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:10]


def account_label(text: str, policy: ModelHostCheckPolicy) -> str | None:
    """Map auth status to a label using the cloud registry's hash-pair format."""
    data = json.loads(text)
    if not isinstance(data, dict) or data.get("loggedIn") is not True:
        return None
    org, email = data.get("orgId"), data.get("email")
    if not isinstance(org, str) or not isinstance(email, str) or not org or not email:
        return None
    pair = (_hash10(org.strip()), _hash10(email.strip().lower()))
    for identity in policy.account_identities:
        if (identity.org_hash, identity.email_hash) == pair:
            return identity.label
    return "unlisted-" + "-".join(pair)


def installed_claims(text: str) -> tuple[ModelInstallClaim, ...]:
    """Only header cells of STATUS/TERMINAL rows count; actor is never trusted.

    This function reads the append service's mirror, not a lane-supplied row.
    Duplicate principal cells destroy attribution rather than choosing one.
    """
    claims: list[ModelInstallClaim] = []
    for line in text.splitlines():
        cells = line.split(" | ")
        if len(cells) < 4 or cells[1] not in ("STATUS", "TERMINAL"):
            continue
        header = cells[2:-1]
        principals = [
            c.removeprefix("principal=") for c in header if c.startswith("principal=")
        ]
        actors = [c.removeprefix("actor=") for c in header if c.startswith("actor=")]
        for cell in header:
            match = re.fullmatch(r"installed=([A-Za-z0-9_.-]+):([0-9a-f]{7,40})", cell)
            if match:
                claims.append(
                    ModelInstallClaim.model_validate(
                        {
                            "row_id": hashlib.sha256(line.encode()).hexdigest(),
                            "host": match[1],
                            "sha": match[2],
                            "row_type": cells[1],
                            "writer_principal": principals[0]
                            if len(principals) == 1
                            else None,
                            "actor": actors[0] if len(actors) == 1 else "",
                        }
                    )
                )
    return tuple(claims)


class MemoryHostCheckFacts:
    def __init__(self, snapshot: ModelHostCheckSnapshot) -> None:
        self.snapshot = snapshot

    def read(self, policy: ModelHostCheckPolicy) -> ModelHostCheckSnapshot:
        return self.snapshot


class HostCheckFacts:
    """Read configured lab identities, revisions and their ancestry, never repair."""

    def _run(
        self, argv: tuple[str, ...], timeout: int
    ) -> subprocess.CompletedProcess[str]:
        if not argv:
            raise ValueError("missing prerequisite read")
        return subprocess.run(
            argv, capture_output=True, text=True, check=False, timeout=timeout
        )

    def _read(self, argv: tuple[str, ...], timeout: int) -> str:
        done = self._run(argv, timeout)
        if done.returncode:
            raise OSError("host read failed")
        return done.stdout

    def read(self, policy: ModelHostCheckPolicy) -> ModelHostCheckSnapshot:
        timeout = policy.read_timeout_seconds
        prerequisite = ""
        hosts: list[ModelHostFact] = []
        try:
            claims = installed_claims(Path(policy.ledger_path).read_text())
            ledger_readable = True
        except OSError:
            claims, ledger_readable = (), False
        for binding in policy.hosts:
            revision: str | None
            try:
                observed = json.loads(self._read(binding.principals_read, timeout))
                lanes = set(observed["lane_principals"])
                installs = set(observed["install_principals"])
                checker = observed["check_principal"]
                if (
                    not lanes
                    or not installs
                    or not all(isinstance(p, str) and p for p in lanes | installs)
                    or lanes & installs
                    or checker in lanes | installs
                    or installs != set(policy.install_principals)
                    or checker != policy.check_principal
                ):
                    raise ValueError("service principals are not distinct")
            except (
                OSError,
                ValueError,
                KeyError,
                TypeError,
                subprocess.SubprocessError,
            ):
                prerequisite = (
                    "R8: host service-principal read missing, shared or inconsistent"
                )
            try:
                label = account_label(self._read(binding.auth_read, timeout), policy)
            except (OSError, ValueError, TypeError, subprocess.SubprocessError):
                label = None
            try:
                revision = self._read(binding.revision_read, timeout).strip()
                if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
                    revision = None
            except (OSError, ValueError, subprocess.SubprocessError):
                revision = None
            contained: dict[str, bool | None] = {}
            for claim in claims:
                if claim.host != binding.host:
                    continue
                contained[claim.sha] = None
                if revision is None or not binding.contains_read:
                    continue
                argv = tuple(
                    arg.replace("{sha}", claim.sha).replace("{revision}", revision)
                    for arg in binding.contains_read
                )
                try:
                    done = self._run(argv, timeout)
                    if done.returncode in (0, 1):
                        contained[claim.sha] = done.returncode == 0
                except (OSError, ValueError, subprocess.SubprocessError):
                    pass
            hosts.append(
                ModelHostFact(
                    host=binding.host,
                    account_label=label,
                    installed_revision=revision,
                    contains=contained,
                )
            )
        return ModelHostCheckSnapshot(
            hosts=tuple(hosts),
            claims=claims,
            ledger_readable=ledger_readable,
            prerequisite_error=prerequisite,
        )
