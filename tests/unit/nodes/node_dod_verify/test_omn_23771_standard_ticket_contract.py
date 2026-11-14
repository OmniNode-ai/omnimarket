# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23771: dod-verify reads the standard ticket contract from the pull request.

Each refusal is reproduced against the collector with no network read, and each
refusal test has a positive control: the same contract, unchanged, passes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumEvidenceCheckStatus,
    ModelEvidenceCheckResult,
)
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
)
from tests.unit.nodes.node_dod_verify.omn_23771_standard_ticket import (
    AT_CUTOVER,
    BELOW_CUTOVER,
    REPOSITORY,
    edited_after_sync,
    standard_contract,
    synced_yaml,
    write_contract,
    write_overlay,
)


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A checkout named for its repository."""
    root = tmp_path / REPOSITORY
    root.mkdir()
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    monkeypatch.delenv("REPO", raising=False)
    return root


@pytest.fixture
def cutover(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = write_overlay(tmp_path / "overlay-root")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root))


def _collect(
    ticket_id: str, contract: Path, full: Path | None = None
) -> list[ModelEvidenceCheckResult]:
    return EvidenceCollector().collect(
        ticket_id,
        contract_path=str(contract),
        full_contract_path=str(full) if full is not None else None,
    )


def _refused(results: list[ModelEvidenceCheckResult], code: str) -> bool:
    return any(
        r.status == EnumEvidenceCheckStatus.FAILED
        and (r.message or "").startswith(code)
        for r in results
    )


@pytest.mark.unit
class TestStandardTicketContract:
    def test_synced_standard_contract_is_accepted(
        self, checkout: Path, cutover: None
    ) -> None:
        """Positive control: the contract every refusal below is a variation of."""
        path = write_contract(checkout, synced_yaml(standard_contract()))
        results = _collect(AT_CUTOVER, path)
        assert [r.status for r in results] == [EnumEvidenceCheckStatus.VERIFIED]

    def test_contract_not_synced_names_both_fingerprints(
        self, checkout: Path, cutover: None
    ) -> None:
        synced = write_contract(checkout, synced_yaml(standard_contract()))
        stored = next(
            line.split(": ", 1)[1].strip()
            for line in synced.read_text(encoding="utf-8").splitlines()
            if line.startswith("contract_fingerprint:")
        )
        edited = write_contract(
            checkout, edited_after_sync(standard_contract()), name="edited.yaml"
        )
        results = _collect(AT_CUTOVER, edited)
        assert len(results) == 1
        assert _refused(results, "contract_not_synced")
        message = results[0].message or ""
        assert stored in message
        assert "onex ticket sync" in message
        recomputed = message.split("content hashes to ", 1)[1].split(";", 1)[0]
        assert recomputed != stored

    def test_contract_not_synced_when_fingerprint_missing(
        self, checkout: Path, cutover: None
    ) -> None:
        data = yaml.safe_load(synced_yaml(standard_contract()))
        data.pop("contract_fingerprint")
        path = write_contract(checkout, yaml.safe_dump(data, sort_keys=False))
        results = _collect(AT_CUTOVER, path)
        assert _refused(results, "contract_not_synced")
        assert "missing" in (results[0].message or "")

    def test_ticket_not_standard_for_a_1_0_0_contract_at_the_cutover(
        self, checkout: Path, cutover: None
    ) -> None:
        old = {
            "schema_version": "1.0.0",
            "ticket_id": AT_CUTOVER,
            "title": "An old-form ticket",
            "dod_evidence": standard_contract()["dod_evidence"],
        }
        path = write_contract(checkout, yaml.safe_dump(old, sort_keys=False))
        results = _collect(AT_CUTOVER, path)
        assert len(results) == 1
        assert _refused(results, "ticket_not_standard")
        # The same contract one number below the cutover is not refused.
        below = {**old, "ticket_id": BELOW_CUTOVER}
        below_path = write_contract(
            checkout,
            yaml.safe_dump(below, sort_keys=False),
            ticket_id=BELOW_CUTOVER,
        )
        assert not _refused(_collect(BELOW_CUTOVER, below_path), "ticket_not_standard")

    def test_grandfathered_recorded_and_checks_still_run(
        self, checkout: Path, cutover: None
    ) -> None:
        old = {
            "schema_version": "1.0.0",
            "ticket_id": BELOW_CUTOVER,
            "title": "An old-form ticket",
            "dod_evidence": standard_contract()["dod_evidence"],
        }
        path = write_contract(
            checkout, yaml.safe_dump(old, sort_keys=False), ticket_id=BELOW_CUTOVER
        )
        results = _collect(BELOW_CUTOVER, path)
        recorded = [r for r in results if r.message and "grandfathered" in r.message]
        assert len(recorded) == 1
        assert BELOW_CUTOVER in (recorded[0].message or "")
        assert "999" not in (recorded[0].message or "").replace(BELOW_CUTOVER, "")
        assert "1000" in (recorded[0].message or "")
        assert "2026-11-20" in (recorded[0].message or "")
        assert recorded[0].is_disposition
        ran = [r for r in results if r.evidence_id == "dod-001"]
        assert [r.status for r in ran] == [EnumEvidenceCheckStatus.VERIFIED]

    def test_grandfathered_recorded_leaves_the_verdict_counts_alone(
        self, checkout: Path, cutover: None
    ) -> None:
        """The record never turns a run that verified nothing into a verified one."""
        old = {
            "schema_version": "1.0.0",
            "ticket_id": BELOW_CUTOVER,
            "title": "An old-form ticket",
            "dod_evidence": standard_contract(command="false")["dod_evidence"],
        }
        path = write_contract(
            checkout, yaml.safe_dump(old, sort_keys=False), ticket_id=BELOW_CUTOVER
        )
        results = _collect(BELOW_CUTOVER, path)
        counted = [r for r in results if not r.is_disposition]
        assert [r.status for r in counted] == [EnumEvidenceCheckStatus.FAILED]

    def test_second_repository_refused(self, checkout: Path, cutover: None) -> None:
        other = standard_contract()
        other["repository"] = "omnibase_core"
        path = write_contract(checkout, synced_yaml(other))
        results = _collect(AT_CUTOVER, path)
        assert len(results) == 1
        assert _refused(results, "contract_in_second_repository")
        assert "omnibase_core" in (results[0].message or "")
        assert REPOSITORY in (results[0].message or "")

    def test_second_repository_refused_names_a_qualified_repository(
        self, checkout: Path, cutover: None
    ) -> None:
        """Positive control: ``OmniNode-ai/omnimarket`` names this repository."""
        own = standard_contract()
        own["repository"] = f"OmniNode-ai/{REPOSITORY}"
        path = write_contract(checkout, synced_yaml(own))
        assert not _refused(_collect(AT_CUTOVER, path), "contract_in_second_repository")

    def test_no_cutover_applies_no_standard_ticket_rule(self, checkout: Path) -> None:
        """With no overlay, the contract that earns refusals above is judged as before."""
        path = write_contract(checkout, edited_after_sync(standard_contract()))
        results = _collect(AT_CUTOVER, path)
        assert [r.status for r in results] == [EnumEvidenceCheckStatus.VERIFIED]

    def test_full_contract_path_must_name_the_same_ticket(
        self, checkout: Path, cutover: None
    ) -> None:
        scoped = write_contract(checkout, synced_yaml(standard_contract()))
        other = write_contract(
            checkout,
            synced_yaml(standard_contract("OMN-1001")),
            ticket_id="OMN-1001",
        )
        results = _collect(AT_CUTOVER, scoped, full=other)
        assert _refused(results, "contract_schema_invalid")
        assert "OMN-1001" in (results[0].message or "")

    def test_full_contract_path_not_found_refused(
        self, checkout: Path, cutover: None
    ) -> None:
        scoped = write_contract(checkout, synced_yaml(standard_contract()))
        results = _collect(AT_CUTOVER, scoped, full=checkout / "contracts" / "no.yaml")
        assert _refused(results, "contract_schema_invalid")

    def test_cli_accepts_full_contract_path(
        self, checkout: Path, cutover: None
    ) -> None:
        """The node's CLI carries --full-contract-path through to the verdict."""
        scoped = write_contract(checkout, synced_yaml(standard_contract()))
        full = write_contract(
            checkout, edited_after_sync(standard_contract()), name="full.yaml"
        )
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "omnimarket.nodes.node_dod_verify",
                "--ticket-id",
                AT_CUTOVER,
                "--contract-path",
                str(scoped),
                "--full-contract-path",
                str(full),
                "--execution-audience",
                "hosted",
            ],
            capture_output=True,
            text=True,
            check=False,
            cwd=checkout,
            env=os.environ.copy(),
        )
        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "contract_not_synced" in proc.stdout + proc.stderr
        json_start = proc.stdout.index("{")
        state = json.loads(proc.stdout[json_start : proc.stdout.rindex("}") + 1])
        assert state["status"] == "failed"
