# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reads every topic a node contract declares, in every shape (OMN-19998).

The node is the single reader of a contract's declared topics.
It carries no effect: it only reports what the contracts declare.
"""

from __future__ import annotations

import re
from typing import Any

from omnimarket.models.model_contract_topics import (
    EnumContractTopicShape,
    ModelContractTopicReport,
    ModelContractTopicRequest,
    ModelContractTopicUse,
    ModelRejectedLiteral,
    ModelUnreadableContract,
)


def _extract_topics(
    document: dict[str, Any],
) -> dict[EnumContractTopicShape, list[str]]:
    """Extracts topic literals from a contract document across all known shapes."""
    topics: dict[EnumContractTopicShape, list[str]] = {}

    def add(shape: EnumContractTopicShape, value: Any) -> None:
        if isinstance(value, str):
            topics.setdefault(shape, []).append(value)

    # EVENT_BUS_SUBSCRIBE, EVENT_BUS_PUBLISH, EVENT_BUS_DLQ
    event_bus = document.get("event_bus")
    if isinstance(event_bus, dict):
        for key, shape in [
            ("subscribe_topics", EnumContractTopicShape.EVENT_BUS_SUBSCRIBE),
            ("publish_topics", EnumContractTopicShape.EVENT_BUS_PUBLISH),
            ("dlq_topics", EnumContractTopicShape.EVENT_BUS_DLQ),
        ]:
            val = event_bus.get(key)
            if isinstance(val, list):
                for item in val:
                    add(shape, item)

    # PROJECTION_API
    projection_api = document.get("projection_api")
    if isinstance(projection_api, dict) and projection_api.get("expose") is True:
        exposures = projection_api.get("exposures")
        if isinstance(exposures, list):
            for exposure in exposures:
                if isinstance(exposure, dict) and exposure.get("bus_backed") is True:
                    add(EnumContractTopicShape.PROJECTION_API, exposure.get("topic"))
        elif (
            "exposures" not in projection_api
            and projection_api.get("bus_backed") is True
        ):
            add(EnumContractTopicShape.PROJECTION_API, projection_api.get("topic"))

    # CONSUMED_EVENTS, EXTERNALLY_PRODUCED, EXTERNALLY_CONSUMED, PUBLISHED_EVENTS
    for key, shape in [
        ("consumed_events", EnumContractTopicShape.CONSUMED_EVENTS),
        ("externally_produced_topics", EnumContractTopicShape.EXTERNALLY_PRODUCED),
        ("externally_consumed_topics", EnumContractTopicShape.EXTERNALLY_CONSUMED),
        ("published_events", EnumContractTopicShape.PUBLISHED_EVENTS),
    ]:
        val = document.get(key)
        if isinstance(val, list):
            for item in val:
                if isinstance(item, dict):
                    add(shape, item.get("topic"))
                else:
                    add(shape, item)

    # COMMAND
    runtime_dispatch = document.get("runtime_dispatch")
    if isinstance(runtime_dispatch, dict):
        add(EnumContractTopicShape.COMMAND, runtime_dispatch.get("command_topic"))

    # TERMINAL
    if isinstance(runtime_dispatch, dict):
        terminal_events = runtime_dispatch.get("terminal_events")
        if isinstance(terminal_events, dict):
            for value in terminal_events.values():
                add(EnumContractTopicShape.TERMINAL, value)
    add(EnumContractTopicShape.TERMINAL, document.get("terminal_event"))

    # HANDLER_ROUTING
    handler_routing = document.get("handler_routing")
    if isinstance(handler_routing, dict):
        handlers = handler_routing.get("handlers")
        if isinstance(handlers, list):
            for handler in handlers:
                if isinstance(handler, dict):
                    add(EnumContractTopicShape.HANDLER_ROUTING, handler.get("topic"))

    # TOPICS_MAP
    topics_map = document.get("topics")
    if isinstance(topics_map, dict):
        for value in topics_map.values():
            add(EnumContractTopicShape.TOPICS_MAP, value)

    return topics


class HandlerContractTopicReader:
    """Handles reading contract topics and compiling the report."""

    def handle(self, request: ModelContractTopicRequest) -> ModelContractTopicReport:
        """Processes the request and returns the compiled report."""
        try:
            pattern = re.compile(request.rule.topic_pattern)
        except re.error as error:
            raise ValueError(
                f"topic_pattern {request.rule.topic_pattern!r} is not a valid regex: {error}"
            ) from error

        uses: set[ModelContractTopicUse] = set()
        rejected: set[ModelRejectedLiteral] = set()
        unreadable: set[ModelUnreadableContract] = set()
        topics_by_shape: dict[EnumContractTopicShape, set[str]] = {}
        contracts_read = 0

        for doc in request.documents:
            if doc.read_error is not None:
                unreadable.add(
                    ModelUnreadableContract(
                        contract=doc.name,
                        path=doc.path,
                        reason=doc.read_error,
                    )
                )
                continue

            if not isinstance(doc.document, dict):
                unreadable.add(
                    ModelUnreadableContract(
                        contract=doc.name,
                        path=doc.path,
                        reason="contract is not a mapping",
                    )
                )
                continue

            contracts_read += 1
            extracted = _extract_topics(doc.document)

            for shape, literals in extracted.items():
                for literal in literals:
                    if pattern.fullmatch(literal):
                        uses.add(
                            ModelContractTopicUse(
                                contract=doc.name,
                                topic=literal,
                                shape=shape,
                            )
                        )
                        topics_by_shape.setdefault(shape, set()).add(literal)
                    else:
                        rejected.add(
                            ModelRejectedLiteral(
                                contract=doc.name,
                                shape=shape,
                                literal=literal,
                            )
                        )

        all_topics = set()
        for topics in topics_by_shape.values():
            all_topics.update(topics)

        return ModelContractTopicReport(
            uses=tuple(
                sorted(uses, key=lambda u: (u.contract, u.shape.value, u.topic))
            ),
            topics_by_shape={
                shape.value: tuple(sorted(topics))
                for shape, topics in sorted(
                    topics_by_shape.items(), key=lambda x: x[0].value
                )
                if topics
            },
            all_topics=tuple(sorted(all_topics)),
            rejected=tuple(
                sorted(rejected, key=lambda r: (r.contract, r.shape.value, r.literal))
            ),
            unreadable=tuple(sorted(unreadable, key=lambda u: (u.contract, u.path))),
            contracts_read=contracts_read,
        )
