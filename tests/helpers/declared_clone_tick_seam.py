# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The SEAM tests of the declared-clone tick (OMN-23557).

``assert_tick_seam`` runs the tick over a bus and asserts the request/receipt topics, payloads and
the host-table head movement. ``assert_overlay_and_unit_seams`` asserts the overlay and the
host-installer unit against the tick module. Each seam fails a test when either side changes shape.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from omnimarket.lab_work.bus import _subscribe
from omnimarket.models.overlay_keys import ModelLabHostsTable
from omnimarket.nodes.node_canonical_clone_refresh_effect.handlers.adapter_declared_clone_refresh_host import (
    load_declared_clone_topics,
)
from omnimarket.nodes.node_canonical_clone_refresh_effect.handlers.handler_declared_clone_refresh import (
    HandlerDeclaredCloneRefresh,
)
from omnimarket.nodes.node_canonical_clone_refresh_effect.models.model_declared_clone_refresh import (
    ModelDeclaredCloneRefreshReceipt,
    ModelDeclaredCloneRefreshRequest,
)
from omnimarket.nodes.node_canonical_clone_refresh_effect.models.model_declared_clone_tick import (
    ModelDeclaredCloneTickOverlay,
)
from omnimarket.nodes.node_canonical_clone_refresh_effect.runtime_declared_clone_tick import (
    run_tick,
)
from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    load_units,
)
from tests.helpers.kbi_clone_coverage_layout import HomesTransport, Layout


async def assert_tick_seam(bus: Any, layout: Layout) -> None:
    """Run one host's tick over ``bus`` and assert the S1 request/receipt seams."""
    topics = load_declared_clone_topics()
    request_msgs: list[Any] = []
    receipt_msgs: list[Any] = []

    async def _collect_request(m: Any) -> None:
        request_msgs.append(m.value)

    async def _collect_receipt(m: Any) -> None:
        receipt_msgs.append(m.value)

    unsubscribe_request = await _subscribe(
        bus, topics.request, _collect_request, "seam-request", "latest"
    )
    unsubscribe_receipt = await _subscribe(
        bus, topics.receipt, _collect_receipt, "seam-receipt", "latest"
    )
    handler = HandlerDeclaredCloneRefresh(
        layout.table, layout.repo_dir, HomesTransport(layout.homes)
    )

    @asynccontextmanager
    async def open_bus() -> AsyncIterator[Any]:
        yield bus

    receipt = await run_tick(open_bus, handler, "h101", 60.0)
    await unsubscribe_request()
    await unsubscribe_receipt()

    assert not receipt.refused
    assert layout.head("h101") == layout.origin_tip
    assert layout.head("h053") == layout.start["h053"]

    assert len(request_msgs) == 1
    request_payload = json.loads(request_msgs[0]).get("payload")
    request = ModelDeclaredCloneRefreshRequest.model_validate(request_payload)
    assert request.hosts == ("h101",)

    assert len(receipt_msgs) >= 1
    receipt_payload = json.loads(receipt_msgs[0]).get("payload")
    receipt_model = ModelDeclaredCloneRefreshReceipt.model_validate(receipt_payload)
    outcomes = {o.host: o.state for o in receipt_model.outcomes}
    assert outcomes == {"h101": "refreshed"}

    contract_text = (
        resources.files("omnimarket.nodes.node_canonical_clone_refresh_effect")
        .joinpath("contract.yaml")
        .read_text()
    )
    contract = yaml.safe_load(contract_text)
    block = contract["declared_clone_refresh"]
    assert topics.request == block["request_topic"]
    assert topics.receipt == block["receipt_topic"]
    consumed = [e["topic"] for e in contract["consumed_events"]]
    published = [e["topic"] for e in contract["published_events"]]
    assert topics.request in consumed
    assert topics.receipt in published


def assert_overlay_and_unit_seams() -> None:
    """Assert the S2 host-table clones column, S3 overlay and S4 host-installer unit seams."""
    row = {
        "name": "h101",
        "target": "h101.invalid",
        "clones": [
            {
                "repo": "OmniNode-ai/kbrepo",
                "ref": "main",
                "path": "$HOME/Code/kbrepo",
            }
        ],
    }
    table = ModelLabHostsTable.model_validate({"hosts": [row]})
    assert table.hosts[0].clones[0].path == "$HOME/Code/kbrepo"

    fixture = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "declared_clone_tick"
        / "node_overlay.yaml"
    )
    overlay = ModelDeclaredCloneTickOverlay.model_validate(
        yaml.safe_load(fixture.read_text())
    )
    units = load_units()
    unit = units["declared-clone-refresh"]
    assert overlay.host_name_env in unit["environment"]

    import omnimarket.nodes.node_canonical_clone_refresh_effect.runtime_declared_clone_tick as tick_module

    assert unit["module"] == tick_module.__name__
    assert (Path(__file__).resolve().parents[2] / unit["entry_file"]).is_file()
