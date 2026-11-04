"""The operator-waiting tick's list and acting row, in the node loops_check.py operator-waiting calls (OMN-19854).

RULING 2026-10-14T18:04:24Z read NOT-ACTING because the tick printed into a session and left no row. The tick's
end-to-end behaviour is tested in ``plugins/omni/skills/session-start/tests/test_operator_waiting.py``.
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime
from pathlib import Path

import yaml

from omnimarket.models.model_ask_state import ModelAskEntry, ModelAskStateReport
from omnimarket.models.model_operator_item_render import ModelOperatorItemRenderRequest
from omnimarket.nodes.node_operator_item_render_compute import (
    NodeOperatorItemRenderCompute,
)

ROOT = Path(__file__).resolve().parents[3]
NODE = "omnimarket.nodes.node_operator_waiting_tick_effect"
handler = importlib.import_module(f"{NODE}.handler")
models = importlib.import_module(f"{NODE}.models")
node = importlib.import_module(f"{NODE}.node")
NOW = datetime(2026, 10, 14, 20, 0, 0, tzinfo=UTC)
ASKS = [
    {
        "stamp": "2026-10-14T18:08:27Z",
        "id": "a1",
        "ticket": "OMN-20074",
        "line": "100",
        "text": "Keep the grant registry?",
    },
    {
        "stamp": "2026-10-11T23:24:11Z",
        "id": "b2",
        "ticket": "OMN-20413",
        "line": "101",
        "text": "Should it stay parked?",
    },
]


def writer(tmp_path: Path, rc: int = 0) -> Path:
    script = tmp_path / "writer.py"
    script.write_text(
        "import sys\n"
        f"open({str(tmp_path / 'calls.log')!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
        f"print('OK' if {rc} == 0 else 'REFUSED 65 grammar')\n"
        f"sys.exit({rc})\n",
        encoding="utf-8",
    )
    return script


def request(tmp_path: Path, asks, new=frozenset({"a1"}), rc: int = 0):
    entries = tuple(
        ModelAskEntry(
            ask_id=a["id"],
            item_id=a["id"],
            kind="question",
            said_at=datetime.fromisoformat(a["stamp"].replace("Z", "+00:00")),
            session_id="test",
            words=a["text"],
            subject="",
            confidence=1.0,
            state="owned",
            owner_lane="owner-" + a["id"],
            orphaned=True,
            tickets=(a["ticket"],),
            age_minutes=0,
        )
        for a in asks
    )
    report = ModelAskStateReport(
        schema_id="omni.ask-state/1", generated_at=NOW, since=NOW, asks=entries
    )
    config = yaml.safe_load(NodeOperatorItemRenderCompute.contract_path.read_text())[
        "config"
    ]
    return models.ModelOperatorWaitingRequest(
        items=ModelOperatorItemRenderRequest(
            report=report, new=new, now=NOW, config=config
        ),
        ledger="L.md",
        resolver=writer(tmp_path, rc),
        lane="orch-test",
        timeout=30.0,
    )


def test_lists_oldest_first_and_appends_one_status_row_with_the_tick_actor(tmp_path):
    out = node.NodeOperatorWaitingTickEffect().handle(request(tmp_path, ASKS))
    assert out.not_recorded is None
    assert out.lines == [
        "OPEN-ITEMS n=2 unowned=2 oldest=2d20h as_of=2026-10-14T20:00:00Z",
        "owner=owner-b2 (1)",
        "  2d20h kind=question owner=owner-b2 state=owned id=b2: Should it stay parked?",
        "owner=owner-a1 (1)",
        "  1h51m kind=question owner=owner-a1 state=owned NEW id=a1: Keep the grant registry?",
    ]
    (call,) = (tmp_path / "calls.log").read_text(encoding="utf-8").splitlines()
    assert "row STATUS --ledger L.md --lane orch-test" in call
    for cell in (
        "actor=script:operator-waiting-tick",
        "open=2",
        "new=1",
        "tickets=2",
        "oldest=2d20h",
    ):
        assert f"--kv {cell}" in call, call


def test_no_open_ask_writes_no_row_and_a_refused_write_is_one_note(tmp_path):
    empty = handler.HandlerOperatorWaitingTick().handle(
        request(tmp_path, [], frozenset())
    )
    assert empty.lines == [
        "OPEN-ITEMS n=0 unowned=0 oldest=none as_of=2026-10-14T20:00:00Z"
    ]
    assert not (tmp_path / "calls.log").exists()
    refused = handler.HandlerOperatorWaitingTick().handle(
        request(tmp_path, ASKS, rc=65)
    )
    assert refused.not_recorded == "the row writer exited 65: REFUSED 65 grammar"
    assert (
        refused.lines[-1]
        == "NOTE operator-waiting: the tick row was not recorded: the row writer exited 65: REFUSED 65 grammar"
    )
    assert refused.lines[0].startswith("OPEN-ITEMS n=2")


def test_the_contract_names_the_handler_and_the_actor_the_ruling_audit_reads():
    contract = yaml.safe_load(
        (
            ROOT
            / "src/omnimarket/nodes/node_operator_waiting_tick_effect/contract.yaml"
        ).read_text()
    )
    assert contract["node_type"] == "effect"
    assert contract["handler"]["class"] == "HandlerOperatorWaitingTick"
    assert handler.ACTOR == "script:operator-waiting-tick"
    assert "script:operator-waiting-tick" in contract["description"]


def test_ask_question_reads_the_writers_own_question_cell_and_never_the_words() -> None:
    """An ask is a MSG carrying a non-empty question= cell; a report (SENT-ON-BEHALF, MANUAL-ENQUEUE, ESCALATION-*)
    carries none by design and a sentence that only mentions a question is not an ask (OMN-16177)."""
    head = [
        "2026-10-21T10:24:46Z",
        "MSG",
        "from=orch",
        "to=operator",
        "id=2026-10-21T10:24:46Z-orch",
        "ticket=OMN-17427",
    ]
    assert node.ask_question(
        [*head, 'question="Close the PR or amend it?  Sources read: x."']
    ) == ("Close the PR or amend it? Sources read: x.")
    assert (
        node.ask_question(
            [
                *head,
                "SENT-ON-BEHALF (FACT) to a person https://example.invalid: sent a status",
            ]
        )
        == ""
    )
    assert (
        node.ask_question(
            [*head, "MANUAL-ENQUEUE (RULING 2026-10-10T04:08:23Z) re-queued 11 PRs"]
        )
        == ""
    )
    assert (
        node.ask_question(
            [*head, "ESCALATION-1 (RULING 2026-10-16T16:30:36Z) waiting 24h"]
        )
        == ""
    )
    assert (
        node.ask_question([*head, 'Should we merge? question="quoted in prose"']) == ""
    )
    assert node.ask_question([*head, 'question=""']) == ""
    assert (
        node.ask_question(
            ['question="in the stamp or type position is not a cell"', "MSG"]
        )
        == ""
    )
    long = node.ask_question([*head, 'question="' + "word " * 80 + '"'])
    assert len(long) == handler.ASK_TEXT_CUT
    assert long.endswith("...")
