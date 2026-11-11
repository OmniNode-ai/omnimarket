# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seam of join J6 for the two codes the J5 join now closes its chain with (OMN-23591).

The producer is the real J5 join handler: when an append raises after the start, or the run's deadline passes, it writes a start
and a typed failure (``ledger_join_interrupted`` or ``ledger_join_deadline_exceeded``) to the emission journal. The consumer is
the join refusal node over the J6 entry of its overlay (``journal_upstream`` names the producer's node), and the floor's own
receipt fold judges the join, as in ``test_join_j6_journal_seam``. Neither side is stubbed.

* golden: each closed chain is answered once with piece 7's ``upstream_`` refusal and the fold reads J6 as an error join with no
  finding; before the node runs the fold reads ``downstream_missing_typed_failure``.
* control: an overlay without the journal rule leaves the chain unanswered, so the test can fail for real.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import pytest

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from omnimarket.nodes.node_work_ledger_append_effect.adapters.run_deadline import (
    run_deadline,
)
from tests.nodes.node_join_refusal_effect.test_join_j6_journal_seam import (
    answer,
    codes,
    control_overlay,
    receipts,
    refusals,
)
from tests.unit.nodes.node_work_ledger_append_effect.test_join_j5 import (
    POLICY,
    _request,
    _Rig,
)

pytestmark = pytest.mark.unit


def interrupted_chain() -> tuple[str, InMemoryEmissionSink]:
    rig = _Rig()
    cid = uuid.uuid4().hex
    rig.upstream_chain(cid, resolves=True)

    def raises(request: Any, *, writer_principal: Any = None) -> Any:
        raise RuntimeError("the append command blew up")

    rig.append.handle = raises
    rig.join.handle(_request(cid))
    return cid, rig.sink


def deadline_chain() -> tuple[str, InMemoryEmissionSink]:
    rig = _Rig()
    cid = uuid.uuid4().hex
    rig.upstream_chain(cid, resolves=True)

    def blocks(request: Any, *, writer_principal: Any = None) -> Any:
        time.sleep(10)

    rig.append.handle = blocks
    with run_deadline(1):
        rig.join.handle(_request(cid))
    return cid, rig.sink


CHAINS = [
    pytest.param(
        interrupted_chain, lambda: POLICY.interrupted_failure_code, id="interrupted"
    ),
    pytest.param(
        deadline_chain, lambda: POLICY.deadline_exceeded_failure_code, id="deadline"
    ),
]


@pytest.mark.parametrize(("build", "code"), CHAINS)
def test_golden_the_floor_reads_the_closed_chain_unanswered_before_the_node_runs(
    build: Any, code: Any
) -> None:
    _, sink = build()
    found = receipts(InMemoryLedger([]), sink)
    assert codes(found) == ["downstream_missing_typed_failure"]
    assert found.joins[0].state == "broken"


@pytest.mark.parametrize(("build", "code"), CHAINS)
def test_golden_the_closed_chain_is_answered_once_with_piece_7s_typed_refusal(
    build: Any, code: Any
) -> None:
    cid, sink = build()
    source = InMemoryLedger([])
    assert answer(source, sink).ok
    [refusal] = refusals(sink)
    assert refusal.failure_code == "upstream_" + code()
    assert refusal.correlation_id == cid
    found = receipts(source, sink)
    assert codes(found) == []
    assert found.joins[0].state == "error_only"
    answer(source, sink)
    assert len(refusals(sink)) == 1


@pytest.mark.parametrize(("build", "code"), CHAINS)
def test_control_an_overlay_without_the_journal_rule_leaves_the_closed_chain_unanswered(
    build: Any, code: Any
) -> None:
    _, sink = build()
    source = InMemoryLedger([])
    answer(source, sink, overlay=control_overlay())
    assert refusals(sink) == []
    assert codes(receipts(source, sink, overlay=control_overlay())) == [
        "downstream_missing_typed_failure"
    ]
