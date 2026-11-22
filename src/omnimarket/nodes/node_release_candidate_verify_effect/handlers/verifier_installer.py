# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The served curl installer against the candidate manifest (OMN-25070 AC4).

The script must be the one the manifest pins by sha256, install the manifest's version of the
customer package, name no package index other than PyPI and never install without --no-build.
A mismatch is a finding that names the line; nothing raises.
"""

from __future__ import annotations

import hashlib
import re

from omnimarket.nodes.node_release_candidate_verify_effect.models.enum_release_candidate_finding import (
    EnumReleaseCandidateFinding as K,
)
from omnimarket.nodes.node_release_candidate_verify_effect.models.model_release_candidate_manifest import (
    ModelReleaseCandidateManifest,
)
from omnimarket.nodes.node_release_candidate_verify_effect.models.model_release_candidate_verify_result import (
    ModelReleaseCandidateFinding,
)

PYPI_INDEXES = frozenset({"https://pypi.org/simple", "https://pypi.org/simple/"})
_ASSIGNMENT = re.compile(r'^([A-Z_]+)="([^"]*)"$')
_INDEX_OPTION = re.compile(
    r"(--index-url|--extra-index-url|--default-index|--index)(?:=|\s+)(\S+)"
)
_INDEX_VARIABLE = re.compile(
    r"\b(UV_INDEX_URL|UV_EXTRA_INDEX_URL|UV_DEFAULT_INDEX|UV_INDEX|PIP_INDEX_URL|PIP_EXTRA_INDEX_URL)=(\S+)"
)
_INSTALL = re.compile(r"\b(?:tool|pip) install\b")
_NO_BUILD = ("--no-build", "--only-binary=:all:")


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _code_lines(script: str) -> list[tuple[int, str]]:
    lines: list[tuple[int, str]] = []
    for n, raw in enumerate(script.splitlines(), start=1):
        stripped = raw.strip()
        if stripped and not stripped.startswith("#"):
            lines.append((n, stripped))
    return lines


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def verify_installer(
    manifest: ModelReleaseCandidateManifest,
    script: str,
) -> list[ModelReleaseCandidateFinding]:
    findings: list[ModelReleaseCandidateFinding] = []

    if manifest.installer is None:
        return [
            ModelReleaseCandidateFinding(
                kind=K.INSTALLER_MISSING_FROM_MANIFEST,
                subject="installer",
                detail="the manifest pins no installer sha256",
            )
        ]

    actual = hashlib.sha256(script.encode("utf-8")).hexdigest()
    if actual != manifest.installer.sha256:
        findings.append(
            ModelReleaseCandidateFinding(
                kind=K.INSTALLER_SHA256,
                subject="installer",
                detail=f"{actual}, manifest {manifest.installer.sha256}",
            )
        )

    lines = _code_lines(script)

    package: str | None = None
    version: str | None = None
    for _, line in lines:
        m = _ASSIGNMENT.match(line)
        if m is None:
            continue
        key, value = m.group(1), m.group(2)
        if key == "ONEX_PACKAGE" and package is None:
            package = value
        elif key == "ONEX_VERSION" and version is None:
            version = value

    if package is None:
        findings.append(
            ModelReleaseCandidateFinding(
                kind=K.INSTALLER_PACKAGE,
                subject="ONEX_PACKAGE",
                detail="the manifest names no such distribution",
            )
        )
    else:
        distribution = None
        for d in manifest.distributions:
            if _normalize(d.name) == _normalize(package):
                distribution = d
                break
        if distribution is None:
            findings.append(
                ModelReleaseCandidateFinding(
                    kind=K.INSTALLER_PACKAGE,
                    subject=package,
                    detail="the manifest names no such distribution",
                )
            )
        elif version is None or version != distribution.version:
            findings.append(
                ModelReleaseCandidateFinding(
                    kind=K.INSTALLER_VERSION,
                    subject=package,
                    detail=f"{version or 'none'}, manifest {distribution.version}",
                )
            )

    for n, line in lines:
        for regex in (_INDEX_OPTION, _INDEX_VARIABLE):
            for m in regex.finditer(line):
                value = _unquote(m.group(2))
                if value not in PYPI_INDEXES:
                    findings.append(
                        ModelReleaseCandidateFinding(
                            kind=K.INSTALLER_INDEX,
                            subject=f"line {n}",
                            detail=f"{value}: {line}",
                        )
                    )

    for n, line in lines:
        if _INSTALL.search(line) is not None and not any(
            token in line for token in _NO_BUILD
        ):
            findings.append(
                ModelReleaseCandidateFinding(
                    kind=K.INSTALLER_BUILD,
                    subject=f"line {n}",
                    detail=line,
                )
            )

    return findings
