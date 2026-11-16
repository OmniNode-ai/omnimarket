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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml

from omnimarket.models.model_dod_verify_start_command import (
    ModelDodVerifyStartCommand,
)
from omnimarket.nodes.node_dod_verify.handlers.handler_dod_verify import (
    HandlerDodVerify,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumDodVerifyStatus,
    EnumEvidenceCheckStatus,
    ModelDodVerifyState,
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

    @staticmethod
    def _old_form(command: str = "echo ok") -> dict[str, Any]:
        return {
            "schema_version": "1.0.0",
            "ticket_id": BELOW_CUTOVER,
            "title": "An old-form ticket",
            "dod_evidence": standard_contract(command=command)["dod_evidence"],
        }

    @staticmethod
    def _verify(contract: Path) -> ModelDodVerifyState:
        """The verdict HandlerDodVerify produces for the contract, not the collector's list."""
        command = ModelDodVerifyStartCommand(
            correlation_id=uuid4(),
            ticket_id=BELOW_CUTOVER,
            contract_path=str(contract),
            execution_audience="hosted",
            dry_run=True,
            requested_at=datetime.now(tz=UTC),
        )
        state, _event = HandlerDodVerify().run_verification(command)
        return state

    def test_grandfathered_recorded_in_the_verdict_and_checks_still_run(
        self, checkout: Path, cutover: None
    ) -> None:
        path = write_contract(
            checkout,
            yaml.safe_dump(self._old_form(), sort_keys=False),
            ticket_id=BELOW_CUTOVER,
        )
        state = self._verify(path)
        recorded = [c for c in state.checks if "grandfathered" in (c.message or "")]
        assert len(recorded) == 1
        assert BELOW_CUTOVER in (recorded[0].message or "")
        assert "999" not in (recorded[0].message or "").replace(BELOW_CUTOVER, "")
        assert "1000" in (recorded[0].message or "")
        assert "2026-11-20" in (recorded[0].message or "")
        assert recorded[0].is_disposition
        ran = [c for c in state.checks if c.evidence_id == "dod-001"]
        assert [c.status for c in ran] == [EnumEvidenceCheckStatus.VERIFIED]
        # The record is left out of every count: one runnable check, verified.
        assert state.status == EnumDodVerifyStatus.VERIFIED
        assert (state.total_checks, state.verified_count) == (1, 1)

    def test_grandfathered_recorded_never_dropped_by_an_early_return(
        self, checkout: Path, cutover: None
    ) -> None:
        """An item refused before it runs is refused by the schema check, with its cause.

        The audience check returns early, past the point that appends the record, but a
        contract the core model accepts cannot reach it: the model refuses an unknown
        scope or field first, and that refusal is the verdict, so no failed verdict
        exists here without its cause. Positive control: the same contract with a valid
        scope reaches the verdict with the record (the test above).
        """
        old = self._old_form()
        old["dod_evidence"][0]["execution_scope"] = "not_a_scope"
        path = write_contract(
            checkout, yaml.safe_dump(old, sort_keys=False), ticket_id=BELOW_CUTOVER
        )
        state = self._verify(path)
        assert state.status == EnumDodVerifyStatus.FAILED
        assert [c.evidence_id for c in state.checks] == ["contract"]
        assert (state.checks[0].message or "").startswith("contract_schema_invalid")
        assert "execution_scope" in (state.checks[0].message or "")

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

    @staticmethod
    def _run_cli(
        checkout: Path, scoped: Path, full: Path, home: Path
    ) -> subprocess.CompletedProcess[str]:
        """The node's CLI under a scrubbed environment, as a dry run.

        The child sees only the settings the run needs: nothing of the ambient
        environment reaches it (no evidence root, no bus, no tokens), and ``HOME`` is
        a scratch directory, so a run cannot write a receipt or emit an event outside it.
        """
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(home),
            "OMNI_HOME": os.environ["OMNI_HOME"],
            "ONEX_SKILL_OVERLAY_ROOTS": os.environ["ONEX_SKILL_OVERLAY_ROOTS"],
        }
        return subprocess.run(
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
                "--dry-run",
            ],
            capture_output=True,
            text=True,
            check=False,
            cwd=checkout,
            env=env,
        )

    def test_cli_accepts_full_contract_path(
        self, checkout: Path, cutover: None, tmp_path: Path
    ) -> None:
        """The node's CLI carries --full-contract-path through to the verdict."""
        scoped = write_contract(
            checkout, synced_yaml(standard_contract()), name="scoped.yaml"
        )
        full = write_contract(
            checkout, edited_after_sync(standard_contract()), name="full.yaml"
        )
        proc = self._run_cli(checkout, scoped, full, tmp_path / "home")
        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "contract_not_synced" in proc.stdout + proc.stderr
        json_start = proc.stdout.index("{")
        state = json.loads(proc.stdout[json_start : proc.stdout.rindex("}") + 1])
        assert state["status"] == "failed"
        assert state["dry_run"] is True

    def test_cli_exits_zero_for_a_synced_full_contract(
        self, checkout: Path, cutover: None, tmp_path: Path
    ) -> None:
        """Control: the refusal above is the contract's, not the harness's."""
        scoped = write_contract(
            checkout, synced_yaml(standard_contract()), name="scoped.yaml"
        )
        full = write_contract(
            checkout, synced_yaml(standard_contract()), name="full.yaml"
        )
        proc = self._run_cli(checkout, scoped, full, tmp_path / "home")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        json_start = proc.stdout.index("{")
        state = json.loads(proc.stdout[json_start : proc.stdout.rindex("}") + 1])
        assert state["status"] == "verified"

    def test_cli_does_not_inherit_the_ambient_evidence_root(
        self,
        checkout: Path,
        cutover: None,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An evidence root in the test process must not receive the child's receipt."""
        ambient = tmp_path / "ambient-evidence"
        monkeypatch.setenv("ONEX_EVIDENCE_ROOT", str(ambient))
        scoped = write_contract(
            checkout, synced_yaml(standard_contract()), name="scoped.yaml"
        )
        full = write_contract(
            checkout, synced_yaml(standard_contract()), name="full.yaml"
        )
        proc = self._run_cli(checkout, scoped, full, tmp_path / "home")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert not ambient.exists()
