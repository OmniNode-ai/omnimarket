# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23774: dod-verify refuses a runtime-affecting change whose contract has no integrated criterion.

Each refusal has a positive control: the same contract, unchanged, passes when the
change does not reach runtime code or when the contract carries an integrated criterion.
"""

from __future__ import annotations

import copy
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.models.model_dod_verify_start_command import (
    ModelDodVerifyStartCommand,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumEvidenceCheckStatus,
    ModelEvidenceCheckResult,
)
from omnimarket.nodes.node_dod_verify.models.model_standard_ticket_cutover import (
    ModelStandardTicketCutover,
)
from omnimarket.nodes.node_dod_verify.services.cli_parser import (
    build_verify_parser,
    read_changed_paths,
)
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
)
from omnimarket.nodes.node_dod_verify.services.standard_ticket_contract import (
    INTEGRATION_ITEM_MISSING,
    standard_ticket_refusals,
)
from tests.unit.nodes.node_dod_verify.omn_23771_standard_ticket import (
    AT_CUTOVER,
    REPOSITORY,
    standard_contract,
    synced_yaml,
    write_contract,
    write_overlay,
)

RUNTIME_PATH = "src/omnimarket/nodes/node_dod_verify/services/evidence_collector.py"
DOCS_PATH = "docs/notes.md"
TEST_PATH = "tests/unit/nodes/node_dod_verify/test_evidence_collector.py"
CUTOVER = ModelStandardTicketCutover(
    cutover_date=date(2026, 11, 20), first_ticket_number=1000
)


def _integrated_contract() -> dict[str, Any]:
    """The code-only standard contract with an integrated criterion bound to a behavior_proven check."""
    data = copy.deepcopy(standard_contract())
    data["requirements"][0]["acceptance"].append(
        {
            "id": "AC2",
            "statement": "observed where it runs",
            "falsifier": "the lab receipt is absent",
            "integrated": True,
        }
    )
    data["dod_evidence"].append(
        {
            "id": "dod-002",
            "description": "observed where it runs",
            "binds_ac": ["AC2"],
            "checks": [{"check_type": "behavior_proven", "check_value": "true"}],
        }
    )
    return data


def _synced(data: dict[str, Any]) -> dict[str, Any]:
    """The contract as the ticket node writes it, so only the rule under test can refuse."""
    loaded = yaml.safe_load(synced_yaml(data))
    assert isinstance(loaded, dict)
    return loaded


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / REPOSITORY
    root.mkdir()
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    monkeypatch.delenv("REPO", raising=False)
    overlay = write_overlay(tmp_path / "overlay-root")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(overlay))
    return root


def _collect(
    contract: Path, changed_paths: list[str] | None
) -> list[ModelEvidenceCheckResult]:
    return EvidenceCollector().collect(
        AT_CUTOVER, contract_path=str(contract), changed_paths=changed_paths
    )


def _refused(results: list[ModelEvidenceCheckResult]) -> bool:
    return any(
        r.status == EnumEvidenceCheckStatus.FAILED
        and (r.message or "").startswith(INTEGRATION_ITEM_MISSING)
        for r in results
    )


@pytest.mark.unit
class TestIntegrationItemMissing:
    def test_runtime_change_with_code_only_contract_is_refused_integration_item_missing(
        self, checkout: Path
    ) -> None:
        path = write_contract(checkout, synced_yaml(standard_contract()))
        results = _collect(path, [DOCS_PATH, RUNTIME_PATH])
        assert len(results) == 1
        assert _refused(results)
        message = results[0].message or ""
        assert RUNTIME_PATH in message
        assert DOCS_PATH not in message
        assert "proof_class code-only" in message

    def test_integration_item_missing_control_docs_and_tests_only_change_passes(
        self, checkout: Path
    ) -> None:
        """Positive control: the same contract passes when the change is not runtime code."""
        path = write_contract(checkout, synced_yaml(standard_contract()))
        results = _collect(path, [DOCS_PATH, TEST_PATH])
        assert not _refused(results)
        assert [r.status for r in results] == [EnumEvidenceCheckStatus.VERIFIED]

    def test_integration_item_missing_control_no_paths_given_does_not_run(
        self, checkout: Path
    ) -> None:
        """Positive control: with no changed paths the rule does not run."""
        path = write_contract(checkout, synced_yaml(standard_contract()))
        results = _collect(path, None)
        assert not _refused(results)

    def test_integration_item_missing_control_integrated_criterion_passes(self) -> None:
        """Positive control: the same runtime change with an integrated criterion bound."""
        refusals = standard_ticket_refusals(
            AT_CUTOVER,
            _synced(_integrated_contract()),
            CUTOVER,
            REPOSITORY,
            [RUNTIME_PATH],
        )
        assert refusals == []

    def test_integration_item_missing_when_integrated_criterion_has_no_integration_check(
        self,
    ) -> None:
        """An integrated criterion bound only to a command check does not observe."""
        data = _integrated_contract()
        data["dod_evidence"][1]["checks"] = [
            {"check_type": "command", "check_value": "echo ok"}
        ]
        refusals = standard_ticket_refusals(
            AT_CUTOVER, _synced(data), CUTOVER, REPOSITORY, [RUNTIME_PATH]
        )
        assert [r.message.split(":", 1)[0] for r in refusals if r.message] == [
            INTEGRATION_ITEM_MISSING
        ]

    def test_integration_item_missing_not_applied_below_cutover(self) -> None:
        refusals = standard_ticket_refusals(
            "OMN-999",
            _synced(standard_contract("OMN-999")),
            CUTOVER,
            REPOSITORY,
            [RUNTIME_PATH],
        )
        assert refusals == []


@pytest.mark.unit
class TestChangedPathsInput:
    def test_cli_reads_changed_paths_file_one_per_line(self, tmp_path: Path) -> None:
        listing = tmp_path / "changed.txt"
        listing.write_text(f"{RUNTIME_PATH}\n\n  {DOCS_PATH}  \n", encoding="utf-8")
        args = build_verify_parser().parse_args(
            [
                "--ticket-id",
                AT_CUTOVER,
                "--execution-audience",
                "hosted",
                "--changed-paths-file",
                str(listing),
            ]
        )
        assert read_changed_paths(args.changed_paths_file) == [RUNTIME_PATH, DOCS_PATH]
        assert read_changed_paths(None) is None

    def test_cli_unreadable_changed_paths_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            read_changed_paths(tmp_path / "absent.txt")

    def test_command_changed_paths_needs_contract_path(self) -> None:
        with pytest.raises(ValueError, match="changed_paths requires contract_path"):
            ModelDodVerifyStartCommand.model_validate(
                {
                    "correlation_id": "00000000-0000-0000-0000-000000000001",
                    "ticket_id": AT_CUTOVER,
                    "changed_paths": [RUNTIME_PATH],
                    "execution_audience": "hosted",
                    "requested_at": "2026-11-14T00:00:00Z",
                }
            )
