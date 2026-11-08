# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The no-producer topic list: a topic consumed with no producer found is listed with its consumer and evidence (OMN-23151 AC1).

The golden leg lists exactly the event topics a graph consumes and nothing produces; the error legs show what keeps a
topic off the list (a producer edge, a producer send, another source that spells it, a topic of another class) and that
a graph which read nothing reads UNKNOWN, never WORKING. The committed list is checked against the contracts of this
repository, so a consumer that stopped subscribing to its topic fails here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_seam_reading_compute.adapters.inmemory_seam_row_store import (
    InMemorySeamRowStore,
)
from omnimarket.nodes.node_seam_reading_compute.handlers.handler_no_producer_topics import (
    HandlerNoProducerTopics,
)
from omnimarket.nodes.node_seam_reading_compute.handlers.handler_seam_row_append import (
    HandlerSeamRowAppend,
)
from omnimarket.nodes.node_seam_reading_compute.models.model_no_producer_topic import (
    ModelNoProducerTopicRequest,
    ModelTopicLiteralMention,
)
from omnimarket.nodes.node_seam_reading_compute.models.model_seam_row import (
    EnumRowAppendStatus,
    ModelSeamRowAppendRequest,
)
from omnimarket.seams.models.model_seam_graph import (
    EnumSeamGraphObservationKind,
    ModelSeamGraphCodeObservation,
    ModelSeamGraphEdgeDeclaration,
    ModelSeamGraphV1,
)

pytestmark = pytest.mark.unit

NODE_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = NODE_DIR.parents[3]
LIST_FILE = NODE_DIR / "no_producer_topics.yaml"
READ_AT = datetime(2026, 11, 8, 22, 40, tzinfo=UTC)
NOW = READ_AT + timedelta(seconds=90)
SEGMENT = ".evt."
SEAM = "fixture-seam"

ORPHAN = "x.evt.fixture.orphan.v1"
EDGE_PRODUCED = "x.evt.fixture.edge-produced.v1"
SEND_PRODUCED = "x.evt.fixture.send-produced.v1"
SPELLED = "x.evt.fixture.spelled-elsewhere.v1"
COMMAND = "x.cmd.fixture.command.v1"


def _edge(topic: str, role: str, path: str) -> ModelSeamGraphEdgeDeclaration:
    return ModelSeamGraphEdgeDeclaration(
        edge_id=topic, seam=topic, role=role, source_contract_path=path, topic=topic
    )


def _graph(*, with_send: bool = True) -> ModelSeamGraphV1:
    observations = (
        (
            ModelSeamGraphCodeObservation(
                source_path="a/producer.py",
                kind=EnumSeamGraphObservationKind.PRODUCER_SEND,
                value=SEND_PRODUCED,
                line_number=3,
            ),
        )
        if with_send
        else ()
    )
    return ModelSeamGraphV1(
        discovery_roots=("a",),
        edges=(
            _edge(ORPHAN, "consumer", "a/consumer_one/contract.yaml"),
            _edge(EDGE_PRODUCED, "consumer", "a/consumer_two/contract.yaml"),
            _edge(EDGE_PRODUCED, "producer", "a/producer_two/contract.yaml"),
            _edge(SEND_PRODUCED, "consumer", "a/consumer_three/contract.yaml"),
            _edge(SPELLED, "consumer", "a/consumer_four/contract.yaml"),
            _edge(COMMAND, "consumer", "a/consumer_five/contract.yaml"),
        ),
        code_observations=observations,
    )


def _request(
    graph: ModelSeamGraphV1, mentions: tuple[ModelTopicLiteralMention, ...] = ()
) -> ModelNoProducerTopicRequest:
    return ModelNoProducerTopicRequest(
        graph=graph,
        literal_mentions=mentions,
        topic_segment=SEGMENT,
        seam=SEAM,
        graph_read_at=READ_AT,
        now=NOW,
    )


def test_no_producer_topic_list_names_the_orphan_with_its_consumer_and_evidence() -> (
    None
):
    mention = ModelTopicLiteralMention(topic=SPELLED, source_path="a/constants.py")
    listed = HandlerNoProducerTopics().handle(_request(_graph(), (mention,)))
    assert [t.topic for t in listed.topics] == [ORPHAN]
    assert listed.count == 1
    [entry] = listed.topics
    assert entry.consumers == ("a/consumer_one/contract.yaml",)
    assert "producer-edges=0" in entry.evidence
    assert "graph-edges=6" in entry.evidence
    assert listed.age_seconds == 90.0
    assert listed.row.verdict is EnumReadingVerdict.WORKING
    assert listed.row.reason == "listed=1"


def test_no_producer_topic_list_each_producer_kind_keeps_a_topic_off_the_list() -> None:
    names = {
        t.topic for t in HandlerNoProducerTopics().handle(_request(_graph())).topics
    }
    assert names == {ORPHAN, SPELLED}
    # Control: without the producer send the same topic is listed, so the send is what kept it off.
    without_send = HandlerNoProducerTopics().handle(_request(_graph(with_send=False)))
    assert SEND_PRODUCED in {t.topic for t in without_send.topics}
    # Control: the topic class is a request setting, not a code default.
    commands = HandlerNoProducerTopics().handle(
        _request(_graph()).model_copy(update={"topic_segment": ".cmd."})
    )
    assert [t.topic for t in commands.topics] == [COMMAND]


def test_no_producer_topic_list_graph_that_read_nothing_reads_unknown() -> None:
    listed = HandlerNoProducerTopics().handle(_request(ModelSeamGraphV1()))
    assert listed.count == 0
    assert listed.row.verdict is EnumReadingVerdict.UNKNOWN
    assert listed.row.reason == "graph-empty"


def test_no_producer_topic_list_row_is_appended_once_and_carries_the_age() -> None:
    store = InMemorySeamRowStore()
    appender = HandlerSeamRowAppend(store=store)
    listed = HandlerNoProducerTopics().handle(_request(_graph()))
    request = ModelSeamRowAppendRequest(correlation_id="np-list", rows=(listed.row,))
    assert appender.handle(request).statuses == (EnumRowAppendStatus.APPENDED,)
    assert appender.handle(request).statuses == (EnumRowAppendStatus.ALREADY_APPENDED,)
    [stored] = store.rows(SEAM)
    assert NOW - stored.probed_at == timedelta(0)
    assert stored.probed_at - READ_AT == timedelta(seconds=listed.age_seconds)


def _committed() -> dict[str, object]:
    loaded = yaml.safe_load(LIST_FILE.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_no_producer_topic_list_committed_file_names_a_consumer_and_evidence_for_each() -> (
    None
):
    doc = _committed()
    topics = doc["topics"]
    assert isinstance(topics, list)
    assert doc["count"] == len(topics) > 0
    assert len({t["topic"] for t in topics}) == len(topics)
    for entry in topics:
        assert doc["topic_segment"] in entry["topic"]
        assert entry["consumers"], entry["topic"]
        assert "producer-edges=0" in entry["evidence"], entry["topic"]


def test_no_producer_topic_list_committed_consumers_in_this_repo_still_subscribe() -> (
    None
):
    checked = 0
    for entry in _committed()["topics"]:  # type: ignore[attr-defined]
        for consumer in entry["consumers"]:
            prefix = "omnimarket/"
            if not consumer.startswith(prefix):
                continue
            contract = yaml.safe_load(
                (REPO_ROOT / consumer.removeprefix(prefix)).read_text(encoding="utf-8")
            )
            assert entry["topic"] in contract["event_bus"]["subscribe_topics"], consumer
            checked += 1
    assert checked > 0
