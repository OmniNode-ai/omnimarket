# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23771 hardening: the gaps a review of the standard-ticket rules found.

Each refusal has a positive control that differs from it in one thing, and every
test here names the unsafe state it must not reach: a rule switched off by an
overlay that names no cutover, a scoped file whose checks the full contract does
not hold, a ticket id argument that grandfathers a contract of a newer ticket,
and a schema version that crashes the run.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.model_dod_verify_start_command import (
    ModelDodVerifyStartCommand,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumEvidenceCheckStatus,
    ModelEvidenceCheckResult,
)
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
)
from omnimarket.nodes.node_dod_verify.services.standard_ticket_contract import (
    load_cutover,
)
from tests.unit.nodes.node_dod_verify.omn_23771_standard_ticket import (
    AT_CUTOVER,
    BELOW_CUTOVER,
    REPOSITORY,
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


def _overlay_with(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    overlay = tmp_path / "bad-overlay-root" / "node_dod_verify" / "overlay.yaml"
    overlay.parent.mkdir(parents=True)
    overlay.write_text(text, encoding="utf-8")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(overlay.parent.parent))


def _scoped_yaml(command: str, item_id: str = "dod-001") -> str:
    """A scoped copy: not synced, carrying one evidence item that runs ``command``."""
    data = standard_contract(command=command)
    data["dod_evidence"][0]["id"] = item_id
    return yaml.safe_dump(data, sort_keys=False)


@pytest.mark.unit
class TestScopedItemsEqualTheFullContract:
    """The checks run from the scoped copy, so each scoped item must be a full-contract item."""

    def test_scoped_item_with_a_rewritten_check_is_refused(
        self, checkout: Path, cutover: None
    ) -> None:
        full = write_contract(
            checkout, synced_yaml(standard_contract(command="false")), name="full.yaml"
        )
        scoped = write_contract(checkout, _scoped_yaml("echo ok"), name="scoped.yaml")
        results = _collect(AT_CUTOVER, scoped, full=full)
        assert len(results) == 1
        assert _refused(results, "scoped_item_not_in_full_contract")
        assert "dod-001" in (results[0].message or "")

    def test_scoped_item_the_full_contract_does_not_hold_is_refused(
        self, checkout: Path, cutover: None
    ) -> None:
        full = write_contract(
            checkout, synced_yaml(standard_contract()), name="full.yaml"
        )
        scoped = write_contract(
            checkout, _scoped_yaml("echo ok", item_id="dod-999"), name="scoped.yaml"
        )
        results = _collect(AT_CUTOVER, scoped, full=full)
        assert _refused(results, "scoped_item_not_in_full_contract")
        assert "dod-999" in (results[0].message or "")

    def test_scoped_item_equal_to_the_full_contract_item_runs(
        self, checkout: Path, cutover: None
    ) -> None:
        """Positive control: the same files with the same command are accepted."""
        full = write_contract(
            checkout, synced_yaml(standard_contract()), name="full.yaml"
        )
        scoped = write_contract(checkout, _scoped_yaml("echo ok"), name="scoped.yaml")
        results = _collect(AT_CUTOVER, scoped, full=full)
        assert [r.status for r in results] == [EnumEvidenceCheckStatus.VERIFIED]
        assert [r.evidence_id for r in results] == ["dod-001"]

    def test_a_grandfathered_scoped_item_is_held_to_the_full_contract_too(
        self, checkout: Path, cutover: None
    ) -> None:
        old_full = {
            "schema_version": "1.0.0",
            "ticket_id": BELOW_CUTOVER,
            "title": "An old-form ticket",
            "dod_evidence": standard_contract(command="false")["dod_evidence"],
        }
        full = write_contract(
            checkout,
            yaml.safe_dump(old_full, sort_keys=False),
            ticket_id=BELOW_CUTOVER,
            name="full.yaml",
        )
        scoped_data = {**old_full, "dod_evidence": standard_contract()["dod_evidence"]}
        scoped = write_contract(
            checkout,
            yaml.safe_dump(scoped_data, sort_keys=False),
            ticket_id=BELOW_CUTOVER,
            name="scoped.yaml",
        )
        results = _collect(BELOW_CUTOVER, scoped, full=full)
        assert _refused(results, "scoped_item_not_in_full_contract")


@pytest.mark.unit
class TestTicketIdArgumentGrandfathersNothing:
    """``--ticket-id`` must not move a newer ticket's contract below the cutover."""

    def test_an_old_ticket_id_over_a_newer_contract_is_refused(
        self, checkout: Path, cutover: None
    ) -> None:
        # ``contract_structure_failure`` compares the contract's own ticket_id with the
        # argument, so the argument alone cannot grandfather this contract.
        old_form = {
            "schema_version": "1.0.0",
            "ticket_id": AT_CUTOVER,
            "title": "An old-form ticket at the cutover",
            "dod_evidence": standard_contract()["dod_evidence"],
        }
        path = write_contract(checkout, yaml.safe_dump(old_form, sort_keys=False))
        results = _collect("OMN-1", path)
        assert [r.status for r in results] == [EnumEvidenceCheckStatus.FAILED]
        assert "found 'OMN-1000'" in (results[0].message or "")
        assert not any("grandfathered" in (r.message or "") for r in results)
        # Positive control: the contract's own id reaches the cutover rule.
        assert _refused(_collect(AT_CUTOVER, path), "ticket_not_standard")

    def test_an_old_ticket_id_with_a_full_contract_of_a_newer_ticket_is_refused(
        self, checkout: Path, cutover: None
    ) -> None:
        full = write_contract(
            checkout, synced_yaml(standard_contract()), name="full.yaml"
        )
        scoped_data = standard_contract("OMN-1")
        scoped = write_contract(
            checkout,
            yaml.safe_dump(scoped_data, sort_keys=False),
            ticket_id="OMN-1",
            name="scoped.yaml",
        )
        results = _collect("OMN-1", scoped, full=full)
        assert [r.status for r in results] == [EnumEvidenceCheckStatus.FAILED]
        assert _refused(results, "contract_schema_invalid")
        assert "'OMN-1000'" in (results[0].message or "")
        assert not any("grandfathered" in (r.message or "") for r in results)


@pytest.mark.unit
class TestAnOverlayThatNamesNoCutoverIsRefused:
    """An overlay that is present but names no cutover must not read as no overlay."""

    def test_no_overlay_reads_as_no_cutover(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Positive control: the only state that switches the rules off."""
        monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "no-such-root"))
        assert load_cutover() is None

    def test_an_overlay_naming_both_values_reads_as_a_cutover(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "ONEX_SKILL_OVERLAY_ROOTS", str(write_overlay(tmp_path / "root"))
        )
        loaded = load_cutover()
        assert loaded is not None
        assert loaded.first_ticket_number == 1000

    def test_an_empty_mapping_overlay_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _overlay_with(tmp_path, monkeypatch, "{}\n")
        with pytest.raises(NodeOverlayError, match="names neither cutover value"):
            load_cutover()

    def test_an_empty_mapping_overlay_refuses_the_run_not_passes_it(
        self, tmp_path: Path, checkout: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _overlay_with(tmp_path, monkeypatch, "{}\n")
        # The contract earns ticket_not_standard when a cutover applies.
        old_form = {
            "schema_version": "1.0.0",
            "ticket_id": AT_CUTOVER,
            "title": "An old-form ticket at the cutover",
            "dod_evidence": standard_contract()["dod_evidence"],
        }
        path = write_contract(checkout, yaml.safe_dump(old_form, sort_keys=False))
        results = _collect(AT_CUTOVER, path)
        assert [r.status for r in results] == [EnumEvidenceCheckStatus.FAILED]
        assert _refused(results, "standard_ticket_cutover_unreadable")

    def test_an_overlay_with_misspelt_keys_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _overlay_with(
            tmp_path,
            monkeypatch,
            'standard_ticket_cutover_dat: "2026-11-20"\n'
            'standard_ticket_first_ticket_numbr: "1000"\n',
        )
        with pytest.raises(NodeOverlayError, match="unknown fields"):
            load_cutover()

    def test_an_overlay_naming_one_value_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _overlay_with(
            tmp_path, monkeypatch, 'standard_ticket_cutover_date: "2026-11-20"\n'
        )
        with pytest.raises(NodeOverlayError, match="must name both"):
            load_cutover()

    @pytest.mark.parametrize(
        ("date_text", "number_text"),
        [("2026-11-20", "ten"), ("not-a-date", "1000"), ("2026-11-20", "0")],
    )
    def test_cutover_values_that_do_not_parse_are_refused(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        date_text: str,
        number_text: str,
    ) -> None:
        _overlay_with(
            tmp_path,
            monkeypatch,
            f'standard_ticket_cutover_date: "{date_text}"\n'
            f'standard_ticket_first_ticket_number: "{number_text}"\n',
        )
        with pytest.raises(NodeOverlayError, match="do not parse"):
            load_cutover()


@pytest.mark.unit
class TestSchemaVersionThatIsNotANumber:
    def test_a_prerelease_schema_version_is_refused_by_core_not_a_crash(
        self, checkout: Path, cutover: None
    ) -> None:
        """Core constrains the form, so ``1.1.0-rc1`` never reaches the version parse."""
        data = standard_contract()
        data["schema_version"] = "1.1.0-rc1"
        path = write_contract(checkout, yaml.safe_dump(data, sort_keys=False))
        results = _collect(AT_CUTOVER, path)
        assert [r.status for r in results] == [EnumEvidenceCheckStatus.FAILED]
        assert _refused(results, "contract_schema_invalid")
        assert "schema_version" in (results[0].message or "")


@pytest.mark.unit
class TestFullContractPathNeedsAContractPath:
    """The command refuses the combinations the collector does not read."""

    def test_full_contract_path_without_contract_path_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="full_contract_path requires"):
            ModelDodVerifyStartCommand(
                correlation_id=uuid4(),
                ticket_id=AT_CUTOVER,
                full_contract_path="contracts/full.yaml",
                requested_at=datetime.now(tz=UTC),
            )

    def test_full_contract_path_on_a_goal_run_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="cannot set full_contract_path"):
            ModelDodVerifyStartCommand(
                correlation_id=uuid4(),
                ticket_id=AT_CUTOVER,
                goal_id=uuid4(),
                full_contract_path="contracts/full.yaml",
                requested_at=datetime.now(tz=UTC),
            )

    def test_both_paths_together_are_accepted(self) -> None:
        """Positive control for the two refusals above."""
        command = ModelDodVerifyStartCommand(
            correlation_id=uuid4(),
            ticket_id=AT_CUTOVER,
            contract_path="contracts/scoped.yaml",
            full_contract_path="contracts/full.yaml",
            requested_at=datetime.now(tz=UTC),
        )
        assert command.full_contract_path == "contracts/full.yaml"


PLANTED_HEAD = (
    Path(__file__).resolve().parents[3] / "fixtures" / "dod_verify_planted_head"
)


@pytest.mark.unit
class TestPlantedHeadFixtureRefusedByTheNodeCli:
    """The committed planted head, run through the node's entry point as the lab run does."""

    @staticmethod
    def _run(contract: Path, tmp_path: Path) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(tmp_path),
            "ONEX_SKILL_OVERLAY_ROOTS": str(PLANTED_HEAD / "overlay"),
        }
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "omnimarket.nodes.node_dod_verify",
                "--ticket-id",
                AT_CUTOVER,
                "--contract-path",
                str(contract),
                "--full-contract-path",
                str(contract),
                "--execution-audience",
                "hosted",
                "--dry-run",
            ],
            capture_output=True,
            text=True,
            check=False,
            cwd=PLANTED_HEAD,
            env=env,
        )

    def test_planted_head_cli_refuses_contract_not_synced(self, tmp_path: Path) -> None:
        proc = self._run(PLANTED_HEAD / "planted_head.yaml", tmp_path)
        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "contract_not_synced" in proc.stdout + proc.stderr

    def test_planted_head_cli_control_accepts_the_synced_contract(
        self, tmp_path: Path
    ) -> None:
        """Control: the same node and overlay accept the same contract once it is synced."""
        synced = tmp_path / REPOSITORY / "contracts" / f"{AT_CUTOVER}.yaml"
        synced.parent.mkdir(parents=True)
        synced.write_text(synced_yaml(standard_contract()), encoding="utf-8")
        proc = self._run(synced, tmp_path)
        assert proc.returncode == 0, proc.stdout + proc.stderr
