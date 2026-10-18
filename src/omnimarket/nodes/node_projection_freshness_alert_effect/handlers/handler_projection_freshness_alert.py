# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Heartbeat-carried freshness evaluation, canonical typed def-B (OMN-19513)."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from omnimarket.inference.secret_store_resolver import resolve_api_key_loop_safe
from omnimarket.nodes.contract_topics import (
    contract_publish_topics,
    contract_secret_ref,
)
from omnimarket.nodes.node_projection_freshness_alert_effect.handlers.build_slack_command import (
    build_slack_command,
)
from omnimarket.nodes.node_projection_freshness_alert_effect.handlers.classify_freshness import (
    classify_freshness,
)
from omnimarket.nodes.node_projection_freshness_alert_effect.handlers.freshness_reader import (
    PostgresFreshnessReader,
    resolve_reads_source_dsn,
)
from omnimarket.nodes.node_projection_freshness_alert_effect.handlers.load_freshness_config import (
    load_freshness_config,
)
from omnimarket.nodes.node_projection_freshness_alert_effect.handlers.protocol_freshness_reader import (
    ProtocolFreshnessReader,
)
from omnimarket.nodes.node_projection_freshness_alert_effect.models import (
    EnumFreshnessState,
    FreshnessPolicyError,
    ModelFreshnessCheck,
    ModelFreshnessDelivery,
    ModelFreshnessEvaluation,
    ModelFreshnessObservation,
    ModelFreshnessTrigger,
    ModelFreshnessVerdict,
)

logger = logging.getLogger(__name__)
_CONTRACT_PATH = Path(__file__).parent.parent / "contract.yaml"
_CHANNEL_SECRET = "SLACK_CHANNEL_ID"


class HandlerProjectionFreshnessAlert:
    """Self-assemble the policy and read targets, then publish stale findings."""

    def __init__(
        self,
        *,
        event_publisher: Callable[[str, bytes], None] | None = None,
        reader: ProtocolFreshnessReader | None = None,
        clock: Callable[[], float] | None = None,
        contract_path: Path | None = None,
    ) -> None:
        self._contract_path = contract_path or _CONTRACT_PATH
        self._policy, self._source, self._checks = load_freshness_config(
            self._contract_path
        )
        topics = [
            topic
            for topic in contract_publish_topics(self._contract_path)
            if "slack-publish" in topic
        ]
        if len(topics) != 1:
            raise FreshnessPolicyError("contract must declare one slack-publish topic")
        self._slack_topic = topics[0]
        self._publisher = event_publisher
        self._reader = reader
        self._clock = clock or time.time
        self._last_bucket: int | None = None

    def handle(self, request: ModelFreshnessTrigger) -> ModelFreshnessEvaluation:
        """Return every finding and delivery attempt; throttle already-read buckets."""
        epoch = self._clock()
        interval = self._policy.check_interval_seconds
        bucket = int(epoch) // interval
        if self._last_bucket is not None and bucket <= self._last_bucket:
            return ModelFreshnessEvaluation(
                evaluated=False,
                check_interval_seconds=interval,
                verdicts=(),
                deliveries=(),
            )
        self._last_bucket = bucket
        now = datetime.fromtimestamp(epoch, tz=UTC)
        correlation_id = request.correlation_id or uuid4()
        verdicts: list[ModelFreshnessVerdict] = []
        deliveries: list[ModelFreshnessDelivery] = []
        for check in self._checks:
            error: str | None = None
            observation: ModelFreshnessObservation | None = None
            try:
                if self._reader is None:
                    # Resolve at the first evaluation, so an absent DSN becomes
                    # a terminal UNKNOWN finding rather than a wiring failure.
                    self._reader = PostgresFreshnessReader(
                        dsn=resolve_reads_source_dsn(self._source),
                        now=lambda: datetime.fromtimestamp(self._clock(), tz=UTC),
                    )
                observation = ModelFreshnessObservation.model_validate(
                    self._reader.read(check)
                )
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
            verdict = classify_freshness(
                check_id=check.check_id,
                now=now,
                supply_newest_at=(
                    observation.supply_newest_at if observation is not None else None
                ),
                demand_distinct_active=(
                    observation.demand_distinct_active
                    if observation is not None
                    else None
                ),
                max_age_seconds=check.max_age_seconds,
                min_distinct=check.demand.min_distinct,
                error=error,
            )
            verdicts.append(verdict)
            if verdict.state is EnumFreshnessState.UNKNOWN:
                logger.error(
                    "Freshness UNKNOWN: check_id=%s error=%s",
                    check.check_id,
                    verdict.error,
                )
            elif verdict.state is EnumFreshnessState.STALE:
                deliveries.append(self._publish(check, verdict, epoch, correlation_id))
        return ModelFreshnessEvaluation(
            evaluated=True,
            check_interval_seconds=interval,
            verdicts=tuple(verdicts),
            deliveries=tuple(deliveries),
        )

    def _publish(
        self,
        check: ModelFreshnessCheck,
        verdict: ModelFreshnessVerdict,
        epoch: float,
        correlation_id: UUID,
    ) -> ModelFreshnessDelivery:
        bucket = int(epoch) // self._policy.renotify_after_seconds
        key = f"projection-freshness|{check.check_id}|STALE|{bucket}"
        error: str | None = None
        publisher = self._publisher
        if publisher is None:
            error = "no event_publisher was injected; Slack command was not published"
        else:
            try:
                ref = contract_secret_ref(self._contract_path, _CHANNEL_SECRET)
                secret = resolve_api_key_loop_safe(ref, env_var_fallback=ref)
                if secret is None:
                    raise RuntimeError(f"secret ref {ref!r} resolved to None")
                command = build_slack_command(
                    check=check,
                    verdict=verdict,
                    channel=secret.get_secret_value(),
                    idempotency_key=key,
                    correlation_id=correlation_id,
                )
                publisher(self._slack_topic, command.model_dump_json().encode("utf-8"))
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
        if error is not None:
            logger.error(
                "Freshness alert NOT published: check_id=%s error=%s",
                check.check_id,
                error,
            )
        return ModelFreshnessDelivery(
            check_id=check.check_id,
            command_topic=self._slack_topic,
            idempotency_key=key,
            published=error is None,
            error=error,
        )
