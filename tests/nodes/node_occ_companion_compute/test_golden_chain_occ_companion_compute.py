# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain test for node_occ_companion_compute (OMN-14285).

Pure COMPUTE node — zero I/O, deterministic. Satisfies golden-chain-coverage-gate
(OMN-12691) and the "Golden Chain Suite" CI job, which collects
``tests/nodes/*/test_golden_chain_*.py`` but does not collect node-local
``src/omnimarket/nodes/node_*/tests/`` directories.

Covers contract/metadata structural validation plus a deterministic request ->
plan replay through the real ``HandlerOccCompanionCompute.handle()`` routing
path (``ModelOccCompanionRequest`` -> ``ModelOccCompanionPlan``), asserting
byte-stable output and the reproducibility fingerprint the OMN-14055
attestation oracle relies on.

The full RSD seam-test suite (T1-T6, purity guard) lives in
``tests/unit/nodes/node_occ_companion_compute/test_occ_companion_compute_seam.py``;
this file is the golden-chain proof, not a duplicate of those seams.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from omnimarket.nodes.node_occ_companion_compute.handlers.handler_occ_companion_compute import (
    HandlerOccCompanionCompute,
    compute_companion_plan,
)
from omnimarket.nodes.node_occ_companion_compute.models.model_occ_companion_plan import (
    ModelOccCompanionPlan,
)
from omnimarket.nodes.node_occ_companion_compute.models.model_occ_companion_request import (
    ModelObservedProbe,
    ModelOccCompanionRequest,
)


@pytest.fixture
def node_dir() -> Path:
    return (
        Path(__file__).resolve().parent.parent.parent.parent
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_occ_companion_compute"
    )


@pytest.fixture
def contract_path(node_dir: Path) -> Path:
    return node_dir / "contract.yaml"


@pytest.fixture
def metadata_path(node_dir: Path) -> Path:
    return node_dir / "metadata.yaml"


def _request(**overrides: object) -> ModelOccCompanionRequest:
    base: dict[str, object] = {
        "repo": "OmniNode-ai/omnimarket",
        "pr_number": 1706,
        "pr_head_sha": "d" * 40,
        "pr_title": "feat(OMN-14285): RSD-1 companion compute",
        "pr_body": "Implements the thing.",
        "pr_state": "open",
        "pr_head_ref": "feature-branch",
        "run_timestamp": "2026-07-10T00:00:00Z",
        "product_probe": ModelObservedProbe(
            command="gh pr view 1706 --repo OmniNode-ai/omnimarket --json number,state",
            stdout='{"number":1706,"state":"OPEN"}',
            exit_code=0,
        ),
    }
    base.update(overrides)
    return ModelOccCompanionRequest(**base)


# ---------------------------------------------------------------------------
# Contract / metadata gate
# ---------------------------------------------------------------------------


class TestContractYaml:
    def test_contract_exists(self, contract_path: Path) -> None:
        assert contract_path.exists()

    def test_contract_loads(self, contract_path: Path) -> None:
        data = yaml.safe_load(contract_path.read_text())
        assert isinstance(data, dict)
        assert data["name"] == "node_occ_companion_compute"
        assert data["lifecycle"] == "experimental"
        assert data["node_type"] == "COMPUTE_GENERIC"

    def test_contract_declares_handler_routing(self, contract_path: Path) -> None:
        data = yaml.safe_load(contract_path.read_text())
        handlers = data.get("handler_routing", {}).get("handlers", [])
        assert handlers, "contract must declare at least one routed handler"
        handler = handlers[0]["handler"]
        assert handler["name"] == "HandlerOccCompanionCompute"
        assert "module" in handler

    def test_contract_declares_io_models(self, contract_path: Path) -> None:
        data = yaml.safe_load(contract_path.read_text())
        assert data["input_model"]["name"] == "ModelOccCompanionRequest"
        assert data["output_model"]["name"] == "ModelOccCompanionPlan"


class TestMetadataYaml:
    def test_metadata_exists(self, metadata_path: Path) -> None:
        assert metadata_path.exists()

    def test_metadata_loads(self, metadata_path: Path) -> None:
        data = yaml.safe_load(metadata_path.read_text())
        assert data["name"] == "node_occ_companion_compute"
        assert "version" in data
        assert "entry_points" in data

    def test_metadata_declares_read_only_no_network(self, metadata_path: Path) -> None:
        data = yaml.safe_load(metadata_path.read_text())
        caps = data.get("capabilities", {})
        assert caps.get("side_effect_class") == "read_only"
        assert caps.get("requires_network") is False


# ---------------------------------------------------------------------------
# Handler import + routing shape
# ---------------------------------------------------------------------------


class TestHandlerImport:
    def test_handler_class_exists(self) -> None:
        assert HandlerOccCompanionCompute is not None

    def test_handler_declares_compute_category(self) -> None:
        handler = HandlerOccCompanionCompute()
        assert handler.handler_category == "COMPUTE"
        assert handler.handler_type == "NODE_HANDLER"


# ---------------------------------------------------------------------------
# Golden chain replay: request -> plan through the real handler.handle() path
# ---------------------------------------------------------------------------


class TestGoldenChainReplay:
    async def test_handler_handle_replays_deterministically(self) -> None:
        handler = HandlerOccCompanionCompute()
        request = _request()
        correlation_id = uuid4()
        plan_a = await handler.handle(correlation_id, request)
        plan_b = await handler.handle(correlation_id, request)
        assert plan_a == plan_b
        assert isinstance(plan_a, ModelOccCompanionPlan)

    async def test_handler_handle_matches_pure_function(self) -> None:
        """handler.handle() must delegate to compute_companion_plan verbatim —
        the RSD-5 attestation oracle re-invokes the pure function directly, so
        the two paths can never diverge."""
        request = _request()
        via_handler = await HandlerOccCompanionCompute().handle(uuid4(), request)
        via_function = compute_companion_plan(request)
        assert via_handler == via_function

    def test_deterministic_digest_stable_across_replays(self) -> None:
        request = _request()
        digests = {
            compute_companion_plan(request).deterministic_digest for _ in range(3)
        }
        assert len(digests) == 1
        assert digests.pop() != ""

    def test_plan_authors_expected_companion_files_for_fresh_ticket(self) -> None:
        plan = compute_companion_plan(_request())
        assert plan.tickets == ("OMN-14285",)
        assert plan.companion_files
        assert plan.no_op is False
        assert all(f.is_net_new for f in plan.companion_files)
