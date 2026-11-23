# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing tick's caller of node_running_read_due_compute (OMN-26549).

The due node is pure and returns ledger append requests; it appends nothing and had no caller. At a tick this
step maps each merged runtime-affecting pull request the request carries to ``write_due_row``, and, when the
request carries the ledger text, asks ``read_due_rows`` at the tick's own clock. Each append request goes to the
tick's ledger appender, the door to the work ledger append effect (append_if_absent keyed on the due id), in an
enabled act mode only. Observe and replay name the request they would send and append nothing.

The append effect takes a request on the writer topic of the publishing broker login; the topic its contract still
names as ``command_topic`` is the unattributed legacy one the serve refuses. WriterTopicLedgerAppender therefore
publishes on the writer topic the effect's own ``writer_topics`` template derives for the tick's login.

A request goes out once: the due id is skipped when the ledger text already holds it or this step already sent
its request, and an alarm request is keyed on its due id by the node. A request that failed to publish is not
remembered, so the next tick sends it again.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import yaml

from omnimarket.models.running_read_due import (
    ModelRunningReadDueRequest,
)
from omnimarket.models.topic_provision.model_writer_topics_template import (
    ModelWriterTopicsTemplate,
)
from omnimarket.models.work_ledger_append import ModelWorkLedgerAppendRequest
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.tick_ports import (
    ProtocolTickLedgerAppender,
    ProtocolTickPublisher,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_orchestrator import (
    ModelTickRequest,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_running_read import (
    ModelTickRunningRead,
    ModelTickRunningReadItem,
)
from omnimarket.nodes.node_running_read_due_compute.handlers.handler_running_read_due import (
    HandlerRunningReadDue,
)

NODES_ROOT = Path(__file__).resolve().parents[2]
APPEND_NODE = "node_work_ledger_append_effect"


def writer_topic_for(principal: str) -> str:
    """The append effect's command topic for one writer login, from the effect's own contract template."""
    contract = yaml.safe_load(
        (NODES_ROOT / APPEND_NODE / "contract.yaml").read_text(encoding="utf-8")
    )
    return ModelWriterTopicsTemplate.model_validate(
        contract["writer_topics"]
    ).topic_for(principal)


class WriterTopicLedgerAppender:
    """ProtocolTickLedgerAppender over the tick's publisher: the request goes on the login's writer topic."""

    def __init__(self, publisher: ProtocolTickPublisher, *, principal: str) -> None:
        self._publisher = publisher
        self._topic = writer_topic_for(principal)

    @property
    def topic(self) -> str:
        return self._topic

    def append(self, request: ModelWorkLedgerAppendRequest) -> None:
        self._publisher.publish(self._topic, request)


def _on_ledger(ledger_text: str | None, due_id: str) -> bool:
    """Whether a ledger row carries the due id as its own id cell."""
    if not ledger_text:
        return False
    return (
        re.search(rf"\|\s*id={re.escape(due_id)}\s*(\||$)", ledger_text, re.M)
        is not None
    )


class TickRunningRead:
    """Stateful only in the request ids it has sent, so a repeat tick in one process sends nothing twice."""

    def __init__(
        self,
        *,
        appender: ProtocolTickLedgerAppender,
        node: HandlerRunningReadDue | None = None,
    ) -> None:
        self._appender = appender
        self._node = node or HandlerRunningReadDue()
        self._sent: set[UUID] = set()

    def run(
        self, request: ModelTickRequest, *, sends: bool
    ) -> ModelTickRunningRead | None:
        """The step's account for this tick; None when the tick carries no merge and no ledger text."""
        if not request.merged_prs and request.running_read_ledger_text is None:
            return None
        items: list[ModelTickRunningReadItem] = []
        refusals: list[str] = []
        ledger_text = request.running_read_ledger_text
        for merged in request.merged_prs:
            written = self._node.handle(
                ModelRunningReadDueRequest(operation="write_due_row", merged=merged)
            )
            if written.refusal:
                refusals.append(written.refusal)
            if written.append_request is None or written.due_id is None:
                continue
            if _on_ledger(ledger_text, written.due_id):
                continue
            item = self._request(
                "due_row", written.due_id, written.append_request, sends
            )
            if item is not None:
                items.append(item)
        open_due_ids: tuple[str, ...] = ()
        blocked: tuple[str, ...] = ()
        if ledger_text is not None:
            read = self._node.handle(
                ModelRunningReadDueRequest(
                    operation="read_due_rows",
                    ledger_text=ledger_text,
                    now=_aware(request.facts.observed_at),
                )
            )
            if read.refusal:
                refusals.append(read.refusal)
            open_due_ids = tuple(read.open_due_ids)
            blocked = tuple(read.blocked_tickets)
            for alarm in read.alarms:
                item = self._request("alarm", alarm.due_id, alarm.append_request, sends)
                if item is not None:
                    items.append(item)
        return ModelTickRunningRead(
            items=tuple(items),
            open_due_ids=open_due_ids,
            blocked_tickets=blocked,
            refusals=tuple(refusals),
        )

    def _request(
        self,
        action: str,
        due_id: str,
        append_request: ModelWorkLedgerAppendRequest,
        sends: bool,
    ) -> ModelTickRunningReadItem | None:
        if append_request.request_id in self._sent:
            return None
        fields = {
            "action": action,
            "due_id": due_id,
            "request_id": append_request.request_id,
        }
        if not sends:
            return ModelTickRunningReadItem.model_validate(
                {**fields, "would_send": True}
            )
        try:
            self._appender.append(append_request)
        except (
            Exception
        ) as exc:  # a failed append is named and retried next tick, never a failed tick
            return ModelTickRunningReadItem.model_validate(
                {**fields, "detail": f"append raised {type(exc).__name__}"}
            )
        self._sent.add(append_request.request_id)
        return ModelTickRunningReadItem.model_validate({**fields, "sent": True})


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


__all__: list[str] = [
    "TickRunningRead",
    "WriterTopicLedgerAppender",
    "writer_topic_for",
]
