# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The node assembles its own evaluation (OMN-16778, operator-approved redesign).

Canonical definition-B shape
----------------------------
``handle(request: ModelConsumerFlowStallAlertTrigger) ->
ModelConsumerFlowStallAlertEvaluation``: typed payload in, typed payload out.
No event envelope in the core, no ``ModelHandlerOutput``.

What changed, and why
---------------------
The first cut declared a fully-assembled request as this node's ``input_model``:
one consumer, one topic, a trailing window history, and the policy — all
supplied by a caller.  **No such caller exists, and none could.**  The only
publisher of the trigger topic is the runtime's generic projection emitter,
which carries the projection's batch ack.  Live on the ``.201`` dev lane at
2026-08-29T01:3x that read as ``ValidationError: 8 validation errors for
ModelConsumerFlowStallAlertRequest`` on every message, 94 times in two minutes,
each one DLQ-routed with ``boundary_swallow_prevented``.  The node was reached
and could not act.

The operator approved the redesign on 2026-08-28 ("Approve redesign"):

* **Policy self-assembles.**  Thresholds are read from this node's own
  ``contract.yaml`` at construction.  This is unchanged in substance from the
  original AC2 — thresholds were always contract data — but the *loading* moved
  from the caller into the node, so nothing outside the node needs to know they
  exist.
* **The node reads its own windows.**  ``windows_source`` in the contract names
  the relation, the topology binding and the DSN variable; the read happens at
  this effect's own boundary through :class:`ConsumerFlowWindowReader`.
* **The trigger shrank to the shape that is actually produced.**

What did NOT change: thresholds still live only in ``contract.yaml``, and
``test_no_threshold_literal_lives_in_python`` still bans a numeric default
anywhere under this node.

Why an EFFECT may read a database and publish
---------------------------------------------
This is an EFFECT node, not a reducer.  ``db_io.db_tables`` declares governed
access — which relation, which schema, which role — and the runtime wires a
typed def-B handler that declares a concrete input model onto the *typed*
dispatch arm, leaving that declaration as a statement about access rather than
about dispatch shape (``handler_wiring.py``, OMN-16767).  The Slack command
goes out through the injected ``event_publisher``, which the runtime scopes to
this contract's own ``publish_topics``; there is no second alerting path and no
transport client is constructed here (OMN-13733).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from omnimarket.inference.secret_store_resolver import resolve_api_key_loop_safe
from omnimarket.nodes.contract_topics import (
    contract_publish_topics,
    contract_secret_ref,
)
from omnimarket.nodes.node_consumer_flow_stall_alert_effect.handlers.build_slack_command import (
    build_slack_command,
)
from omnimarket.nodes.node_consumer_flow_stall_alert_effect.handlers.decide_stall_alert import (
    decide_stall_alert,
)
from omnimarket.nodes.node_consumer_flow_stall_alert_effect.handlers.flow_window_reader import (
    ConsumerFlowWindowReader,
    ProtocolFlowWindowReader,
    resolve_windows_source_dsn,
)
from omnimarket.nodes.node_consumer_flow_stall_alert_effect.models import (
    ModelConsumerFlowStallAlertDecision,
    ModelConsumerFlowStallAlertEvaluation,
    ModelConsumerFlowStallAlertRequest,
    ModelConsumerFlowStallAlertTrigger,
    ModelStallAlertDelivery,
    load_stall_alert_policy,
    load_windows_source,
)

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

_CONTRACT_PATH = Path(__file__).parent.parent / "contract.yaml"

#: Substring identifying this node's Slack command topic among its declared
#: publish topics. The topic STRING itself is never written here — it is read
#: from the contract, so a rename on either side of the chain fails the golden
#: chain test instead of going quiet on the bus.
_SLACK_COMMAND_TOPIC_MARK = "slack-publish"

#: The secret whose value is the destination channel. Declared in the
#: contract's ``secrets`` block; the ref NAME is read from there via
#: ``contract_secret_ref`` so no literal ref lives in this file either.
_CHANNEL_SECRET = "SLACK_CHANNEL_ID"


def _slack_command_topic(contract_path: Path) -> str:
    """Resolve the Slack command topic from the contract's publish topics."""
    for topic in contract_publish_topics(contract_path):
        if _SLACK_COMMAND_TOPIC_MARK in topic:
            return topic
    raise RuntimeError(
        f"{contract_path} declares no publish topic containing "
        f"{_SLACK_COMMAND_TOPIC_MARK!r}; the stall alert has no way to reach "
        "node_slack_publish_effect and must not invent a second alerting path"
    )


class HandlerConsumerFlowStallAlert:
    """Evaluate every key an applied event touched, and publish what fires."""

    def __init__(
        self,
        *,
        event_publisher: Callable[[str, bytes], None] | None = None,
        window_reader: ProtocolFlowWindowReader | None = None,
        contract_path: Path | None = None,
    ) -> None:
        """Self-assemble everything this node needs from its own contract.

        Args:
            event_publisher: Runtime-injected publisher, scoped by the wiring
                to this contract's declared ``publish_topics``. ``None`` only
                in a lane with no bus; a decided alert then reports itself
                undelivered on the terminal event rather than vanishing.
            window_reader: The trailing-history read. Defaults to the real
                Postgres reader built from the contract's ``windows_source``;
                the hermetic tests inject an in-memory one so they drive this
                exact handler rather than a stand-in.
            contract_path: Contract to self-assemble from. Overridable so a
                test can point the node at a modified copy and watch the
                decision change with no code edit (AC2).

        Raises:
            StallAlertPolicyError: The contract declares no usable
                ``alert_policy``.
            WindowsSourceError: The contract declares no usable
                ``windows_source``, or its DSN variable is unset.
        """
        path = contract_path or _CONTRACT_PATH
        self._contract_path = path
        self._policy = load_stall_alert_policy(path)
        self._windows_source = load_windows_source(path)
        self._slack_topic = _slack_command_topic(path)
        self._publisher = event_publisher
        self._reader: ProtocolFlowWindowReader = (
            window_reader
            if window_reader is not None
            else ConsumerFlowWindowReader(
                self._windows_source,
                dsn=resolve_windows_source_dsn(self._windows_source),
            )
        )

    @property
    def policy(self) -> object:
        """The contract-declared policy this node assembled for itself."""
        return self._policy

    @property
    def windows_source(self) -> object:
        """The contract-declared window read target this node assembled."""
        return self._windows_source

    def handle(
        self, request: ModelConsumerFlowStallAlertTrigger
    ) -> ModelConsumerFlowStallAlertEvaluation:
        """Evaluate every (consumer_group, topic) the trigger names.

        Args:
            request: The projection's applied-event batch.

        Returns:
            One decision per evaluated key plus a delivery record for each
            decision that asked to publish. An applied event that wrote no rows
            returns an empty, entirely honest evaluation rather than an error.
        """
        keys = request.alerting_keys()
        ceiling = self._windows_source.max_keys_per_trigger
        evaluated = keys[:ceiling]
        skipped = len(keys) - len(evaluated)
        if skipped:
            logger.warning(
                "Stall alert evaluated %d of %d keys from one applied event; "
                "max_keys_per_trigger=%d. The remainder are not lost — the next "
                "heartbeat re-triggers them — but the ceiling is being hit.",
                len(evaluated),
                len(keys),
                ceiling,
            )

        node_ids = {
            (row.consumer_group, row.topic): row.node_id for row in request.flow_rows
        }
        correlation_id = uuid4()
        decisions: list[ModelConsumerFlowStallAlertDecision] = []
        deliveries: list[ModelStallAlertDelivery] = []
        windows_read = 0

        for consumer_group, topic in evaluated:
            history = self._reader.read_history(
                consumer_group=consumer_group,
                topic=topic,
                limit=self._windows_source.history_windows,
            )
            windows_read += len(history)
            if not history:
                # The trigger named a key the projection has no row for. That is
                # a producer/consumer disagreement, not a stall, and inventing a
                # verdict for it would be the confident-zero this epic exists to
                # stop. Say so and move on.
                logger.warning(
                    "Stall alert read no window history for consumer_group=%s "
                    "topic=%s from %s; the applied event named a key the "
                    "projection does not hold",
                    consumer_group,
                    topic,
                    self._windows_source.relation,
                )
                continue
            decision = decide_stall_alert(
                ModelConsumerFlowStallAlertRequest(
                    consumer_group=consumer_group,
                    topic=topic,
                    node_id=node_ids.get((consumer_group, topic)),
                    correlation_id=correlation_id,
                    windows=history,
                    policy=self._policy,
                )
            )
            decisions.append(decision)
            if decision.should_publish:
                deliveries.append(self._publish(decision, correlation_id))

        return ModelConsumerFlowStallAlertEvaluation(
            keys_evaluated=len(decisions),
            keys_skipped=skipped,
            windows_read=windows_read,
            decisions=tuple(decisions),
            deliveries=tuple(deliveries),
        )

    def _publish(
        self,
        decision: ModelConsumerFlowStallAlertDecision,
        correlation_id: UUID,
    ) -> ModelStallAlertDelivery:
        """Put one Slack command on the declared topic, or say why it did not.

        A failure here is recorded on the terminal event and logged at ERROR
        rather than raised. Raising would DLQ the whole batch onto a topic named
        for *malformed input*, losing every other key's verdict to describe a
        delivery problem — and losing verdicts in order to report a lost verdict
        is the exact shape of failure this node exists to end.
        """
        if decision.alert is None or decision.idempotency_key is None:
            raise RuntimeError(
                "a decision that asks to publish must carry both its payload "
                f"and its idempotency key; {decision.outcome.value} carried "
                f"alert={decision.alert is not None} "
                f"key={decision.idempotency_key is not None}"
            )
        failure: str | None = None
        if self._publisher is None:
            failure = (
                "no event_publisher was injected: this lane has no bus, so the "
                "Slack command could not be published"
            )
        else:
            try:
                channel = self._resolve_channel()
                command = build_slack_command(
                    payload=decision.alert,
                    channel=channel,
                    idempotency_key=decision.idempotency_key,
                    correlation_id=correlation_id,
                )
                self._publisher(
                    self._slack_topic,
                    command.model_dump_json().encode("utf-8"),
                )
            except Exception as exc:
                failure = f"{type(exc).__name__}: {exc}"

        if failure is not None:
            logger.error(
                "Stall alert DECIDED but NOT published: consumer_group=%s "
                "topic=%s outcome=%s idempotency_key=%s reason=%s",
                decision.consumer_group,
                decision.topic,
                decision.outcome.value,
                decision.idempotency_key,
                failure,
            )
        return ModelStallAlertDelivery(
            consumer_group=decision.consumer_group,
            topic=decision.topic,
            command_topic=self._slack_topic,
            idempotency_key=decision.idempotency_key,
            published=failure is None,
            error=failure,
        )

    def _resolve_channel(self) -> str:
        """Resolve the destination channel through the canonical secret store.

        ``resolve_api_key_loop_safe`` rather than the plain sync resolver:
        ``handle`` is synchronous and the runtime may dispatch it from inside
        the kernel's own event loop, where the sync-only guard would raise
        (OMN-13843).
        """
        ref = contract_secret_ref(self._contract_path, _CHANNEL_SECRET)
        secret = resolve_api_key_loop_safe(ref, env_var_fallback=ref)
        if secret is None:
            raise RuntimeError(
                f"secret ref {ref!r} resolved to None; the stall alert has no "
                "channel to post to and will not guess one"
            )
        return secret.get_secret_value()


__all__ = ["HandlerConsumerFlowStallAlert"]
