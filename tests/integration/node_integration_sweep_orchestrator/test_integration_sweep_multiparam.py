# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Multi-parameter integration proof for node_integration_sweep_orchestrator.

OMN-13680, WS5 Wave 6. The handler exposes a synchronous in-process ``handle()``
that writes a deterministic drift/integration artifact and runs runtime-SHA
verification over per-ticket contract files — there is no bus round-trip surface,
so this is a Variant A direct call.

The I/O boundary is mocked by injecting a ``HandlerRuntimeShaVerify`` subclass
that overrides only the SSH probe method (``_probe_deployed_sha``) to return a
deterministic SHA — the real PASS/FAIL receipt logic still runs, and no
subprocess is spawned. ``run_surface_probes=False`` keeps the live KAFKA/DB/CI
probes off. Contract fixtures (the repo snapshot) are written under ``tmp_path``.

The stale-receipt case is the negative control: a contract whose merge SHA does
not match the deployed SHA must drive ``status=blocked``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from omnibase_core.validation.runtime_sha_match import CHECK_TYPE_RUNTIME_SHA_MATCH

from omnimarket.nodes.node_dod_verify.handlers.handler_runtime_sha_verify import (
    HandlerRuntimeShaVerify,
    ModelRuntimeShaVerifyRequest,
)
from omnimarket.nodes.node_integration_sweep_orchestrator.handlers.handler_integration_sweep_orchestrator import (
    HandlerIntegrationSweepOrchestrator,
)
from omnimarket.nodes.node_integration_sweep_orchestrator.models.model_integration_sweep_orchestrator_request import (
    ModelIntegrationSweepOrchestratorRequest,
)

_DEPLOYED_SHA = "abc1234def5678abc1234def5678abc1234def56"


class _StubShaProbe(HandlerRuntimeShaVerify):
    """Overrides only the SSH probe so the real receipt logic runs offline."""

    def _probe_deployed_sha(self, request: ModelRuntimeShaVerifyRequest) -> str:
        return _DEPLOYED_SHA


def _write_contract(contracts_dir: Path, ticket_id: str, merge_sha: str) -> None:
    contracts_dir.mkdir(parents=True, exist_ok=True)
    contract = {
        "name": ticket_id,
        "dod_evidence": [
            {
                "id": "ev-1",
                "checks": [
                    {
                        "check_type": CHECK_TYPE_RUNTIME_SHA_MATCH,
                        "check_value": merge_sha,
                    }
                ],
            }
        ],
    }
    (contracts_dir / f"{ticket_id}.yaml").write_text(
        yaml.safe_dump(contract), encoding="utf-8"
    )


def _request(
    tmp_path: Path, **overrides: object
) -> ModelIntegrationSweepOrchestratorRequest:
    base: dict[str, object] = {
        "artifact_root": str(tmp_path / "cc"),
        "contracts_dir": str(tmp_path / "cc" / "contracts"),
        "receipts_dir": str(tmp_path / "cc" / "receipts"),
        "run_surface_probes": False,
        "artifact_date": "2026-06-27",
    }
    base.update(overrides)
    return ModelIntegrationSweepOrchestratorRequest(**base)  # type: ignore[arg-type]


@pytest.mark.integration
def test_integration_sweep_dry_run_writes_nothing(tmp_path: Path) -> None:
    handler = HandlerIntegrationSweepOrchestrator(runtime_sha_handler=_StubShaProbe())
    result = handler.handle(_request(tmp_path, dry_run=True, tickets=["OMN-1"]))
    assert result.status == "recorded"
    assert result.artifact_written is False
    assert not Path(result.artifact_path).exists()
    assert result.details["runtime_sha_checks"] == "0"


@pytest.mark.integration
def test_integration_sweep_no_tickets_records_artifact(tmp_path: Path) -> None:
    handler = HandlerIntegrationSweepOrchestrator(runtime_sha_handler=_StubShaProbe())
    result = handler.handle(_request(tmp_path, tickets=[]))
    assert result.status == "recorded"
    assert result.artifact_written is True
    assert result.ticket_count == 0
    written = Path(result.artifact_path)
    assert written.exists()
    payload = yaml.safe_load(written.read_text(encoding="utf-8"))
    assert payload["status"] == "recorded"
    assert payload["tickets"] == []


@pytest.mark.integration
def test_integration_sweep_matching_sha_passes(tmp_path: Path) -> None:
    """A contract whose merge SHA matches the deployed SHA records a PASS."""
    _write_contract(tmp_path / "cc" / "contracts", "OMN-100", _DEPLOYED_SHA)
    handler = HandlerIntegrationSweepOrchestrator(runtime_sha_handler=_StubShaProbe())
    result = handler.handle(_request(tmp_path, tickets=["OMN-100"]))
    assert result.status == "recorded"
    assert result.ticket_count == 1
    assert result.details["runtime_sha_checks"] == "1"
    assert result.details["runtime_sha_stale"] == "0"


@pytest.mark.integration
def test_integration_sweep_stale_sha_blocks(tmp_path: Path) -> None:
    """Negative control: a mismatched merge SHA drives status=blocked."""
    _write_contract(
        tmp_path / "cc" / "contracts",
        "OMN-200",
        "0000000ffffffff0000000ffffffff0000000ff",
    )
    handler = HandlerIntegrationSweepOrchestrator(runtime_sha_handler=_StubShaProbe())
    result = handler.handle(_request(tmp_path, tickets=["OMN-200"]))
    assert result.status == "blocked"
    assert result.details["runtime_sha_checks"] == "1"
    assert result.details["runtime_sha_stale"] == "1"
    # A receipt artifact was written for the stale check.
    receipt = (
        tmp_path
        / "cc"
        / "receipts"
        / "OMN-200"
        / "ev-1"
        / f"{CHECK_TYPE_RUNTIME_SHA_MATCH}.yaml"
    )
    assert receipt.exists()


@pytest.mark.integration
def test_integration_sweep_multi_ticket_mixed(tmp_path: Path) -> None:
    """Mixed PASS+FAIL tickets: any stale check blocks the whole sweep."""
    contracts = tmp_path / "cc" / "contracts"
    _write_contract(contracts, "OMN-300", _DEPLOYED_SHA)
    _write_contract(contracts, "OMN-301", "1111111eeeeeeee1111111eeeeeeee1111111ee")
    handler = HandlerIntegrationSweepOrchestrator(runtime_sha_handler=_StubShaProbe())
    result = handler.handle(_request(tmp_path, tickets=["OMN-300", "OMN-301"]))
    assert result.ticket_count == 2
    assert result.details["runtime_sha_checks"] == "2"
    assert result.details["runtime_sha_stale"] == "1"
    assert result.status == "blocked"
