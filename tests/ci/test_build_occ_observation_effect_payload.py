# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for scripts/ci/build_occ_observation_effect_payload.py (OMN-14888)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.events.occ_observation_store import OCC_OBSERVATION_EVIDENCE_TICKET
from scripts.ci.build_occ_observation_effect_payload import (
    build_payload,
    find_observation_payload,
    main,
)

_OBSERVATION = {
    "product_repo": "OmniNode-ai/omnimarket",
    "product_pr_number": 1841,
    "occ_pr_number": 4500,
    "minted_by_node": True,
    "attestation_match": True,
    "occ_preflight_eligible": True,
    "observed_at": "2026-07-21T00:00:00Z",
    "reason": "",
}


@pytest.mark.unit
class TestFindObservationPayload:
    def test_finds_top_level_observation(self) -> None:
        assert find_observation_payload(_OBSERVATION) == _OBSERVATION

    def test_finds_nested_observation_inside_a_receipt_wrapper(self) -> None:
        wrapper = {
            "status": "success",
            "steps": [{"name": "x", "result": {"observation": _OBSERVATION}}],
        }
        assert find_observation_payload(wrapper) == _OBSERVATION

    def test_returns_none_when_absent(self) -> None:
        assert find_observation_payload({"status": "success"}) is None

    def test_returns_none_for_non_container(self) -> None:
        assert find_observation_payload("just a string") is None


@pytest.mark.unit
class TestBuildPayload:
    def test_shape(self) -> None:
        payload = build_payload(
            observation=_OBSERVATION,
            head_sha="a" * 40,
            policy_version="v1",
            workflow_run_id=123,
            run_attempt=1,
            recorded_at="2026-07-21T00:00:01Z",
            occ_repo="OmniNode-ai/onex_change_control",
            mode="dry_run",
            evidence_ticket=OCC_OBSERVATION_EVIDENCE_TICKET,
        )
        assert payload["mode"] == "dry_run"
        assert payload["occ_repo"] == "OmniNode-ai/onex_change_control"
        record = payload["record"]
        assert isinstance(record, dict)
        assert record["product_repo"] == "OmniNode-ai/omnimarket"
        assert record["product_pr_number"] == 1841
        assert record["head_sha"] == "a" * 40
        assert record["workflow_run_id"] == 123
        assert record["run_attempt"] == 1
        assert record["observation"] == _OBSERVATION


@pytest.mark.unit
class TestMainCli:
    def test_end_to_end_writes_expected_file(self, tmp_path: Path) -> None:
        observe_result = tmp_path / "occ_attestation_observe_result.json"
        observe_result.write_text(json.dumps({"observation": _OBSERVATION}))
        output = tmp_path / "occ_observation_effect_payload.json"

        exit_code = main(
            [
                "--observe-result",
                str(observe_result),
                "--head-sha",
                "b" * 40,
                "--workflow-run-id",
                "999",
                "--run-attempt",
                "1",
                "--recorded-at",
                "2026-07-21T00:00:02Z",
                "--output",
                str(output),
            ]
        )

        assert exit_code == 0
        payload = json.loads(output.read_text())
        assert payload["record"]["head_sha"] == "b" * 40
        assert payload["mode"] == "dry_run"
        # OMN-15323: emitted EXPLICITLY, not inherited from the model default —
        # the caller must be able to see which ticket the OCC PR will bind to.
        assert payload["evidence_ticket"] == OCC_OBSERVATION_EVIDENCE_TICKET

    def test_evidence_ticket_is_overridable(self, tmp_path: Path) -> None:
        """The ticket is a real CLI seam, not a constant nobody can reach."""
        observe_result = tmp_path / "occ_attestation_observe_result.json"
        observe_result.write_text(json.dumps({"observation": _OBSERVATION}))
        output = tmp_path / "occ_observation_effect_payload.json"

        exit_code = main(
            [
                "--observe-result",
                str(observe_result),
                "--head-sha",
                "b" * 40,
                "--workflow-run-id",
                "999",
                "--run-attempt",
                "1",
                "--recorded-at",
                "2026-07-21T00:00:02Z",
                "--evidence-ticket",
                "OMN-99999",
                "--output",
                str(output),
            ]
        )

        assert exit_code == 0
        assert json.loads(output.read_text())["evidence_ticket"] == "OMN-99999"

    def test_missing_observation_fails_loud_not_silent(self, tmp_path: Path) -> None:
        observe_result = tmp_path / "occ_attestation_observe_result.json"
        observe_result.write_text(json.dumps({"status": "error"}))
        output = tmp_path / "occ_observation_effect_payload.json"

        exit_code = main(
            [
                "--observe-result",
                str(observe_result),
                "--head-sha",
                "c" * 40,
                "--workflow-run-id",
                "1",
                "--run-attempt",
                "1",
                "--recorded-at",
                "2026-07-21T00:00:03Z",
                "--output",
                str(output),
            ]
        )

        assert exit_code == 1
        assert not output.exists()

    def test_unparseable_input_fails_loud_not_silent(self, tmp_path: Path) -> None:
        observe_result = tmp_path / "occ_attestation_observe_result.json"
        observe_result.write_text("not json at all")
        output = tmp_path / "occ_observation_effect_payload.json"

        exit_code = main(
            [
                "--observe-result",
                str(observe_result),
                "--head-sha",
                "d" * 40,
                "--workflow-run-id",
                "1",
                "--run-attempt",
                "1",
                "--recorded-at",
                "2026-07-21T00:00:04Z",
                "--output",
                str(output),
            ]
        )

        assert exit_code == 1
        assert not output.exists()
