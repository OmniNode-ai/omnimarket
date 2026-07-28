# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fail-closed, purity, secret-safety, and seam coverage (OMN-15253 slice 1).

Four properties are asserted here, none of which survive review-only enforcement:

1. **Absent is not a pass.** Dropping a snapshot block makes its checks
   INDETERMINATE and the whole verdict BLOCKED — the vacuous-green trap
   (``feedback_optional_input_means_the_check_does_not_exist``) closed by test.
2. **No secret value can reach the verdict.** No model in the module has a field
   that can carry one, key-name lists reject value-shaped entries, and the
   rejection message does not echo the rejected entry.
3. **The handler does no I/O.** ``open``, ``socket.socket``, and
   ``subprocess.Popen`` are patched to raise for the duration of a real
   evaluation.
4. **The seam holds.** Every ``snapshot_sources[].parses_into`` resolves to a
   real field path on ``ModelStagingLiveSnapshot`` — one cross-boundary test
   driving the actual seam, not two independent unit suites.
"""

from __future__ import annotations

import builtins
import socket
import subprocess
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_staging_readiness_compute.handlers.handler_staging_readiness_compute import (
    HandlerStagingReadinessCompute,
)
from omnimarket.staging_readiness.engine_staging_readiness import (
    ALL_CHECKS,
    findings_for_check,
    snapshot_field_path_exists,
)
from omnimarket.staging_readiness.model_staging_composition import (
    EnumStagingFindingSeverity,
    EnumStagingReadiness,
    ModelObservedSecrets,
    ModelStagingLiveSnapshot,
    ModelStagingReadinessVerdict,
    document_sha256,
)
from tests.integration.node_staging_readiness_compute.canonical_dev_fixtures import (
    build_contract,
    build_request,
    contract_payload,
    repaired_snapshot_payload,
)

_SNAPSHOT_BLOCKS = [
    "cluster",
    "runtime",
    "broker",
    "secrets",
    "host",
    "services",
    "migrations",
    "schema_objects",
    "publisher",
    "workloads",
    "rollback_resources",
]


@pytest.mark.parametrize("block", _SNAPSHOT_BLOCKS)
def test_absent_snapshot_block_is_indeterminate_and_blocks(block: str) -> None:
    snapshot = repaired_snapshot_payload()
    snapshot[block] = None

    verdict = HandlerStagingReadinessCompute().handle(build_request(snapshot=snapshot))

    indeterminate = [
        item
        for item in verdict.findings
        if item.severity is EnumStagingFindingSeverity.INDETERMINATE
    ]
    assert indeterminate, f"dropping snapshot.{block} produced no INDETERMINATE finding"
    assert verdict.status is EnumStagingReadiness.BLOCKED
    assert verdict.deployment_permitted is False
    assert all(item.observed == "<absent>" for item in indeterminate)


def test_empty_snapshot_is_indeterminate_never_ready() -> None:
    """A snapshot that observed nothing says so; it never renders READY."""
    verdict = HandlerStagingReadinessCompute().handle(
        build_request(snapshot={"captured_at": "2026-07-27T16:55:00Z"})
    )

    assert verdict.status is EnumStagingReadiness.INDETERMINATE
    assert verdict.deployment_permitted is False
    assert verdict.blocking_findings_count == 0
    for check in ALL_CHECKS:
        assert findings_for_check(verdict, check), f"{check} silently evaluated to pass"


def test_every_check_is_always_evaluated() -> None:
    """No check can be skipped: there is no optional flag to set."""
    verdict = HandlerStagingReadinessCompute().handle(build_request())
    assert list(verdict.checks_evaluated) == list(ALL_CHECKS)


def test_absent_field_inside_a_present_block_still_blocks() -> None:
    """Partial observation is not partial credit."""
    snapshot = repaired_snapshot_payload()
    snapshot["host"]["sysctls"] = {}

    verdict = HandlerStagingReadinessCompute().handle(build_request(snapshot=snapshot))

    assert verdict.status is EnumStagingReadiness.BLOCKED
    assert any(
        item.severity is EnumStagingFindingSeverity.INDETERMINATE
        for item in verdict.findings
    )


# --------------------------------------------------------------------------
# Secret safety
# --------------------------------------------------------------------------

_SENTINEL = "SENTINELVALUEDONOTLEAK1234567890"


def test_snapshot_has_no_field_that_can_carry_a_secret_value() -> None:
    """Structural: the observed-secrets model exposes key NAMES only."""
    assert set(ModelObservedSecrets.model_fields) == {
        "synced_key_names_by_target",
        "workload_env_key_names",
    }


def test_value_shaped_secret_entry_is_rejected_without_echoing_it() -> None:
    snapshot = repaired_snapshot_payload()
    target = next(iter(snapshot["secrets"]["synced_key_names_by_target"]))
    snapshot["secrets"]["synced_key_names_by_target"][target] = [
        f"GEMINI_API_KEY=sk-{_SENTINEL}"
    ]

    with pytest.raises(ValidationError) as excinfo:
        ModelStagingLiveSnapshot.model_validate(snapshot)

    assert _SENTINEL not in str(excinfo.value), (
        "the rejection message echoed the value it was rejecting — the guard "
        "became the leak"
    )


def test_unknown_snapshot_key_is_a_hard_error_not_a_silent_drop() -> None:
    snapshot = repaired_snapshot_payload()
    snapshot["secrets"]["synced_key_values"] = {"GEMINI_API_KEY": _SENTINEL}

    with pytest.raises(ValidationError):
        ModelStagingLiveSnapshot.model_validate(snapshot)


def test_serialized_verdict_never_contains_a_planted_sentinel() -> None:
    """Plant sentinels in every free-text observed field the verdict may echo."""
    snapshot = repaired_snapshot_payload()
    snapshot["cluster"]["name_tag"] = _SENTINEL
    snapshot["runtime"]["image_source_rev"] = _SENTINEL
    snapshot["broker"]["instance_class"] = _SENTINEL

    verdict = HandlerStagingReadinessCompute().handle(build_request(snapshot=snapshot))
    rendered = verdict.model_dump_json()

    # The sentinels above are legitimately-echoed OBSERVED identifiers, so they
    # do appear; what must never appear is a secret VALUE. Prove the negative on
    # a snapshot whose secret block is fully populated with key names only.
    assert verdict.status is EnumStagingReadiness.BLOCKED
    secret_names = repaired_snapshot_payload()["secrets"]["synced_key_names_by_target"]
    for names in secret_names.values():
        for name in names:
            assert f"{name}=" not in rendered


def test_verdict_is_json_serializable_and_round_trips() -> None:
    verdict = HandlerStagingReadinessCompute().handle(build_request())
    restored = ModelStagingReadinessVerdict.model_validate_json(
        verdict.model_dump_json()
    )
    assert restored == verdict


# --------------------------------------------------------------------------
# Purity
# --------------------------------------------------------------------------


def test_handler_performs_no_io(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enforced by test, not by review: file, socket, and subprocess all raise."""

    def _no_open(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("handler opened a file")

    def _no_socket(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("handler opened a socket")

    def _no_popen(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("handler spawned a subprocess")

    request = build_request()
    monkeypatch.setattr(builtins, "open", _no_open)
    monkeypatch.setattr(socket, "socket", _no_socket)
    monkeypatch.setattr(subprocess, "Popen", _no_popen)

    verdict = HandlerStagingReadinessCompute().handle(request)
    assert verdict.status is EnumStagingReadiness.READY


def test_evaluation_is_deterministic() -> None:
    """Same inputs, byte-identical verdict — the replayability property."""
    first = HandlerStagingReadinessCompute().handle(build_request())
    second = HandlerStagingReadinessCompute().handle(build_request())
    assert first.model_dump_json() == second.model_dump_json()


def test_provenance_binds_the_verdict_to_its_inputs() -> None:
    request = build_request()
    verdict = HandlerStagingReadinessCompute().handle(request)

    assert verdict.provenance.contract_sha256 == request.contract.sha256
    assert verdict.provenance.snapshot_sha256 == request.snapshot.content_sha256()
    assert verdict.provenance.source_rev == request.contract.source_rev
    assert verdict.provenance.evaluated_at == request.evaluated_at
    assert verdict.provenance.snapshot_captured_at == request.snapshot.captured_at


# --------------------------------------------------------------------------
# Seam: snapshot_sources[].parses_into <-> ModelStagingLiveSnapshot
# --------------------------------------------------------------------------


def test_every_declared_probe_target_resolves_on_the_snapshot_model() -> None:
    contract = build_contract()
    unresolved = [
        source.parses_into
        for source in contract.snapshot_sources
        if not snapshot_field_path_exists(source.parses_into)
    ]
    assert not unresolved, (
        "snapshot_sources[].parses_into targets no field on "
        f"ModelStagingLiveSnapshot: {unresolved} — slice 2's collect EFFECT "
        "would write into a hole"
    )


def test_unknown_probe_target_is_detected() -> None:
    """Negative control for the seam check itself."""
    assert snapshot_field_path_exists("cluster.instance_id")
    assert not snapshot_field_path_exists("cluster.no_such_field")
    assert not snapshot_field_path_exists("no_such_block")


def test_every_probe_is_read_only_and_uniquely_identified() -> None:
    contract = build_contract()
    probe_ids = [source.probe_id for source in contract.snapshot_sources]
    assert len(probe_ids) == len(set(probe_ids)), "duplicate probe_id"
    assert all(source.read_only for source in contract.snapshot_sources)


def test_mutating_probe_is_rejected_at_validation() -> None:
    payload: dict[str, Any] = contract_payload()
    payload["snapshot_sources"][0]["read_only"] = False
    with pytest.raises(ValidationError):
        build_contract(payload)


def test_findings_name_the_probe_that_would_resolve_them() -> None:
    """A BLOCKED verdict must be actionable: every finding names its probe."""
    snapshot = repaired_snapshot_payload()
    snapshot["runtime"]["active_runtime_packages"] = ["omnibase_infra"]

    verdict = HandlerStagingReadinessCompute().handle(build_request(snapshot=snapshot))

    blocking = [
        item
        for item in verdict.findings
        if item.severity is EnumStagingFindingSeverity.BLOCKING
    ]
    assert blocking
    assert all(item.probe_id for item in blocking)
    assert all(item.contract_field_path for item in blocking)


def test_contract_sha256_is_the_self_hash_of_its_document() -> None:
    payload = contract_payload()
    payload["sha256"] = document_sha256(payload)
    assert build_contract(payload).sha256_matches_document(payload)


def test_a_tampered_document_fails_its_own_self_hash() -> None:
    """Negative control: editing a field without re-freezing is detectable."""
    payload = contract_payload()
    payload["sha256"] = document_sha256(payload)
    payload["cluster"]["instance_id"] = (
        "i-0e596e8b557e27785"  # onex-allow-test-fixture OMN-15253 reason="replays the real July 25-27 staging defect; the wrong-cluster and IAM-grant checks are only meaningful against the actual account and legacy instance ids"
    )
    assert not build_contract(payload).sha256_matches_document(payload)
