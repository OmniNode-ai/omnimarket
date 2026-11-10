# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""List the topics consumed with no producer found, with each topic's consumers and graph evidence (OMN-23151).

Pure: the seam graph, the literal mentions the caller scanned for, and two instants. A topic of the listed class is on
the list when a contract subscribes to it or code subscribes to it, and nothing produces it: no contract publishes it,
no code sends to it, and no source other than its consumers spells it. This is a candidate list (the graph covers
literals and contract declarations only), so each entry names the evidence it was listed on; the join tests settle it.

The result carries one append-only seam row, so the floor reads the list's age from the row's ``probed_at``. A graph with
no edges and no observations proves nothing, so its row reads UNKNOWN, never WORKING.
"""

from __future__ import annotations

from collections import defaultdict

from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.models.seam_reading.model_required_sender import EnumSeamTrigger
from omnimarket.nodes.node_seam_reading_compute.models.model_no_producer_topic import (
    ModelNoProducerTopic,
    ModelNoProducerTopicList,
    ModelNoProducerTopicRequest,
)
from omnimarket.nodes.node_seam_reading_compute.models.model_seam_row import (
    ModelSeamRow,
)
from omnimarket.seams.models.model_seam_graph import EnumSeamGraphObservationKind


class HandlerNoProducerTopics:
    """Render the no-producer topic list of a seam graph."""

    def handle(self, request: ModelNoProducerTopicRequest) -> ModelNoProducerTopicList:
        graph = request.graph
        consumers: dict[str, set[str]] = defaultdict(set)
        produced: set[str] = set()
        for edge in graph.edges:
            if edge.role == "producer":
                produced.add(edge.topic)
            else:
                consumers[edge.topic].add(edge.source_contract_path)
        for observation in graph.code_observations:
            if observation.kind is EnumSeamGraphObservationKind.PRODUCER_SEND:
                produced.add(observation.value)
            elif observation.kind is EnumSeamGraphObservationKind.CONSUMER_SUBSCRIBE:
                consumers[observation.value].add(observation.source_path)
        mentions: dict[str, set[str]] = defaultdict(set)
        for mention in request.literal_mentions:
            mentions[mention.topic].add(mention.source_path)

        topics: list[ModelNoProducerTopic] = []
        for topic in sorted(consumers):
            if request.topic_segment not in topic or topic in produced:
                continue
            elsewhere = mentions.get(topic, set()) - consumers[topic]
            if elsewhere:
                continue
            topics.append(
                ModelNoProducerTopic(
                    topic=topic,
                    consumers=tuple(sorted(consumers[topic])),
                    evidence=(
                        f"consumers={len(consumers[topic])} producer-edges=0 "
                        "producer-sends=0 other-source-mentions=0 "
                        f"graph-edges={len(graph.edges)} "
                        f"graph-observations={len(graph.code_observations)}"
                    ),
                )
            )

        read_nothing = not graph.edges and not graph.code_observations
        verdict = (
            EnumReadingVerdict.UNKNOWN if read_nothing else EnumReadingVerdict.WORKING
        )
        reason = "graph-empty" if read_nothing else f"listed={len(topics)}"
        row = ModelSeamRow.of(
            seam=request.seam,
            trigger=EnumSeamTrigger.ON_DEMAND,
            verdict=verdict,
            reason=reason,
            probed_at=request.now,
        )
        return ModelNoProducerTopicList(
            topics=tuple(topics),
            count=len(topics),
            graph_read_at=request.graph_read_at,
            generated_at=request.now,
            age_seconds=(request.now - request.graph_read_at).total_seconds(),
            row=row,
        )
