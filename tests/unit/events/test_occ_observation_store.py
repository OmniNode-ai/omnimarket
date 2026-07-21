# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for the OCC observation store path/render/parse convention (OMN-14888)."""

from __future__ import annotations

import pytest

from omnimarket.events.occ_autoauthor import ModelOccAutoauthorObservation
from omnimarket.events.occ_observation_record import ModelOccObservationRecord
from omnimarket.events.occ_observation_store import (
    OCC_OBSERVATIONS_ROOT,
    occ_observation_record_relpath,
    parse_occ_observation_record,
    render_occ_observation_record,
)


def _record(
    *,
    product_repo: str = "OmniNode-ai/omnimarket",
    product_pr_number: int = 1841,
    head_sha: str = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
    policy_version: str = "v1",
    workflow_run_id: int = 123456,
    run_attempt: int = 1,
    minted_by_node: bool = True,
    attestation_match: bool = True,
    occ_preflight_eligible: bool = True,
) -> ModelOccObservationRecord:
    return ModelOccObservationRecord(
        product_repo=product_repo,
        product_pr_number=product_pr_number,
        head_sha=head_sha,
        policy_version=policy_version,
        workflow_run_id=workflow_run_id,
        run_attempt=run_attempt,
        recorded_at="2026-07-21T00:00:00Z",
        observation=ModelOccAutoauthorObservation(
            product_repo=product_repo,
            product_pr_number=product_pr_number,
            occ_pr_number=4500,
            minted_by_node=minted_by_node,
            attestation_match=attestation_match,
            occ_preflight_eligible=occ_preflight_eligible,
            observed_at="2026-07-21T00:00:00Z",
            reason="",
        ),
    )


@pytest.mark.unit
class TestOccObservationRecordRelpath:
    def test_shape_and_root(self) -> None:
        path = occ_observation_record_relpath(_record())
        assert path.startswith(f"{OCC_OBSERVATIONS_ROOT}/")
        assert path == (
            f"{OCC_OBSERVATIONS_ROOT}/OmniNode-ai__omnimarket/pr-1841/"
            "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678__v1__run123456-1.yaml"
        )

    def test_identical_raw_attempt_maps_to_identical_path(self) -> None:
        assert occ_observation_record_relpath(
            _record()
        ) == occ_observation_record_relpath(_record())

    def test_different_run_attempt_maps_to_different_path(self) -> None:
        first = occ_observation_record_relpath(_record(run_attempt=1))
        second = occ_observation_record_relpath(_record(run_attempt=2))
        assert first != second

    def test_different_workflow_run_id_maps_to_different_path(self) -> None:
        first = occ_observation_record_relpath(_record(workflow_run_id=1))
        second = occ_observation_record_relpath(_record(workflow_run_id=2))
        assert first != second

    def test_different_head_sha_maps_to_different_path(self) -> None:
        first = occ_observation_record_relpath(_record(head_sha="a" * 40))
        second = occ_observation_record_relpath(_record(head_sha="b" * 40))
        assert first != second

    def test_path_traversal_characters_are_sanitized(self) -> None:
        hostile = _record(product_repo="../../etc/passwd", policy_version="../v1")
        path = occ_observation_record_relpath(hostile)
        assert ".." not in path
        assert path.startswith(f"{OCC_OBSERVATIONS_ROOT}/")


@pytest.mark.unit
class TestRenderParseRoundTrip:
    def test_round_trip_is_exact(self) -> None:
        record = _record()
        rendered = render_occ_observation_record(record)
        parsed = parse_occ_observation_record(rendered)
        assert parsed == record

    def test_render_is_deterministic(self) -> None:
        record = _record()
        assert render_occ_observation_record(record) == render_occ_observation_record(
            record
        )

    def test_render_carries_schema_version_envelope(self) -> None:
        rendered = render_occ_observation_record(_record())
        assert "schema_version:" in rendered

    def test_parse_rejects_non_mapping_yaml(self) -> None:
        with pytest.raises(ValueError, match="mapping"):
            parse_occ_observation_record("- just\n- a\n- list\n")

    def test_round_trip_preserves_non_clean_observation(self) -> None:
        record = _record(minted_by_node=False, attestation_match=False)
        parsed = parse_occ_observation_record(render_occ_observation_record(record))
        assert parsed.observation.is_clean is False
        assert parsed == record
