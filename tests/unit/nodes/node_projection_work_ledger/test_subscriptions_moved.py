# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every consumer of the old row topics reads the accepted-row topic instead (OMN-25630, L12, AC3).

Golden chain: the projection, the render node and the inbox spool declare the accepted-row topic as their one
subscription, and the projection writer folds an accepted row (bootstrap or live, raw or typed) into the same
statements the old v1 topic produced. Error chain: a live accepted row with no ledger_seq is refused to the poison
dead-letter path. Failing control: the scanner that proves no node subscribes to an old topic does find a
subscription planted in a copy of a contract.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.events.enum_ledger_row_type import EnumLedgerRowType
from omnimarket.events.model_ledger_row_event import work_ledger_row_id
from omnimarket.models.model_work_ledger_accepted_row import (
    EnumAcceptedRowSource,
    ModelAcceptedRowEvent,
)
from omnimarket.nodes.node_agent_inbox_spool.node import load_topics as inbox_topics
from omnimarket.nodes.node_projection_work_ledger.contract_topics import (
    ALL_SUBSCRIBE_TOPICS,
    ROLLBACK_ROW_TOPICS,
    TOPIC_ACCEPTED_ROW,
)
from omnimarket.nodes.node_projection_work_ledger.handlers.handler_work_ledger_projection import (
    UnsequencedLiveRowError,
)
from omnimarket.nodes.node_work_ledger_render_effect.contract_topics import (
    ALL_SUBSCRIBE_TOPICS as RENDER_SUBSCRIBE_TOPICS,
)
from tests.unit.nodes.node_projection_work_ledger.accepted_row_support import (
    LEDGER_ID,
    history_event,
    live_event,
)
from tests.unit.nodes.node_projection_work_ledger.test_work_ledger_writer import (
    CLAIM,
    _FakeDb,
    _writer,
)

pytestmark = pytest.mark.unit

_NODES = Path(__file__).resolve().parents[4] / "src/omnimarket/nodes"
# the contract keys under which a node declares what it subscribes to
_SUBSCRIPTION_KEYS: tuple[tuple[str, ...], ...] = (
    ("event_bus", "subscribe_topics"),
    ("subscriptions", "topics"),
    ("caller_subscription", "topics"),
)


def _declared_subscriptions(contract: dict[str, Any]) -> set[str]:
    found: set[str] = set()
    for path in _SUBSCRIPTION_KEYS:
        value: Any = contract
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        if isinstance(value, list):
            found.update(t for t in value if isinstance(t, str))
    for consumed in contract.get("consumed_events") or []:
        if isinstance(consumed, dict) and isinstance(consumed.get("topic"), str):
            found.add(consumed["topic"])
    return found


def _old_topic_subscribers(contracts: dict[str, dict[str, Any]]) -> dict[str, set[str]]:
    old = set(ROLLBACK_ROW_TOPICS)
    return {
        node: hit
        for node, contract in contracts.items()
        if (hit := _declared_subscriptions(contract) & old)
    }


def _all_contracts() -> dict[str, dict[str, Any]]:
    contracts: dict[str, dict[str, Any]] = {}
    for path in sorted(_NODES.glob("node_*/contract.yaml")):
        loaded = yaml.load(
            path.read_text(), Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader)
        )
        if isinstance(loaded, dict):
            contracts[path.parent.name] = loaded
    return contracts


def test_subscriptions_moved_projection_render_and_inbox_read_the_accepted_row_topic() -> (
    None
):
    assert ALL_SUBSCRIBE_TOPICS == (TOPIC_ACCEPTED_ROW,)
    assert RENDER_SUBSCRIBE_TOPICS == (TOPIC_ACCEPTED_ROW,)
    assert inbox_topics() == (TOPIC_ACCEPTED_ROW,)
    assert _writer(_FakeDb()).subscribe_topics == [TOPIC_ACCEPTED_ROW]


def test_subscriptions_moved_no_node_subscribes_to_an_old_row_topic() -> None:
    contracts = _all_contracts()
    assert len(contracts) > 100, (
        "the scan must see the node contracts or it proves nothing"
    )
    assert len(ROLLBACK_ROW_TOPICS) == 22
    assert _old_topic_subscribers(contracts) == {}
    # the rollback path is a list of topics the republish command writes to, not a subscription of the node
    projection = contracts["node_projection_work_ledger"]
    assert sorted(projection["rollback"]["republish_old_topics"]) == sorted(
        topic for t in EnumLedgerRowType for topic in (t.topic, t.typed_topic)
    )


def test_subscriptions_moved_failing_control_a_planted_old_subscription_is_found() -> (
    None
):
    contracts = copy.deepcopy(_all_contracts())
    planted = EnumLedgerRowType.MSG.typed_topic
    contracts["node_agent_inbox_spool"]["caller_subscription"]["topics"] = [planted]
    contracts["node_work_ledger_render_effect"]["subscriptions"]["topics"].append(
        EnumLedgerRowType.CLAIM.topic
    )
    assert _old_topic_subscribers(contracts) == {
        "node_agent_inbox_spool": {planted},
        "node_work_ledger_render_effect": {EnumLedgerRowType.CLAIM.topic},
    }


def test_subscriptions_moved_the_writer_folds_a_live_accepted_row_like_the_old_v1_row() -> (
    None
):
    old, new = _FakeDb(), _FakeDb()
    _writer(old).handle(
        {"raw_row": CLAIM, "_topic": EnumLedgerRowType.CLAIM.topic, "ledger_seq": 7}
    )
    event = ModelAcceptedRowEvent(
        ledger_id=LEDGER_ID,
        row_id=work_ledger_row_id(CLAIM),
        raw_row=CLAIM,
        source=EnumAcceptedRowSource.LIVE,
        ledger_seq=7,
    )
    result = _writer(new).handle(
        {**event.model_dump(mode="json"), "_topic": TOPIC_ACCEPTED_ROW}
    )
    assert result == {"rows_upserted": 2}
    # the same statements and arguments, but the stored source column and the projected_at stamp
    for (old_sql, old_args), (new_sql, new_args) in zip(
        old.statements, new.statements, strict=True
    ):
        assert old_sql == new_sql
        assert len(old_args) == len(new_args)
    assert new.statements[0][1][9] == 7  # ledger_seq
    assert new.statements[0][1][7] == "live"  # source
    assert new.statements[1][1][0] == "claim:alpha"
    assert new.locks == [("work-ledger:rolling-work-ledger",)]


def test_subscriptions_moved_a_bootstrap_row_keeps_a_null_sequence() -> None:
    db = _FakeDb()
    event = history_event(1, ordinal=1)
    _writer(db).handle({**event.model_dump(mode="json"), "_topic": TOPIC_ACCEPTED_ROW})
    sql, args = db.statements[0]
    assert "work_ledger_rows" in sql
    assert args[9] is None
    assert args[7] == "bootstrap"
    assert db.holder_lookups == []


def test_subscriptions_moved_a_live_accepted_row_with_no_sequence_is_poison() -> None:
    db = _FakeDb()
    event = live_event(1, 1).model_copy(update={"ledger_seq": None})
    with pytest.raises(UnsequencedLiveRowError, match="requires a ledger_seq"):
        _writer(db).handle(
            {**event.model_dump(mode="json"), "_topic": TOPIC_ACCEPTED_ROW}
        )
    assert db.statements == []


def test_subscriptions_moved_a_tampered_accepted_row_is_refused_before_any_statement() -> (
    None
):
    db = _FakeDb()
    event = live_event(1, 1)
    body = {**event.model_dump(mode="json"), "_topic": TOPIC_ACCEPTED_ROW}
    body["raw_row"] = event.raw_row.replace("step", "tampered")
    with pytest.raises(Exception, match="not the hash"):
        _writer(db).handle(body)
    assert db.statements == []
