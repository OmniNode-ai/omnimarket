# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain test for node_occ_observation_projection (OMN-14851).

Pure COMPUTE node — zero I/O, deterministic, storage-agnostic. Satisfies
golden-chain-coverage-gate and the "Golden Chain Suite" CI job (collects
``tests/nodes/*/test_golden_chain_*.py``).

Covers contract/metadata structural validation plus a deterministic
records -> projection-result replay through the real
``HandlerOccObservationProjection.handle()`` path. The full dedup-semantics
suite lives in
``tests/unit/nodes/node_occ_observation_projection/test_observation_projection.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.events.occ_autoauthor import ModelOccAutoauthorObservation
from omnimarket.events.occ_observation_record import ModelOccObservationRecord
from omnimarket.nodes.node_occ_observation_projection.handlers.handler_occ_observation_projection import (
    HandlerOccObservationProjection,
    compute_observation_projection,
)
from omnimarket.nodes.node_occ_observation_projection.models.model_occ_observation_projection_request import (
    ModelOccObservationProjectionRequest,
)
from omnimarket.nodes.node_occ_observation_projection.models.model_occ_observation_projection_result import (
    ModelOccObservationProjectionResult,
)

_REPO = "OmniNode-ai/omnimarket"


@pytest.fixture
def node_dir() -> Path:
    return (
        Path(__file__).resolve().parent.parent.parent.parent
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_occ_observation_projection"
    )


def _records(n: int) -> tuple[ModelOccObservationRecord, ...]:
    return tuple(
        ModelOccObservationRecord(
            product_repo=_REPO,
            product_pr_number=i,
            head_sha=f"sha-{i:04d}",
            policy_version="v2",
            workflow_run_id=1000 + i,
            run_attempt=1,
            recorded_at=f"2026-07-20T00:{i:02d}:00Z",
            observation=ModelOccAutoauthorObservation(
                product_repo=_REPO,
                product_pr_number=i,
                occ_pr_number=5000 + i,
                minted_by_node=True,
                attestation_match=True,
                occ_preflight_eligible=True,
                observed_at=f"2026-07-20T00:{i:02d}:00Z",
            ),
        )
        for i in range(1, n + 1)
    )


class TestContractYaml:
    def test_contract_loads_as_compute_node(self, node_dir: Path) -> None:
        data = yaml.safe_load((node_dir / "contract.yaml").read_text())
        assert data["name"] == "node_occ_observation_projection"
        assert data["node_type"] == "COMPUTE_GENERIC"
        assert data["lifecycle"] == "experimental"

    def test_contract_declares_projection_operation(self, node_dir: Path) -> None:
        data = yaml.safe_load((node_dir / "contract.yaml").read_text())
        ops = {
            h["operation"]: h["handler"]["name"]
            for h in data["handler_routing"]["handlers"]
        }
        assert (
            ops.get("compute_observation_projection")
            == "HandlerOccObservationProjection"
        )

    def test_contract_is_directly_invoked_compute_path(self, node_dir: Path) -> None:
        # No event_bus and no top-level terminal_event scalar => the local
        # runtime uses the _run_compute path for `onex node`, matching
        # node_occ_autoauthor_window. The runtime_dispatch block declares the
        # dispatch-addressable command seam without changing that path.
        data = yaml.safe_load((node_dir / "contract.yaml").read_text())
        assert "event_bus" not in data
        assert "terminal_event" not in data
        assert (
            data["runtime_dispatch"]["command_topic"]
            == "onex.cmd.omnimarket.occ-observation-projection-requested.v1"
        )
        assert data["handler"]["class"] == "HandlerOccObservationProjection"

    def test_metadata_is_read_only_no_network(self, node_dir: Path) -> None:
        data = yaml.safe_load((node_dir / "metadata.yaml").read_text())
        caps = data["capabilities"]
        assert caps["side_effect_class"] == "read_only"
        assert caps["requires_network"] is False


class TestGoldenChainReplay:
    async def test_handle_replays_deterministically(self) -> None:
        handler = HandlerOccObservationProjection()
        request = ModelOccObservationProjectionRequest(records=_records(10))
        a = await handler.handle(request)
        b = await handler.handle(request)
        assert a == b
        assert isinstance(a, ModelOccObservationProjectionResult)

    async def test_handle_matches_pure_function(self) -> None:
        request = ModelOccObservationProjectionRequest(records=_records(7))
        via_handler = await HandlerOccObservationProjection().handle(request)
        via_function = compute_observation_projection(request)
        assert via_handler == via_function

    async def test_ten_distinct_tuples_project_to_ten_observations(self) -> None:
        request = ModelOccObservationProjectionRequest(records=_records(10))
        result = await HandlerOccObservationProjection().handle(request)
        assert result.distinct_source_tuples == 10
        assert len(result.observations) == 10
        assert all(o.is_clean for o in result.observations)
