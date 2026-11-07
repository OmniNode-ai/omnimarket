# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane start sequence over the scripted transport and a ledger fixture file (OMN-22848, plan T15).

AC2 CLAIM first, AC4 early refusals write nothing, AC5 close, and the overlay's missing-key refusal.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from omnibase_spi.contracts.services import (
    ModelLaneHostPoolSnapshot,
    ModelLaneReadEngineExitRequest,
    ModelLaneReadEngineExitResult,
)

from omnimarket.nodes.node_remote_lane_start_effect.adapters.adapter_scripted_transport import (
    ENGINE_START_CALLS,
    ModelScriptedTransportScript,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.models.model_remote_lane_start import (
    EnumRemoteLaneStartOutcome,
    ModelRemoteLaneStartSettings,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.orchestration.core import (
    claim_request_id,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.tests.lane_start_fixtures import (
    DONE_MESSAGE,
    T0,
    Rig,
    cells,
    request,
    snapshot,
    stream_result,
)

pytestmark = [pytest.mark.unit]

NODE_DIR = Path(__file__).resolve().parents[1]
DONE = ModelScriptedTransportScript(
    exit_polls=1, log_lines=("starting", stream_result(DONE_MESSAGE))
)


def _first(calls: list[str], prefix: str) -> int:
    return next(i for i, c in enumerate(calls) if c.startswith(prefix))


# ------------------------------------------------------------------ AC2 CLAIM first


def test_claim_before_engine(tmp_path: Path) -> None:
    rig = Rig(tmp_path, DONE)
    result = rig.orchestrator.run(request())
    assert result.outcome is EnumRemoteLaneStartOutcome.ENDED
    assert result.exit_code == 0
    claim_at = rig.calls.index("ledger:append CLAIM")
    start_at = min(
        _first(rig.calls, f"host:{name}")
        for name in ENGINE_START_CALLS
        if any(c.startswith(f"host:{name}") for c in rig.calls)
    )
    assert claim_at < start_at
    assert not any(c.startswith("host:") for c in rig.calls[:claim_at])
    [claim] = rig.ledger_rows("CLAIM")
    assert cells(claim)["lane"] == "floor-alpha-a1"
    assert cells(claim)["key"] == "floor:alpha:alpha-t1"
    assert cells(claim)["host"] == "h106"
    assert f"req={claim_request_id('floor-alpha-a1', 1)}" in claim
    [terminal] = rig.ledger_rows("TERMINAL")
    assert cells(terminal)["outcome"] == "handed-off"
    assert cells(terminal)["closes-CLAIM"] == claim.split(" | ")[0]


def test_claim_refused_starts_nothing(tmp_path: Path) -> None:
    rig = Rig(tmp_path, DONE, refuse_ledger=True)
    result = rig.orchestrator.run(request())
    assert result.outcome is EnumRemoteLaneStartOutcome.CLAIM_REFUSED
    assert result.exit_code == 70
    assert rig.host_calls() == []
    assert rig.ledger_rows() == []
    assert any(line.startswith("REMOTE-LANE-LEDGER-REFUSED") for line in result.lines)


def test_claim_request_id_dedup(tmp_path: Path) -> None:
    first = Rig(tmp_path, DONE)
    assert first.orchestrator.run(request()).outcome is EnumRemoteLaneStartOutcome.ENDED
    replay = Rig(tmp_path, DONE)
    result = replay.orchestrator.run(request())
    assert result.outcome is EnumRemoteLaneStartOutcome.ALREADY_CLAIMED
    assert result.exit_code == 70
    assert replay.host_calls() == []
    assert len(replay.ledger_rows("CLAIM")) == 1


def test_claim_dedup_survives_restart(tmp_path: Path) -> None:
    # The CLAIM lands, then the leader dies before any host call (the start never settles).
    first = Rig(tmp_path, ModelScriptedTransportScript(start_state="unresolved"))
    assert (
        first.orchestrator.run(request()).outcome
        is EnumRemoteLaneStartOutcome.START_UNRESOLVED
    )
    # A new process: a new ledger effect over the same ledger file, the same request id.
    # The replay comes later, so its row carries a new time stamp.
    restarted = Rig(tmp_path, DONE, now=T0 + timedelta(minutes=5))
    result = restarted.orchestrator.run(request())
    assert result.outcome is EnumRemoteLaneStartOutcome.ALREADY_CLAIMED
    assert restarted.host_calls() == []
    assert len(restarted.ledger_rows("CLAIM")) == 1
    # A new attempt is a new request id and claims again.
    again = Rig(tmp_path, DONE)
    assert (
        again.orchestrator.run(request(attempt=2)).outcome
        is EnumRemoteLaneStartOutcome.ENDED
    )


# ------------------------------------------------------------------ AC4 early refusals write nothing


@pytest.mark.parametrize(
    ("name", "fields", "pool", "outcome", "code"),
    [
        ("no-host", {}, snapshot(slots=0), EnumRemoteLaneStartOutcome.NO_HOST, 75),
        (
            "cloud-placement",
            {"placement": "cloud"},
            None,
            EnumRemoteLaneStartOutcome.UNSUPPORTED_SHAPE,
            64,
        ),
        (
            "engine-codex",
            {"engine": "codex"},
            None,
            EnumRemoteLaneStartOutcome.UNSUPPORTED_SHAPE,
            64,
        ),
        (
            "engine-glm",
            {"engine": "glm"},
            None,
            EnumRemoteLaneStartOutcome.UNSUPPORTED_SHAPE,
            64,
        ),
        (
            "engine-code-edit",
            {"engine": "code-edit"},
            None,
            EnumRemoteLaneStartOutcome.UNSUPPORTED_SHAPE,
            64,
        ),
    ],
)
def test_early_refusals_write_nothing(
    tmp_path: Path,
    name: str,
    fields: dict[str, str],
    pool: ModelLaneHostPoolSnapshot | None,
    outcome: EnumRemoteLaneStartOutcome,
    code: int,
) -> None:
    rig = Rig(tmp_path, DONE, pool=pool)
    result = rig.orchestrator.run(request(**fields))
    assert result.outcome is outcome, name
    assert result.exit_code == code
    assert rig.calls == []
    assert rig.ledger_rows() == []
    assert rig.receipts.written == []


# ------------------------------------------------------------------ AC5 close


def test_sigterm_closes_once(tmp_path: Path) -> None:
    rig = Rig(tmp_path, ModelScriptedTransportScript(exit_polls=99), stop_after_polls=2)
    result = rig.orchestrator.run(request())
    assert result.outcome is EnumRemoteLaneStartOutcome.STOPPED
    assert result.exit_code == 143
    stop_at = rig.calls.index("host:stop_engine host=h106")
    [terminal] = rig.ledger_rows("TERMINAL")
    assert cells(terminal)["outcome"] == "stopped"
    assert rig.calls.index("ledger:append TERMINAL") > stop_at
    assert rig.calls.count("ledger:append TERMINAL") == 1


def test_sigterm_survivor_no_terminal(tmp_path: Path) -> None:
    rig = Rig(
        tmp_path,
        ModelScriptedTransportScript(exit_polls=99, stop_confirmed=False),
        stop_after_polls=1,
    )
    result = rig.orchestrator.run(request())
    assert result.outcome is EnumRemoteLaneStartOutcome.STOP_REFUSED
    assert result.exit_code == 143
    assert rig.ledger_rows("TERMINAL") == []
    assert len(rig.ledger_rows("CLAIM")) == 1
    assert result.receipt.state == "stop-refused"
    assert not result.receipt.final


def test_start_unresolved_no_terminal(tmp_path: Path) -> None:
    rig = Rig(tmp_path, ModelScriptedTransportScript(start_state="unresolved"))
    result = rig.orchestrator.run(request())
    assert result.outcome is EnumRemoteLaneStartOutcome.START_UNRESOLVED
    assert result.exit_code == 1
    assert rig.ledger_rows("TERMINAL") == []
    assert len(rig.ledger_rows("CLAIM")) == 1
    assert "host:read_engine_exit host=h106" not in rig.calls


def test_no_double_terminal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(tmp_path, DONE)
    lane_close = "2026-11-07T08:00:30Z | TERMINAL | lane=floor-alpha-a1 | ticket=OMN-22848 | outcome=handed-off | closed by the lane"
    real_read = rig.transport.read_engine_exit

    # the lane closes itself while the engine runs
    def lane_writes_its_own_terminal(
        req: ModelLaneReadEngineExitRequest,
    ) -> ModelLaneReadEngineExitResult:
        if not any(" | TERMINAL | " in r for r in rig.ledger_rows()):
            with rig.ledger_path.open("a", encoding="utf-8") as fh:
                fh.write(lane_close + "\n")
        return real_read(req)

    monkeypatch.setattr(rig.transport, "read_engine_exit", lane_writes_its_own_terminal)
    result = rig.orchestrator.run(request())
    assert result.outcome is EnumRemoteLaneStartOutcome.ENDED
    assert rig.ledger_rows("TERMINAL") == [lane_close]
    [status] = rig.ledger_rows("STATUS")
    assert "ended status=handed-off engine_rc=0" in status


def test_engine_fails_closes_failed(tmp_path: Path) -> None:
    rig = Rig(tmp_path, ModelScriptedTransportScript(exit_code=1, log_lines=("boom",)))
    result = rig.orchestrator.run(request())
    assert result.outcome is EnumRemoteLaneStartOutcome.ENDED
    assert result.exit_code == 1
    [terminal] = rig.ledger_rows("TERMINAL")
    assert "engine_rc=1" in terminal
    assert cells(terminal)["outcome"] != "handed-off"


def test_receipt_rewritten_at_each_state_change(tmp_path: Path) -> None:
    rig = Rig(tmp_path, DONE)
    rig.orchestrator.run(request())
    states = [r.state for r in rig.receipts.written]
    assert states[0] == "claimed"
    assert "running" in states
    assert "ended" in states
    assert rig.receipts.written[-1].final


# ------------------------------------------------------------------ no hard-coded value


def test_missing_overlay_key_refused_naming_the_key() -> None:
    overlay = yaml.safe_load(
        (NODE_DIR / "overlay.example.yaml").read_text(encoding="utf-8")
    )
    ModelRemoteLaneStartSettings.from_overlay(overlay)
    for key in ModelRemoteLaneStartSettings.model_fields:
        partial = {
            "remote_lane_start": {
                k: v for k, v in overlay["remote_lane_start"].items() if k != key
            }
        }
        with pytest.raises(KeyError) as refused:
            ModelRemoteLaneStartSettings.from_overlay(partial)
        assert f"remote_lane_start.{key}" in str(refused.value)


def test_runner_command_names_node() -> None:
    overlay = yaml.safe_load(
        (NODE_DIR / "overlay.example.yaml").read_text(encoding="utf-8")
    )
    command = overlay["floor_fill"]["runner_command"]
    assert "node_remote_lane_start_orchestrator" in command
    assert not any("onex_remote_lane" in part for part in command)


def test_ledger_path_is_required() -> None:
    with pytest.raises(ValueError, match="ledger_path"):
        request(ledger_path="")
