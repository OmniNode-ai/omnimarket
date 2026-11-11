# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23663: the real wiring (``build_real``, the ssh transport) never reaches a host in ``start_mode: observe``.

The ssh transport runs every host call through its command runner. The spy below stands in for that runner and records each
argv it is handed (it answers every one with a failure, so nothing real could run). In ``observe`` mode the leader places the
lane and publishes the start it would make, and the spy sees no command (no ssh, no login draw, no process) and the ledger file
holds no new row. The control is the same wiring with ``start_mode: act``: the spy is called and the CLAIM is written, so a
spy that could not see a call would turn the observe reading red.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

from omnimarket.nodes.node_remote_lane_start_effect.adapters.adapter_ssh_transport import (
    ModelHostCommandResult,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.handlers.adapter_host_pool import (
    InMemoryLaneHostPool,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.handlers.handler_remote_lane_start import (
    HandlerRemoteLaneStart,
    build_real,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.models.model_remote_lane_start import (
    EnumRemoteLaneStartOutcome,
    ModelRemoteLaneStartRequest,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.tests import (
    lane_start_fixtures as fx,
)

WRITER = """\
import sys
ledger = sys.argv[1]
row = sys.argv[sys.argv.index("--append") + 1]
with open(ledger, "a", encoding="utf-8") as out:
    out.write(row + "\\n")
"""


class SpyHostRunner:
    """Record every command the ssh transport would run; answer each with a failure."""

    def __init__(self) -> None:
        self.argvs: list[tuple[str, ...]] = []

    def run(
        self,
        argv: Sequence[str],
        timeout_seconds: float,
        stdin: bytes | None = None,
    ) -> ModelHostCommandResult:
        self.argvs.append(tuple(argv))
        return ModelHostCommandResult(exit_code=1, stderr="spy: no host is reachable")


def _wired(
    tmp_path: Path,
    start_mode: str,
    out_of_service: dict[str, str] | None = None,
) -> tuple[HandlerRemoteLaneStart, SpyHostRunner, Path, ModelRemoteLaneStartRequest]:
    writer = tmp_path / "writer.py"
    writer.write_text(WRITER, encoding="utf-8")
    ledger = tmp_path / "ledger.md"
    ledger.write_text("# fixture ledger\n", encoding="utf-8")
    cfg = fx.settings(
        ledger_append_argv=[sys.executable, str(writer)],
        receipt_dir=str(tmp_path / "receipts"),
        start_mode=start_mode,
    )
    spy = SpyHostRunner()
    handler = build_real(
        cfg,
        InMemoryLaneHostPool(fx.snapshot()),
        stop_requested=lambda: False,
        sleep=lambda _s: None,
        echo=lambda _line: None,
        host_availability=lambda: out_of_service or {},
        host_runner=spy,
    )
    return handler, spy, ledger, fx.request(ledger_path=str(ledger))


def test_observe_never_reaches_the_ssh_transport_nor_the_ledger_nor_the_receipt_dir(
    tmp_path: Path,
) -> None:
    handler, spy, ledger, req = _wired(tmp_path, "observe")
    before = ledger.read_text(encoding="utf-8")

    result = handler.handle(req)

    assert result.outcome is EnumRemoteLaneStartOutcome.OBSERVED
    assert result.exit_code == 0
    assert result.observed is not None
    assert result.observed.host == "h106"
    assert spy.argvs == [], "observe mode ran a host command"
    assert ledger.read_text(encoding="utf-8") == before, "observe mode wrote a row"
    assert not (tmp_path / "receipts").exists(), "observe mode wrote a receipt"


def test_control_act_reaches_the_ssh_transport_and_writes_the_claim(
    tmp_path: Path,
) -> None:
    handler, spy, ledger, req = _wired(tmp_path, "act")

    result = handler.handle(req)

    assert result.outcome is not EnumRemoteLaneStartOutcome.OBSERVED
    assert spy.argvs, "act mode ran no host command: the spy cannot see a call"
    assert " | CLAIM | " in ledger.read_text(encoding="utf-8")


def test_observe_writes_no_receipt_even_when_no_host_takes_the_lane(
    tmp_path: Path,
) -> None:
    """The only host is out of service: the start ends no-host, and in observe mode that ending leaves no receipt file."""
    handler, spy, ledger, req = _wired(tmp_path, "observe", {"h106": "away"})
    before = ledger.read_text(encoding="utf-8")

    result = handler.handle(req)

    assert result.outcome is EnumRemoteLaneStartOutcome.NO_HOST
    assert result.exit_code == 75
    assert spy.argvs == []
    assert ledger.read_text(encoding="utf-8") == before
    assert not (tmp_path / "receipts").exists(), "observe mode wrote a receipt"


def test_control_act_writes_a_receipt_when_no_host_takes_the_lane(
    tmp_path: Path,
) -> None:
    """Failing control for the case above: the same refusal in act mode writes its receipt file."""
    handler, spy, _ledger, req = _wired(tmp_path, "act", {"h106": "away"})

    result = handler.handle(req)

    assert result.outcome is EnumRemoteLaneStartOutcome.NO_HOST
    assert (tmp_path / "receipts").exists(), (
        "the receipt file the control reads is missing"
    )
    assert spy.argvs == []
