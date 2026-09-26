# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pin the one deployment fact the delegation chains depend on (OMN-19713).

The routing reducer loads a bifrost contract and deep-merges an endpoint
overlay, which by default is a file under the user's home. A chain that
escalates or retries therefore takes a different orchestrator path on a host
with an overlay than on a CI runner without one. Every case in this package
runs against the same committed test contract that the delegation escalation
unit tests use, with the overlay pointed at a path that does not exist. The
handler, the escalation decision and the routing code are not replaced.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)
from tests.unit.delegation.conftest import BIFROST_FRONTIER_UNCONFIGURED


@pytest.fixture(autouse=True)
def pinned_bifrost_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[None]:
    contract_path = tmp_path / "bifrost_delegation.yaml"
    contract_path.write_text(BIFROST_FRONTIER_UNCONFIGURED)
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(contract_path))
    monkeypatch.setenv("BIFROST_OVERLAY_PATH", str(tmp_path / "no-overlay.yaml"))
    routing._load_bifrost_endpoints.cache_clear()
    try:
        yield
    finally:
        routing._load_bifrost_endpoints.cache_clear()
