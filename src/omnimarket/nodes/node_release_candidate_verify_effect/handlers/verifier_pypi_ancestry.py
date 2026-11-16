# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The manifest against what PyPI serves, and the cut against the launch-required changes."""

from __future__ import annotations

import base64
import binascii
import csv

from omnimarket.nodes.node_release_candidate_verify_effect.models.enum_release_candidate_finding import (
    EnumReleaseCandidateFinding as K,
)
from omnimarket.nodes.node_release_candidate_verify_effect.models.model_release_candidate_manifest import (
    ModelManifestRecordEntry,
    ModelReleaseCandidateManifest,
)
from omnimarket.nodes.node_release_candidate_verify_effect.models.model_release_candidate_verify_request import (
    ModelAncestryFact,
    ModelPypiFact,
)
from omnimarket.nodes.node_release_candidate_verify_effect.models.model_release_candidate_verify_result import (
    ModelReleaseCandidateFinding as Finding,
)


def parse_wheel_record(text: str) -> tuple[ModelManifestRecordEntry, ...]:
    """Read a wheel RECORD (path,sha256=<urlsafe base64>,size) into hex entries."""
    entries = []
    for row in csv.reader(text.splitlines()):
        if len(row) != 3:
            continue
        path, rest = row[0], row[1]
        digest = ""
        if rest.startswith("sha256="):
            raw = rest[len("sha256=") :]
            try:
                digest = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).hex()
            except binascii.Error:
                digest = ""
        entries.append(ModelManifestRecordEntry(path=path, sha256=digest))
    return tuple(entries)


def verify_pypi(
    manifest: ModelReleaseCandidateManifest, facts: tuple[ModelPypiFact, ...]
) -> list[Finding]:
    findings: list[Finding] = []
    by_file = {fact.filename: fact for fact in facts}
    for dist in manifest.distributions:
        fact = by_file.get(dist.wheel_filename)
        if fact is None:
            findings.append(
                Finding(kind=K.PYPI_FACT_MISSING, subject=dist.wheel_filename)
            )
            continue
        if fact.sha256 != dist.wheel_sha256:
            findings.append(
                Finding(
                    kind=K.PYPI_WHEEL_SHA256,
                    subject=dist.wheel_filename,
                    detail=f"PyPI {fact.sha256}, manifest {dist.wheel_sha256}",
                )
            )
        manifest_record = {e.path: e.sha256 for e in dist.record}
        pypi_record = {e.path: e.sha256 for e in fact.record}
        for path in sorted(set(manifest_record) | set(pypi_record)):
            if manifest_record.get(path) != pypi_record.get(path):
                findings.append(
                    Finding(kind=K.PYPI_RECORD, subject=f"{dist.wheel_filename}:{path}")
                )
    return findings


def verify_ancestry(
    manifest: ModelReleaseCandidateManifest, facts: tuple[ModelAncestryFact, ...]
) -> list[Finding]:
    findings: list[Finding] = []
    read = {(fact.repo, fact.sha): fact.is_ancestor for fact in facts}
    for change in manifest.required_changes:
        outcome = read.get((change.repo, change.sha))
        if outcome is not True:
            findings.append(
                Finding(
                    kind=K.CHANGE_NOT_IN_CANDIDATE,
                    subject=f"{change.repo}@{change.sha}",
                    detail=f"{change.ticket}: "
                    + (
                        "not an ancestor of the candidate tag"
                        if outcome is False
                        else "no ancestry read supplied"
                    ),
                )
            )
    return findings
