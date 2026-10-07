# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract to handler to decision: the lease node as its contract declares it."""

from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from typing import Any

import pytest
import yaml

NODE = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_worktree_lease_compute"
)


def _symbol(dotted: str) -> Any:
    module, _, name = dotted.rpartition(".")
    return getattr(import_module(module), name)


@pytest.mark.unit
def test_golden_chain_worktree_lease_compute() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text())
    request_model = _symbol(contract["input_model"])
    decision_model = _symbol(contract["output_model"])
    handler = getattr(
        import_module(contract["handler"]["module"]), contract["handler"]["class"]
    )()
    ledger = "\n".join(
        [
            "2026-10-07T09:00:00Z | CLAIM | lane=holder-1 | ticket=OMN-100 | scope",
            "2026-10-07T11:00:00Z | STATUS | lane=holder-1 | ticket=OMN-100 | busy",
        ]
    )
    request = request_model(
        requester_lane="second-2",
        worktree_path="/trees/OMN-100/repo",
        root="/trees",
        ledger_text=ledger,
        now=datetime(2026, 10, 7, 12, 0, tzinfo=UTC),
    )
    decision = handler.handle(request)
    assert isinstance(decision, decision_model)
    assert decision.verdict == "refused"
    assert decision.holder_lane == "holder-1"
    assert decision.release_path is not None
    assert "RELEASE | lane=holder-1" in decision.release_path
    # The refusal survives the bus round trip a published event would take.
    assert decision_model.model_validate_json(decision.model_dump_json()) == decision
