# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ledger row grammar node's running_path_share operation (OMN-24535).

The share of a ledger text's done and handed-off TERMINAL rows that carry the running-path
cell, over a payload with every row judged (``running_path_cell.EVERY_ROW_STAMP``), so the
2026-11-17 reading is one node call and not a script. The answer never reads
``ONEX_LEDGER_REQUIRE_RUNNING_PATH``; every test deletes the variable to prove it. Golden
chain: a request through the registered contract on the in-memory bus, and the handler's
answer for three rows. Error chain: degenerate payloads confirm nothing. The failing
control is the real 2026-11-14 remote-lane TERMINAL, kept verbatim, which closed a lane
done with no cell naming what runs the change.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import omnimarket.nodes.node_ledger_row_grammar_compute as node_package
from omnimarket.handlers import ledger_running_path_cell as running_path_cell
from omnimarket.nodes.node_ledger_row_grammar_compute.handlers.handler_ledger_row_grammar import (
    HandlerLedgerRowGrammar,
)
from omnimarket.nodes.node_ledger_row_grammar_compute.models.model_ledger_row_grammar import (
    ModelLedgerRowGrammarRequest,
    ModelLedgerRowGrammarResult,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit

NODE_DIR = Path(node_package.__file__).parent
TS = "2026-11-14T19:33:58Z"
ENFORCE = running_path_cell.ENFORCE_FROM_ENV
LINE = "2026-11-14T19:30Z node_x_effect tick refused 3 rows"
GOVERNED = (
    f"{TS} | TERMINAL | lane=lane-a | friction=none | "
    "actor=claude:sonnet:remote-lane | model=claude-sonnet | "
    "delegated=0 delegation_reason=no-text-or-code"
)
MACHINE = (
    f"{TS} | TERMINAL | lane=lane-a | friction=none | "
    "actor=script:landing-controller | model=none"
)
STATUS = (
    "2026-10-15T06:00:00Z | STATUS | lane=lane-a | ticket=OMN-1234 | "
    "actor=codex | model=gpt | halfway"
)
GOLDEN_PAYLOAD = "\n".join(
    (
        f"{GOVERNED} | outcome=done | running_path=node_x_effect | running_line={LINE}",
        f"{GOVERNED} | outcome=done",
        f"{MACHINE} | outcome=done",
    )
)

# A real TERMINAL row of 2026-11-14, kept verbatim except the principal name (renamed): outcome=done with a receipt and a
# host closeout and no cell naming what runs the change.
REAL_ROW_NO_READING = (
    "2026-11-14T19:33:58Z | TERMINAL | lane=fill-ticket-OMN-23215-20261114T1908Z | "
    "principal=dev-lane-principal | ticket=OMN-23215 | "
    "actor=claude:sonnet:remote-lane | model=claude-sonnet | friction=none | "
    "closes-CLAIM=2026-11-14T19:32:27Z | outcome=done | host=h053 | "
    "run=rlane-fill-ticket-OMN-23215-20261114T1908Z-9ec20d | "
    "delegated=0 delegation_reason=no-text-or-code | "
    "req=5eb94e1d-3ffd-4ff2-8ae4-4f6d4af891f1 | via=bus:h200 | remote lane on h053 "
    "ended status=done engine_rc=0; receipt "
    "rlane-fill-ticket-OMN-23215-20261114T1908Z-9ec20d.json on the launching host; "
    "host closeout: the lane made no worktrees"
)
HANDLER = HandlerLedgerRowGrammar()


def ask(**fields: Any) -> ModelLedgerRowGrammarResult:
    return HANDLER.handle(ModelLedgerRowGrammarRequest.model_validate(fields))


def test_golden_three_rows_confirm_two_carry_one_share_half(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ENFORCE, raising=False)
    result = ask(operation="running_path_share", payload=GOLDEN_PAYLOAD)
    assert result.admitted is True
    assert result.confirming_rows == 2
    assert result.carrying_rows == 1
    assert result.share == 0.5


def test_error_chain_an_empty_payload_confirms_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ENFORCE, raising=False)
    result = ask(operation="running_path_share", payload="")
    assert result.admitted is True
    assert result.confirming_rows == 0
    assert result.carrying_rows == 0
    assert result.share is None


def test_error_chain_status_rows_and_a_partial_outcome_confirm_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ENFORCE, raising=False)
    result = ask(
        operation="running_path_share",
        payload=f"{STATUS}\n{GOVERNED} | outcome=partial",
    )
    assert result.confirming_rows == 0
    assert result.carrying_rows == 0
    assert result.share is None


def test_failing_control_the_real_2026_11_14_row_shares_zero_of_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ENFORCE, raising=False)
    result = ask(operation="running_path_share", payload=REAL_ROW_NO_READING)
    assert result.confirming_rows == 1
    assert result.carrying_rows == 0
    assert result.share == 0.0


def test_failing_control_its_twin_with_the_honest_cell_shares_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ENFORCE, raising=False)
    twin = REAL_ROW_NO_READING.replace(
        " | delegated=0",
        " | running_path=none-ci-only:fill-lane-opens-no-pull-request | delegated=0",
    )
    result = ask(operation="running_path_share", payload=twin)
    assert result.confirming_rows == 1
    assert result.carrying_rows == 1
    assert result.share == 1.0


async def _run(tmp_path: Path, payload: dict[str, Any], name: str) -> RuntimeLocal:
    input_path = tmp_path / f"{name}.json"
    input_path.write_text(json.dumps(payload))
    runtime = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=input_path,
        state_root=tmp_path / f"state-{name}",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    await runtime.run_async()
    return runtime


@pytest.mark.asyncio
async def test_golden_chain_over_the_bus_answers_the_share(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ENFORCE, raising=False)
    runtime = await _run(
        tmp_path,
        {"operation": "running_path_share", "payload": GOLDEN_PAYLOAD},
        "share",
    )
    result = runtime.handler_result
    assert isinstance(result, ModelLedgerRowGrammarResult)
    assert result.admitted is True
    assert result.confirming_rows == 2
    assert result.carrying_rows == 1
    assert result.share == 0.5
    assert (
        ModelLedgerRowGrammarResult.model_validate_json(result.model_dump_json())
        == result
    )


def test_an_outcome_sharing_its_pipe_cell_with_other_keys_is_counted() -> None:
    """The bypass a one-token read would leave: the share must not skip a row whose outcome is not its own cell."""
    row = (
        "2026-11-14T19:33:58Z | TERMINAL | lane=lane-a | friction=none | "
        "actor=claude:sonnet:x | model=claude-sonnet | "
        "outcome=done delegated=0 delegation_reason=no-text-or-code"
    )
    result = HandlerLedgerRowGrammar().handle(
        ModelLedgerRowGrammarRequest(operation="running_path_share", payload=row)
    )
    assert (result.confirming_rows, result.carrying_rows, result.share) == (1, 0, 0.0)
