# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The manifest against PyPI, and the cut against its required changes (OMN-23343, AC2 and AC3)."""

from __future__ import annotations

import base64
import hashlib

import pytest

from omnimarket.nodes.node_release_candidate_verify_effect.handlers.handler_release_candidate_verify import (
    HandlerReleaseCandidateVerify,
)
from omnimarket.nodes.node_release_candidate_verify_effect.handlers.verifier_pypi_ancestry import (
    parse_wheel_record,
)
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
    ModelReleaseCandidateVerifyRequest,
)
from omnimarket.nodes.node_release_candidate_verify_effect.models.model_release_candidate_verify_result import (
    ModelReleaseCandidateVerifyResult,
)

pytestmark = pytest.mark.unit


def _facts(manifest: ModelReleaseCandidateManifest) -> tuple[ModelPypiFact, ...]:
    return tuple(
        ModelPypiFact(
            name=d.name,
            version=d.version,
            filename=d.wheel_filename,
            sha256=d.wheel_sha256,
            record=d.record,
        )
        for d in manifest.distributions
    )


def _pypi(
    manifest: ModelReleaseCandidateManifest, facts: tuple[ModelPypiFact, ...]
) -> ModelReleaseCandidateVerifyResult:
    return HandlerReleaseCandidateVerify().handle(
        ModelReleaseCandidateVerifyRequest(
            operation="verify_pypi", manifest=manifest, pypi_facts=facts
        )
    )


def test_manifest_matches_pypi(manifest: ModelReleaseCandidateManifest) -> None:
    result = _pypi(manifest, _facts(manifest))
    assert result.matches is True
    assert result.reading == "candidate"


def test_pypi_wheel_sha256_differs(manifest: ModelReleaseCandidateManifest) -> None:
    facts = list(_facts(manifest))
    facts[0] = facts[0].model_copy(update={"sha256": "0" * 64})
    result = _pypi(manifest, tuple(facts))
    assert result.matches is False
    assert [f.kind for f in result.findings] == [K.PYPI_WHEEL_SHA256]


def test_pypi_record_differs_names_the_path(
    manifest: ModelReleaseCandidateManifest,
) -> None:
    facts = list(_facts(manifest))
    record = list(facts[1].record)
    record[0] = ModelManifestRecordEntry(path=record[0].path, sha256="f" * 64)
    facts[1] = facts[1].model_copy(update={"record": tuple(record)})
    result = _pypi(manifest, tuple(facts))
    assert [(f.kind, f.subject.split(":")[1]) for f in result.findings] == [
        (K.PYPI_RECORD, record[0].path)
    ]


def test_pypi_fact_missing(manifest: ModelReleaseCandidateManifest) -> None:
    result = _pypi(manifest, _facts(manifest)[:2])
    assert [f.kind for f in result.findings] == [K.PYPI_FACT_MISSING]


def test_parse_wheel_record_reads_hex_digests() -> None:
    digest = hashlib.sha256(b"hello").digest()
    line = f"pkg/a.py,sha256={base64.urlsafe_b64encode(digest).rstrip(b'=').decode()},5\npkg-1.dist-info/RECORD,,\n"
    entries = parse_wheel_record(line)
    assert entries[0].sha256 == digest.hex()
    assert entries[1].sha256 == ""


def _ancestry(
    manifest: ModelReleaseCandidateManifest, ancestor: bool | None
) -> ModelReleaseCandidateVerifyResult:
    change = manifest.required_changes[0]
    facts = (
        ()
        if ancestor is None
        else (
            ModelAncestryFact(repo=change.repo, sha=change.sha, is_ancestor=ancestor),
        )
    )
    return HandlerReleaseCandidateVerify().handle(
        ModelReleaseCandidateVerifyRequest(
            operation="verify_ancestry", manifest=manifest, ancestry_facts=facts
        )
    )


def test_cut_containing_every_required_change_passes(
    manifest: ModelReleaseCandidateManifest,
) -> None:
    assert _ancestry(manifest, True).matches is True


def test_cut_missing_a_required_change_is_refused(
    manifest: ModelReleaseCandidateManifest,
) -> None:
    result = _ancestry(manifest, False)
    assert result.matches is False
    assert result.reading == "refused"
    assert result.findings[0].kind is K.CHANGE_NOT_IN_CANDIDATE
    assert "OMN-0001" in result.findings[0].detail


def test_unread_ancestry_is_refused_not_passed(
    manifest: ModelReleaseCandidateManifest,
) -> None:
    assert _ancestry(manifest, None).matches is False
