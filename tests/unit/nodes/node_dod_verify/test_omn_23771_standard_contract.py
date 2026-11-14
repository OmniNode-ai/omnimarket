# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-23771: dod-verify judges the contract as the standard ticket form.

AC3 to AC6 of the ticket. AC1 and AC2 live in test_evidence_collector.py beside
the collector tests they name.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumDodVerifyStatus,
    EnumEvidenceCheckStatus,
)
from omnimarket.nodes.node_dod_verify.services import evidence_collector as ec_mod
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
)
from tests.unit.nodes.node_dod_verify.omn_23771_standard_contract_world import (
    legacy_contract,
    standard_contract,
    subject_for,
    write_contract,
)

CUTOVER_ENV = "DOD_VERIFY_STANDARD_TICKET_CUTOVER"


@pytest.fixture(autouse=True)
def _no_ambient_cutover(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CUTOVER_ENV, raising=False)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)


@pytest.mark.unit
class TestContractNotSynced:
    def test_contract_not_synced_names_both_fingerprints(self, tmp_path: Path) -> None:
        data = standard_contract("OMN-90001")
        stored = data["contract_fingerprint"]
        data["title"] = "edited by hand after the last sync"
        path = write_contract(tmp_path / "OMN-90001.yaml", data)

        results = EvidenceCollector().collect(
            "OMN-90001", contract_path=str(path), execution_audience="hosted"
        )

        assert len(results) == 1
        refusal = results[0]
        assert refusal.status == EnumEvidenceCheckStatus.FAILED
        assert refusal.message.startswith("contract_not_synced")
        assert stored in refusal.message
        # the recomputed one: a 16-hex fingerprint different from the stored one
        recomputed = [
            word.strip(".,:;()'\"")
            for word in refusal.message.split()
            if len(word.strip(".,:;()'\"")) == 16 and word.strip(".,:;()'\"") != stored
        ]
        assert recomputed, refusal.message

    def test_contract_not_synced_missing_fingerprint_is_refused(
        self, tmp_path: Path
    ) -> None:
        data = standard_contract("OMN-90001")
        del data["contract_fingerprint"]
        path = write_contract(tmp_path / "OMN-90001.yaml", data)

        results = EvidenceCollector().collect(
            "OMN-90001", contract_path=str(path), execution_audience="hosted"
        )

        assert results[0].message.startswith("contract_not_synced")

    def test_synced_contract_runs_its_checks(self, tmp_path: Path) -> None:
        path = write_contract(tmp_path / "OMN-90001.yaml", standard_contract())

        results = EvidenceCollector().collect(
            "OMN-90001", contract_path=str(path), execution_audience="hosted"
        )

        assert [r.evidence_id for r in results][:1] == ["dod-001"]
        assert results[0].status == EnumEvidenceCheckStatus.VERIFIED


@pytest.mark.unit
class TestTicketNotStandard:
    def test_ticket_not_standard_at_cutover(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(CUTOVER_ENV, "90001")
        path = write_contract(tmp_path / "OMN-90001.yaml", legacy_contract("OMN-90001"))

        results = EvidenceCollector().collect(
            "OMN-90001", contract_path=str(path), execution_audience="hosted"
        )

        assert len(results) == 1
        assert results[0].status == EnumEvidenceCheckStatus.FAILED
        assert results[0].message.startswith("ticket_not_standard")

    def test_ticket_not_standard_above_cutover(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(CUTOVER_ENV, "90000")
        path = write_contract(tmp_path / "OMN-90005.yaml", legacy_contract("OMN-90005"))

        results = EvidenceCollector().collect(
            "OMN-90005", contract_path=str(path), execution_audience="hosted"
        )

        assert results[0].message.startswith("ticket_not_standard")

    def test_malformed_cutover_is_refused_not_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(CUTOVER_ENV, "soon")
        path = write_contract(tmp_path / "OMN-90001.yaml", legacy_contract("OMN-90001"))

        results = EvidenceCollector().collect(
            "OMN-90001", contract_path=str(path), execution_audience="hosted"
        )

        assert results[0].status == EnumEvidenceCheckStatus.FAILED
        assert CUTOVER_ENV in results[0].message


@pytest.mark.unit
class TestGrandfathered:
    def test_grandfathered_recorded_and_checks_still_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(CUTOVER_ENV, "90000")
        path = write_contract(tmp_path / "OMN-80001.yaml", legacy_contract("OMN-80001"))
        collector = EvidenceCollector()

        results = collector.collect(
            "OMN-80001", contract_path=str(path), execution_audience="hosted"
        )

        assert collector.contract_standing == "grandfathered"
        assert results[0].evidence_id == "dod-001"
        assert results[0].status == EnumEvidenceCheckStatus.VERIFIED

    def test_grandfathered_recorded_in_the_verdict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from omnimarket.models.model_dod_verify_start_command import (
            ModelDodVerifyStartCommand,
        )
        from omnimarket.nodes.node_dod_verify.handlers.handler_dod_verify import (
            HandlerDodVerify,
        )

        monkeypatch.setenv(CUTOVER_ENV, "90000")
        path = write_contract(tmp_path / "OMN-80001.yaml", legacy_contract("OMN-80001"))

        state, _ = HandlerDodVerify().run_verification(
            ModelDodVerifyStartCommand(
                ticket_id="OMN-80001",
                contract_path=str(path),
                execution_audience="hosted",
            )
        )

        assert state.contract_standing == "grandfathered"
        assert state.status == EnumDodVerifyStatus.VERIFIED
        assert state.verified_count == 1
        assert '"contract_standing":"grandfathered"' in state.model_dump_json()

    def test_standard_contract_carries_no_grandfathered_record(
        self, tmp_path: Path
    ) -> None:
        path = write_contract(tmp_path / "OMN-90001.yaml", standard_contract())
        collector = EvidenceCollector()

        collector.collect(
            "OMN-90001", contract_path=str(path), execution_audience="hosted"
        )

        assert collector.contract_standing is None


@pytest.mark.unit
class TestSecondRepository:
    def test_second_repository_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(CUTOVER_ENV, "90000")
        monkeypatch.setattr(
            ec_mod,
            "resolve_contract_subject",
            lambda _path: subject_for("OmniNode-ai/omnimarket"),
        )
        path = write_contract(
            tmp_path / "OMN-90001.yaml",
            standard_contract("OMN-90001", repository="omnibase_infra"),
        )

        results = EvidenceCollector().collect(
            "OMN-90001", contract_path=str(path), execution_audience="hosted"
        )

        assert len(results) == 1
        assert results[0].status == EnumEvidenceCheckStatus.FAILED
        assert results[0].message.startswith("contract_in_second_repository")
        assert "omnibase_infra" in results[0].message

    def test_own_repository_is_not_a_second_repository(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(CUTOVER_ENV, "90000")
        monkeypatch.setattr(
            ec_mod,
            "resolve_contract_subject",
            lambda _path: subject_for("OmniNode-ai/omnimarket"),
        )
        path = write_contract(tmp_path / "OMN-90001.yaml", standard_contract())

        results = EvidenceCollector().collect(
            "OMN-90001", contract_path=str(path), execution_audience="hosted"
        )

        assert results[0].evidence_id == "dod-001"
        assert results[0].status == EnumEvidenceCheckStatus.VERIFIED

    def test_second_repository_below_cutover_is_not_judged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(CUTOVER_ENV, "99999")
        monkeypatch.setattr(
            ec_mod,
            "resolve_contract_subject",
            lambda _path: subject_for("OmniNode-ai/omnimarket"),
        )
        path = write_contract(
            tmp_path / "OMN-90001.yaml",
            standard_contract("OMN-90001", repository="omnibase_infra"),
        )

        results = EvidenceCollector().collect(
            "OMN-90001", contract_path=str(path), execution_audience="hosted"
        )

        assert results[0].evidence_id == "dod-001"


@pytest.mark.unit
class TestNodeCliPlantedHead:
    """AC7's command, run as ``python -m`` on a planted head."""

    def test_cli_refuses_a_planted_head_contract_not_synced(
        self, tmp_path: Path
    ) -> None:
        import subprocess
        import sys

        data = standard_contract("OMN-90001")
        planted = {**data, "title": "changed on the head without the ticket node"}
        full_path = write_contract(tmp_path / "full" / "OMN-90001.yaml", planted)
        # The workflow's scoped copy still carries the old, matching text.
        scoped_path = write_contract(tmp_path / "scoped" / "OMN-90001.yaml", data)

        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "omnimarket.nodes.node_dod_verify",
                "--ticket-id",
                "OMN-90001",
                "--contract-path",
                str(scoped_path),
                "--full-contract-path",
                str(full_path),
                "--execution-audience",
                "hosted",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=240,
            cwd=tmp_path,
        )

        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "contract_not_synced" in proc.stdout
        assert data["contract_fingerprint"] in proc.stdout

    def test_cli_passes_the_synced_head_control(self, tmp_path: Path) -> None:
        import subprocess
        import sys

        data = standard_contract("OMN-90001")
        full_path = write_contract(tmp_path / "full" / "OMN-90001.yaml", data)

        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "omnimarket.nodes.node_dod_verify",
                "--ticket-id",
                "OMN-90001",
                "--contract-path",
                str(full_path),
                "--full-contract-path",
                str(full_path),
                "--execution-audience",
                "hosted",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=240,
            cwd=tmp_path,
        )

        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "contract_not_synced" not in proc.stdout
