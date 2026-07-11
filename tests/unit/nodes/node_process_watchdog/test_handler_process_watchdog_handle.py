# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Unit tests for HandlerProcessWatchdog.handle() -- the canonical thin
single-payload RuntimeLocal dispatch entry point (OMN-14242).

Prior to this change ``handle()`` took an optional keyword-only ``targets``
argument that RuntimeLocal's single-parameter dispatch (``handler.handle(payload)``)
can never supply -- so any real dispatch through the runtime silently produced
an empty, UNKNOWN-status report (fail-fast violation: a wrong default hiding a
broken wiring path). These tests pin the corrected behavior: ``handle()``
takes exactly one positional argument and wires the production check-target
set internally so a real dispatch actually executes checks.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime

import pytest

from omnimarket.nodes.node_process_watchdog.handlers import (
    handler_process_watchdog as hpw_module,
)
from omnimarket.nodes.node_process_watchdog.handlers.handler_process_watchdog import (
    HandlerProcessWatchdog,
    InmemoryCheckTarget,
)
from omnimarket.nodes.node_process_watchdog.models.model_watchdog_start_command import (
    ModelWatchdogStartCommand,
)
from omnimarket.nodes.node_process_watchdog.models.model_watchdog_state import (
    EnumCheckStatus,
    EnumCheckTarget,
)


def _command(**overrides: object) -> ModelWatchdogStartCommand:
    defaults: dict[str, object] = {
        "check_targets": [EnumCheckTarget.EMIT_DAEMON],
        "correlation_id": "corr-handle-dispatch",
        "dry_run": True,
        "alert_on_degraded": True,
        "requested_at": datetime.now(tz=UTC),
    }
    defaults.update(overrides)
    return ModelWatchdogStartCommand(**defaults)


@pytest.mark.unit
def test_handle_signature_is_single_positional_payload() -> None:
    """Canonical shape: handle(self, payload: TypedRequest) -> TypedResult.

    No keyword-only DI params -- RuntimeLocal's adapter calls
    ``handler.handle(payload)`` with exactly one positional argument.
    """
    sig = inspect.signature(HandlerProcessWatchdog.handle)
    params = [p for name, p in sig.parameters.items() if name != "self"]

    assert len(params) == 1
    assert params[0].name == "payload"
    assert params[0].kind in (
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    )


@pytest.mark.unit
def test_handle_wires_production_targets_and_executes_real_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """handle(payload) must not silently no-op -- it wires real check
    targets (production set) so a single-arg RuntimeLocal dispatch actually
    runs checks instead of returning an empty/UNKNOWN report."""
    fake_target = InmemoryCheckTarget(
        name="emit_daemon",
        category=EnumCheckTarget.EMIT_DAEMON,
        status=EnumCheckStatus.HEALTHY,
        message="ok",
    )
    monkeypatch.setattr(hpw_module, "build_production_targets", lambda: [fake_target])

    handler = HandlerProcessWatchdog()
    command = _command()

    completed = handler.handle(command)

    assert completed.overall_status == EnumCheckStatus.HEALTHY
    assert completed.report.total_checks == 1
    assert completed.report.checks[0].target == "emit_daemon"
    assert completed.correlation_id == command.correlation_id


@pytest.mark.unit
def test_contract_declares_both_publish_topics() -> None:
    """The node's contract declares both output topics it can publish on:
    the terminal completed topic and the degraded-alert topic."""
    from pathlib import Path

    import omnimarket.nodes.node_process_watchdog as node_pkg

    contract_text = (Path(node_pkg.__file__).parent / "contract.yaml").read_text()
    assert "onex.evt.omnimarket.watchdog-completed.v1" in contract_text
    assert "onex.evt.omnimarket.watchdog-alert.v1" in contract_text
