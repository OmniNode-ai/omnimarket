"""Smoke test: HandlerOvernight dispatches tonight's real contract in dry_run.

Loads a real overnight contract YAML, strips unsupported shim fields
(``dispatch_items`` — omnibase_compat#37 not yet merged), forces dry_run,
and asserts the dispatch_phases=True path runs end-to-end.

Marked ``slow`` because it imports the full build-loop orchestrator graph.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from omnibase_compat.overseer.model_overnight_contract import ModelOvernightContract

from omnimarket.nodes.node_overnight.handlers.handler_overnight import (
    EnumOvernightStatus,
    HandlerOvernight,
    ModelOvernightCommand,
)

_CONTRACT = Path(
    "/Volumes/PRO-G40/Code/omni_worktrees/pr-78-review/docs/contracts/"
    "tonight-2026-04-10-overseer-contract.yaml"
)


@pytest.mark.slow
@pytest.mark.skipif(not _CONTRACT.exists(), reason="real contract not available")
def test_overnight_dispatch_real_contract_dry_run() -> None:
    data = yaml.safe_load(_CONTRACT.read_text())
    data["dry_run"] = True
    for phase in data.get("phases", []):
        phase.pop("dispatch_items", None)

    contract = ModelOvernightContract.model_validate(data)
    handler = HandlerOvernight()
    result = handler.handle(
        ModelOvernightCommand(
            correlation_id="smoke-2026-04-11",
            dry_run=True,
            overnight_contract=contract,
        ),
        dispatch_phases=True,
    )

    assert result.session_status == EnumOvernightStatus.COMPLETED
    assert result.halt_reason is None
    assert "build_loop_orchestrator" in result.phases_run
    assert "platform_readiness" in result.phases_run
    assert result.phases_failed == []
