# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Derive one red-CI event per repo/PR/head/check-set from watcher observations."""

import json
from collections import OrderedDict
from collections.abc import Mapping
from datetime import datetime
from uuid import NAMESPACE_URL, uuid5

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
from pydantic import BaseModel, ConfigDict, model_validator

from omnimarket.events.pr_state import EnumPrState, ModelPrStateObservedEvent
from omnimarket.events.topics import CI_RUN_FAILED_TOPIC_V1, PR_STATE_OBSERVED_TOPIC_V1
from omnimarket.models.ci_red_triage import (
    ModelCiRedPeer,
    ModelCiRunFailedEvent,
    ci_run_failed_event_id,
)


class ModelDetectCiRedRequest(BaseModel):
    """Validate the watcher wire payload before runtime handler dispatch."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    event: ModelPrStateObservedEvent

    @model_validator(mode="before")
    @classmethod
    def from_wire(cls, value: object) -> object:
        if isinstance(value, Mapping) and "event" not in value:
            payload = {
                k: value[k]
                for k in ModelPrStateObservedEvent.model_fields
                if k in value
            }
            if "digest" not in payload:
                raise ValueError("wire observation must carry digest")
            return {
                "event": ModelPrStateObservedEvent.model_validate_json(
                    json.dumps(payload)
                )
            }
        return value


class HandlerDetectCiRed:
    """A bounded derivation cache rebuilt from the observation stream.

    The index is empty after a restart; the polling backstop covers it. Closed
    observations are retained only as bounded freshness markers so delayed open
    observations cannot resurrect a PR removed from the index.
    """

    MEMORY_LIMIT = 4096
    subscribe_topic = PR_STATE_OBSERVED_TOPIC_V1
    published_event_topics = {ModelCiRunFailedEvent: CI_RUN_FAILED_TOPIC_V1}

    def __init__(self) -> None:
        self._observations: OrderedDict[tuple[str, int], ModelPrStateObservedEvent] = (
            OrderedDict()
        )
        self._newest: OrderedDict[tuple[str, int], datetime] = OrderedDict()
        self._emitted: OrderedDict[str, None] = OrderedDict()

    async def handle(
        self,
        request: ModelDetectCiRedRequest
        | ModelPrStateObservedEvent
        | Mapping[str, object],
    ) -> ModelHandlerOutput[None]:
        if isinstance(request, ModelPrStateObservedEvent):
            event = request
        else:
            ingress = (
                request
                if isinstance(request, ModelDetectCiRedRequest)
                else ModelDetectCiRedRequest.model_validate(request)
            )
            event = ingress.event
        correlation_id = uuid5(NAMESPACE_URL, "onex:ci-red-observation:" + event.digest)
        events: tuple[ModelCiRunFailedEvent, ...] = ()
        key = (event.repo, event.pr_number)
        observed_at = datetime.fromisoformat(event.observed_at)
        previous = self._newest.get(key)
        if previous is None or observed_at >= previous:
            self._newest[key] = observed_at
            self._newest.move_to_end(key)
            while len(self._newest) > self.MEMORY_LIMIT:
                expired, _ = self._newest.popitem(last=False)
                self._observations.pop(expired, None)
            if event.state != EnumPrState.OPEN:
                self._observations.pop(key, None)
            else:
                self._observations[key] = event
                self._observations.move_to_end(key)
                if not event.draft and event.ci_verdict == "RED" and event.red_contexts:
                    checks = tuple(sorted(set(event.red_contexts)))
                    event_id = ci_run_failed_event_id(
                        event.repo, event.pr_number, event.head_sha, checks
                    )
                    if event_id in self._emitted:
                        self._emitted.move_to_end(event_id)
                    else:
                        peers = tuple(
                            ModelCiRedPeer(
                                pr_number=peer.pr_number,
                                head_sha=peer.head_sha,
                                armed=peer.armed,
                                red_contexts=peer.red_contexts,
                            )
                            for peer_key, peer in sorted(self._observations.items())
                            if peer_key != key
                            and peer.repo == event.repo
                            and peer.ci_verdict == "RED"
                            and set(checks).intersection(peer.red_contexts)
                        )
                        events = (
                            ModelCiRunFailedEvent(
                                event_id=event_id,
                                repo=event.repo,
                                pr_number=event.pr_number,
                                head_sha=event.head_sha,
                                base=event.base,
                                armed=event.armed,
                                queued=event.queued,
                                failing_checks=checks,
                                ci_read_at=event.ci_read_at,
                                observed_at=event.observed_at,
                                source_digest=event.digest,
                                peers=peers,
                            ),
                        )
                        self._emitted[event_id] = None
                        while len(self._emitted) > self.MEMORY_LIMIT:
                            self._emitted.popitem(last=False)
        return ModelHandlerOutput.for_effect(
            input_envelope_id=correlation_id,
            correlation_id=correlation_id,
            handler_id="node_pr_state_emit_effect.detect_ci_red",
            events=events,
        )
