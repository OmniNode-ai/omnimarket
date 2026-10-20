# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Decide the interval's alert actions and put each on its command topic.

Canonical definition-B shape: ``handle(ModelLabSliAlertRequest) ->
ModelLabSliAlertResult``. The decision is the pure ``DecideLabSliAlert``; this
handler is the effect boundary, in the shape of
node_consumer_flow_stall_alert_effect: a push goes out as the existing
``slack-publish`` command (channel resolved from the contract's secret ref, at
this boundary only) through the runtime-injected ``event_publisher`` scoped to
this contract's publish topics. Fix lanes are requested on the result itself,
the ``lab-sli-alerted`` terminal event, each as a ``ModelLabSliFixLaneRequested``
in ``fix_lanes`` for the lab-fill launcher to pick up. A delivery failure is recorded on the result, never raised: a
lost delivery must not take the interval's other verdicts with it.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from pathlib import Path
from uuid import UUID, uuid4

from omnimarket.enums.enum_lab_sli import EnumLabSliActionKind
from omnimarket.handlers.lab_sli_pack_gate import require_lab_sli_pack
from omnimarket.inference.secret_store_resolver import resolve_api_key_loop_safe
from omnimarket.models.lab_sli import (
    ModelLabSliAlertAction,
    ModelLabSliAlertRequest,
    ModelLabSliAlertResult,
    ModelLabSliDelivery,
    ModelLabSliFixLaneRequested,
)
from omnimarket.nodes.contract_topics import (
    contract_publish_topics,
    contract_secret_ref,
)
from omnimarket.nodes.node_lab_sli_alert_effect.handlers.decide_lab_sli_alert import (
    DecideLabSliAlert,
    ModelLabSliAlertPolicy,
)

logger = logging.getLogger(__name__)

CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"
_CHANNEL_SECRET = "SLACK_CHANNEL_ID"


def _topic(contract_path: Path, marker: str) -> str:
    matches = [t for t in contract_publish_topics(contract_path) if marker in t]
    if len(matches) != 1:
        raise ValueError(f"{contract_path}: expected one publish topic naming {marker}")
    return matches[0]


class HandlerLabSliAlert:
    """Severity-driven alert actions for one interval, published."""

    def __init__(
        self,
        *,
        event_publisher: Callable[[str, bytes], None] | None = None,
        policy: ModelLabSliAlertPolicy | None = None,
        channel_resolver: Callable[[], str] | None = None,
        contract_path: Path = CONTRACT_PATH,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._environ = environ
        self._decide = DecideLabSliAlert(policy)
        self._publisher = event_publisher
        self._contract_path = contract_path
        self._push_topic = _topic(contract_path, ".slack-publish.")
        self._channel_resolver = channel_resolver or self._resolve_channel

    def handle(self, request: ModelLabSliAlertRequest) -> ModelLabSliAlertResult:
        require_lab_sli_pack("node_lab_sli_alert_effect", self._environ)
        decided = self._decide.handle(request)
        correlation_id = uuid4()
        deliveries = tuple(
            self._push(a, correlation_id)
            for a in decided.actions
            if a.kind is EnumLabSliActionKind.PUSH_OPERATOR
        )
        fix_lanes = tuple(
            ModelLabSliFixLaneRequested(
                lane=a.lane,
                severity=a.severity,
                sli_ids=a.sli_ids,
                brief=a.text,
                idempotency_key=a.dedup_key,
                correlation_id=correlation_id,
            )
            for a in decided.actions
            if a.kind is not EnumLabSliActionKind.PUSH_OPERATOR
        )
        return decided.model_copy(
            update={"deliveries": deliveries, "fix_lanes": fix_lanes}
        )

    def _push(
        self, action: ModelLabSliAlertAction, correlation_id: UUID
    ) -> ModelLabSliDelivery:
        topic = self._push_topic
        error = ""
        publish = self._publisher
        if publish is None:
            error = "no event_publisher was injected: this lane has no bus"
        else:
            try:
                payload = {
                    "channel": self._channel_resolver(),
                    "text": action.text,
                    "idempotency_key": action.dedup_key,
                    "correlation_id": str(correlation_id),
                }
                publish(topic, json.dumps(payload).encode("utf-8"))
            except Exception as exc:  # recorded on the result, see the module docstring
                error = f"{type(exc).__name__}: {exc}"
        if error:
            logger.error(
                "Lab SLI action DECIDED but NOT published: key=%s topic=%s reason=%s",
                action.dedup_key,
                topic,
                error,
            )
        return ModelLabSliDelivery(
            dedup_key=action.dedup_key, topic=topic, published=not error, error=error
        )

    def _resolve_channel(self) -> str:
        ref = contract_secret_ref(self._contract_path, _CHANNEL_SECRET)
        secret = resolve_api_key_loop_safe(ref, env_var_fallback=ref)
        if secret is None:
            raise RuntimeError(
                f"secret ref {ref!r} resolved to None; no channel to push to"
            )
        return secret.get_secret_value()


__all__ = ["HandlerLabSliAlert"]
