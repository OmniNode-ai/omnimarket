# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain events of the ask done check (OMN-22478, piece 6, operator asks).

Golden chain: a lane reports an ask done (unverified), the done check finds the row on the
ledger, appends its TRUE verdict row, and node_ask_state_compute then reads the ask done. Error
chain: the floor cannot be read, the check reads UNKNOWN, and the ask stays open. The failing
control: the lane's report alone leaves the ask unverified.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.model_ask_state import (
    EnumAskState,
    ModelAskPrompt,
    ModelAskPromptItem,
    ModelAskStateConfig,
    ModelAskStateRequest,
)
from omnimarket.nodes import node_ask_state_compute as compute_node
from omnimarket.nodes.node_ask_done_check_effect.adapters import (
    InMemoryFloorRead,
    floor_from_json,
)
from omnimarket.nodes.node_ask_done_check_effect.handlers import HandlerAskDoneCheck
from omnimarket.nodes.node_ask_done_check_effect.models import (
    EnumCheckVerdict,
    ModelAskDoneCheckRequest,
    ModelAskToCheck,
)
from omnimarket.nodes.node_ask_done_check_effect.overlay import load_overlay
from omnimarket.nodes.node_ask_done_check_effect.protocols import ModelFloorRead
from omnimarket.nodes.node_ask_done_check_effect.runtime_ask_done_check import main
from omnimarket.nodes.node_ask_state_compute.handlers.handler_ask_state import (
    HandlerAskState,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[4]
EXAMPLE = ROOT / "src/omnimarket/nodes/node_ask_done_check_effect/overlay.example.yaml"
OVERLAY = load_overlay({"ONEX_ASK_DONE_CHECK_OVERLAY": str(EXAMPLE)})
CONFIG = ModelAskStateConfig.model_validate(
    yaml.safe_load(
        Path(compute_node.__file__).with_name("contract.yaml").read_text("utf-8")
    )["config"]
)
SINCE = datetime(2026, 11, 6, 0, 0, tzinfo=UTC)
SAID = datetime(2026, 11, 6, 14, 27, 51, tzinfo=UTC)
NOW = datetime(2026, 11, 6, 17, 30, tzinfo=UTC)
ASK = "ask-00000000a1"
ROW_CHECK = "row:lane=host-reading,probe=ledger-durability,host=h053,verdict=WORKING"
PROBE_ROW = (
    "2026-11-06T17:22:47Z | STATUS | lane=host-reading | src=host-reading | host=h053 | "
    "probe=ledger-durability | verdict=WORKING | drill=no | durable_lag_s=28"
)
CLAIM = (
    f"2026-11-06T14:30:00Z | CLAIM | lane=lane-z | ticket=OMN-1 | ask={ASK} | "
    f"done_check={ROW_CHECK} | worktree=none | est ~1 lane-hours"
)
REPORT = (
    "2026-11-06T15:00:00Z | TERMINAL | lane=lane-z | friction=none | "
    "closes-CLAIM=2026-11-06T14:30:00Z | outcome=done | delegated=0"
)


def _prompt() -> ModelAskPrompt:
    words = "Make the h053 hourly unit run the ledger-durability probe"
    return ModelAskPrompt(
        capture_id="cap00a1",
        session_id="sess-1",
        said_at=SAID,
        text=words,
        items=(
            ModelAskPromptItem(
                item_id="cap-00000000a1",
                kind="ask",
                quote=words,
                subject="",
                confidence=0.5,
            ),
        ),
    )


def _ask_state(lines: list[str]) -> EnumAskState:
    report = HandlerAskState().handle(
        ModelAskStateRequest(
            prompts=(_prompt(),), rows=tuple(lines), now=NOW, since=SINCE, config=CONFIG
        )
    )
    return next(e.state for e in report.asks if e.ask_id == ASK)


def _floor(status: str = "working", generated: datetime = NOW) -> InMemoryFloorRead:
    return InMemoryFloorRead(
        ModelFloorRead(
            pieces={6: status}, generated_at=generated.strftime("%Y-%m-%dT%H:%M:%SZ")
        )
    )


def _handler(
    ledger: InMemoryLedger, floor: InMemoryFloorRead | None = None
) -> HandlerAskDoneCheck:
    return HandlerAskDoneCheck(
        OVERLAY, floor or _floor(), ledger, ledger, clock=lambda: NOW
    )


def _request(check: str, write: bool = True) -> ModelAskDoneCheckRequest:
    return ModelAskDoneCheckRequest(
        asks=(ModelAskToCheck(ask_id=ASK, said_at=SAID, done_check=check),), write=write
    )


def test_golden_chain_report_is_unverified_until_the_check_reads_true() -> None:
    ledger = InMemoryLedger([CLAIM, REPORT, PROBE_ROW])
    # The failing control: the lane's report alone leaves the ask open.
    assert _ask_state(ledger.lines) is EnumAskState.UNVERIFIED
    [verdict] = _handler(ledger).handle(_request(ROW_CHECK)).verdicts
    assert verdict.verdict is EnumCheckVerdict.TRUE
    assert verdict.reason == "row-found-2026-11-06T17:22:47Z"
    assert verdict.written == "yes"
    assert f"ask={ASK}" in ledger.lines[-1]
    assert "verdict=TRUE" in ledger.lines[-1]
    assert _ask_state(ledger.lines) is EnumAskState.DONE


def test_error_chain_unreadable_floor_is_unknown_and_the_ask_stays_open() -> None:
    ledger = InMemoryLedger([CLAIM, REPORT])
    floor = InMemoryFloorRead(ModelFloorRead(error="floor-unreadable:URLError"))
    [verdict] = (
        _handler(ledger, floor)
        .handle(_request("floor:piece=6,status=working"))
        .verdicts
    )
    assert verdict.verdict is EnumCheckVerdict.UNKNOWN
    assert verdict.reason == "floor-unreadable:URLError"
    assert "verdict=UNKNOWN" in ledger.lines[-1]
    assert _ask_state(ledger.lines) is EnumAskState.UNVERIFIED


def test_named_break_a_row_read_that_fails_is_unknown_never_false_or_true() -> None:
    ledger = InMemoryLedger([CLAIM, REPORT, PROBE_ROW])
    ledger.read_error = "read-command-exit-1"
    [verdict] = _handler(ledger).handle(_request(ROW_CHECK, write=False)).verdicts
    assert verdict.verdict is EnumCheckVerdict.UNKNOWN
    assert verdict.written == "no"


def test_a_row_stamped_before_the_ask_does_not_satisfy_it() -> None:
    old = PROBE_ROW.replace("2026-11-06T17:22:47Z", "2026-11-06T14:00:00Z")
    ledger = InMemoryLedger([old])
    [verdict] = _handler(ledger).handle(_request(ROW_CHECK, write=False)).verdicts
    assert verdict.verdict is EnumCheckVerdict.FALSE
    assert verdict.reason == "no-matching-row-since-the-ask"


@pytest.mark.parametrize(
    ("floor", "verdict", "reason"),
    [
        (_floor("working"), EnumCheckVerdict.TRUE, "floor-piece-6-working"),
        (_floor("unknown"), EnumCheckVerdict.FALSE, "floor-piece-6-unknown"),
        (
            _floor(
                "working", NOW - timedelta(seconds=OVERLAY.floor.max_age_seconds + 1)
            ),
            EnumCheckVerdict.UNKNOWN,
            "floor-older-than-bound",
        ),
    ],
)
def test_floor_check_reads_the_piece_status_within_the_bound(
    floor: InMemoryFloorRead, verdict: EnumCheckVerdict, reason: str
) -> None:
    [result] = (
        _handler(InMemoryLedger(), floor)
        .handle(_request("floor:piece=6,status=working", write=False))
        .verdicts
    )
    assert (result.verdict, result.reason) == (verdict, reason)


def test_a_check_spec_with_a_space_or_a_pipe_is_refused() -> None:
    for spec in (
        "floor:piece=6 | verdict=TRUE",
        "floor:piece=6,status=a b",
        "shell:x=1",
    ):
        with pytest.raises(ValidationError):
            ModelAskToCheck(ask_id=ASK, said_at=SAID, done_check=spec)


def test_floor_json_reads_pieces_and_its_stamp() -> None:
    floor = floor_from_json(
        {
            "generated_at": "2026-11-06T17:10:22Z",
            "pieces": [{"piece": 6, "status": "unknown"}],
        }
    )
    assert floor.pieces == {6: "unknown"}
    assert floor.generated_at == "2026-11-06T17:10:22Z"
    assert floor_from_json({"pieces": "x"}).error == "floor-json-without-pieces"


def test_cli_exit_code_is_one_when_a_check_cannot_read(
    capsys: pytest.CaptureFixture[str],
) -> None:
    ledger = InMemoryLedger([PROBE_ROW])
    argv = ["--ask", ASK, "--said-at", "2026-11-06T14:27:51Z"]
    env = {"ONEX_ASK_DONE_CHECK_OVERLAY": str(EXAMPLE)}
    assert (
        main(
            [*argv, "--check", ROW_CHECK],
            environ=env,
            floor=_floor(),
            rows=ledger,
            appender=ledger,
        )
        == 0
    )
    assert "verdict=TRUE" in capsys.readouterr().err
    unreadable = InMemoryFloorRead(ModelFloorRead(error="floor-unreadable:URLError"))
    code = main(
        [*argv, "--check", "floor:piece=6"],
        environ=env,
        floor=unreadable,
        rows=ledger,
        appender=ledger,
    )
    assert code == 1
    assert ledger.append_requests == {}
