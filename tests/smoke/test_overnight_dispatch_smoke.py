"""Smoke test: HandlerOvernight dispatches tonight's real contract in dry_run.

Loads a real overnight contract YAML, strips unsupported shim fields
(``dispatch_items`` — omnibase_compat#37 not yet merged), forces dry_run,
and asserts the dispatch_phases=True path runs end-to-end.

Marked ``slow`` because it imports the full build-loop orchestrator graph.

Skipped unless ``ONEX_OVERNIGHT_CONTRACT`` env var points to an existing YAML file.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml
from onex_change_control.overseer.model_overnight_contract import ModelOvernightContract

from omnimarket.nodes.node_overnight.handlers.handler_overnight import (
    EnumOvernightStatus,
    HandlerOvernight,
    ModelOvernightCommand,
)

_CONTRACT_ENV = os.environ.get("ONEX_OVERNIGHT_CONTRACT", "")
_CONTRACT = Path(_CONTRACT_ENV) if _CONTRACT_ENV else None


@pytest.mark.slow
@pytest.mark.skipif(
    _CONTRACT is None or not _CONTRACT.exists(),
    reason="ONEX_OVERNIGHT_CONTRACT not set or file not found",
)
def test_overnight_dispatch_real_contract_dry_run() -> None:
    assert _CONTRACT is not None  # narrowing for mypy
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
