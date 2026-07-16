# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain test for node_occ_autoauthor_window (OMN-14393).

Pure COMPUTE node — zero I/O, deterministic. Satisfies golden-chain-coverage-gate
and the "Golden Chain Suite" CI job (collects ``tests/nodes/*/test_golden_chain_*.py``).

Covers contract/metadata structural validation plus a deterministic
observations -> window-result replay through the real
``HandlerOccAutoauthorWindow.handle()`` path. The full counter-semantics suite
lives in ``tests/unit/nodes/node_occ_autoauthor_window/test_window_counter.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.events.occ_autoauthor import ModelOccAutoauthorObservation
from omnimarket.nodes.node_occ_autoauthor_window.handlers.handler_occ_autoauthor_window import (
    HandlerOccAutoauthorWindow,
    aggregate_autoauthor_window,
)
from omnimarket.nodes.node_occ_autoauthor_window.models.model_occ_autoauthor_window_request import (
    ModelOccAutoauthorWindowRequest,
)
from omnimarket.nodes.node_occ_autoauthor_window.models.model_occ_autoauthor_window_result import (
    ModelOccAutoauthorWindowResult,
)


@pytest.fixture
def node_dir() -> Path:
    return (
        Path(__file__).resolve().parent.parent.parent.parent
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_occ_autoauthor_window"
    )


def _clean_trail(n: int) -> tuple[ModelOccAutoauthorObservation, ...]:
    return tuple(
        ModelOccAutoauthorObservation(
            product_repo="OmniNode-ai/omnimarket",
            product_pr_number=i,
            occ_pr_number=1000 + i,
            minted_by_node=True,
            attestation_match=True,
            occ_preflight_eligible=True,
            observed_at=f"2026-07-16T00:{i:02d}:00Z",
        )
        for i in range(1, n + 1)
    )


class TestContractYaml:
    def test_contract_loads_as_compute_node(self, node_dir: Path) -> None:
        data = yaml.safe_load((node_dir / "contract.yaml").read_text())
        assert data["name"] == "node_occ_autoauthor_window"
        assert data["node_type"] == "COMPUTE_GENERIC"
        assert data["lifecycle"] == "experimental"

    def test_contract_declares_window_operation(self, node_dir: Path) -> None:
        data = yaml.safe_load((node_dir / "contract.yaml").read_text())
        ops = {
            h["operation"]: h["handler"]["name"]
            for h in data["handler_routing"]["handlers"]
        }
        assert ops.get("aggregate_autoauthor_window") == "HandlerOccAutoauthorWindow"

    def test_contract_is_topicless_directly_invoked(self, node_dir: Path) -> None:
        # No event_bus / terminal_event => no graph edges; runs via _run_compute.
        data = yaml.safe_load((node_dir / "contract.yaml").read_text())
        assert "event_bus" not in data
        assert "terminal_event" not in data
        assert data["handler"]["class"] == "HandlerOccAutoauthorWindow"

    def test_metadata_is_read_only_no_network(self, node_dir: Path) -> None:
        data = yaml.safe_load((node_dir / "metadata.yaml").read_text())
        caps = data["capabilities"]
        assert caps["side_effect_class"] == "read_only"
        assert caps["requires_network"] is False


class TestGoldenChainReplay:
    async def test_handle_replays_deterministically(self) -> None:
        handler = HandlerOccAutoauthorWindow()
        request = ModelOccAutoauthorWindowRequest(
            observations=_clean_trail(10), required_streak=10
        )
        a = await handler.handle(request)
        b = await handler.handle(request)
        assert a == b
        assert isinstance(a, ModelOccAutoauthorWindowResult)

    async def test_handle_matches_pure_function(self) -> None:
        request = ModelOccAutoauthorWindowRequest(
            observations=_clean_trail(7), required_streak=10
        )
        via_handler = await HandlerOccAutoauthorWindow().handle(request)
        via_function = aggregate_autoauthor_window(request)
        assert via_handler == via_function

    async def test_ten_clean_reaches_flip_ready(self) -> None:
        request = ModelOccAutoauthorWindowRequest(
            observations=_clean_trail(10), required_streak=10
        )
        result = await HandlerOccAutoauthorWindow().handle(request)
        assert result.consecutive_clean == 10
        assert result.flip_ready is True
