# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: a demand on node_ask_done_check_effect's request topic reaches its handler (OMN-24993).

The handler is the one the contract names, built with no arguments as the runtime builds it. Golden leg:
a deployment is present, the check reads a ledger row stamped after the ask, and the verdict is TRUE with
its row appended. Error leg: no deployment is present, so the ask reads UNKNOWN, nothing is appended and
the ask stays open.
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_ask_done_check_effect.adapters import InMemoryFloorRead
from omnimarket.nodes.node_ask_done_check_effect.handlers import HandlerAskDoneCheck
from omnimarket.nodes.node_ask_done_check_effect.models import (
    EnumCheckVerdict,
    ModelAskDoneCheckRequest,
    ModelAskToCheck,
)
from omnimarket.nodes.node_ask_done_check_effect.protocols import ModelFloorRead
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)

pytestmark = pytest.mark.unit

NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_ask_done_check_effect"
)
SAID = datetime(2026, 11, 6, 14, 27, 51, tzinfo=UTC)
NOW = datetime(2026, 11, 6, 17, 30, tzinfo=UTC)
ASK = "ask-00000000b2"
CHECK = "row:lane=host-reading,probe=ledger-durability,host=h053,verdict=WORKING"
PROBE_ROW = (
    "2026-11-06T17:22:47Z | STATUS | lane=host-reading | src=host-reading | host=h053 | "
    "probe=ledger-durability | verdict=WORKING | drill=no | durable_lag_s=28"
)


def _contract() -> dict[str, Any]:
    loaded = yaml.safe_load((NODE_DIR / "contract.yaml").read_text("utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _contract_handler(**kwargs: Any) -> Any:
    declared = _contract()["handler"]
    module = importlib.import_module(declared["module"])
    return getattr(module, declared["class"])(**kwargs)


def _request() -> ModelAskDoneCheckRequest:
    return ModelAskDoneCheckRequest(
        asks=(ModelAskToCheck(ask_id=ASK, said_at=SAID, done_check=CHECK),), write=True
    )


def test_golden_chain_a_demand_reads_true_and_appends_the_verdict_row() -> None:
    ledger = InMemoryLedger([PROBE_ROW])

    def factory(overlay: Any, _env: Any) -> HandlerAskDoneCheck:
        floor = InMemoryFloorRead(
            ModelFloorRead(pieces={}, generated_at="2026-11-06T17:30:00Z")
        )
        return HandlerAskDoneCheck(overlay, floor, ledger, ledger, clock=lambda: NOW)

    handler = _contract_handler(
        environ={"ONEX_ASK_DONE_CHECK_OVERLAY": str(NODE_DIR / "overlay.example.yaml")},
        factory=factory,
    )
    [verdict] = handler.handle(_request()).verdicts
    assert verdict.verdict is EnumCheckVerdict.TRUE
    assert verdict.written == "yes"
    assert f"ask={ASK}" in ledger.lines[-1]
    assert "verdict=TRUE" in ledger.lines[-1]


def test_error_chain_a_demand_with_no_deployment_reads_unknown_and_appends_nothing() -> (
    None
):
    handler = _contract_handler(environ={})
    [verdict] = handler.handle(_request()).verdicts
    assert verdict.verdict is EnumCheckVerdict.UNKNOWN
    assert verdict.written == "no"
    assert verdict.row == ""
