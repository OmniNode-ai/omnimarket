# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Emit red CI the moment GitHub reports a check run's conclusion, not on the watcher's next poll."""

import hashlib
from collections.abc import Mapping
from uuid import NAMESPACE_URL, uuid5

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
from pydantic import BaseModel, ConfigDict, model_validator

from omnimarket.events.pr_state import EnumPrState, ModelPrCheckFact
from omnimarket.events.topics import CI_RUN_FAILED_TOPIC_V1, GITHUB_CHECK_RUN_TOPIC_V1
from omnimarket.models.ci_red_triage import ModelCiRunFailedEvent
from omnimarket.models.github_check_run import (
    RED_CHECK_CONCLUSIONS,
    ModelGitHubCheckRunObservation,
    observation_from_wire,
)
from omnimarket.nodes.node_pr_state_emit_effect.handlers.handler_detect_ci_red import (
    MEMORY_LIMIT,
    PROCESS_INDEX,
    CiRedIndex,
    build_ci_run_failed,
    repo_short_name,
)


class ModelDetectCiRedCheckRunRequest(BaseModel):
    """Validate the webhook ingress wire payload before runtime handler dispatch."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    observation: ModelGitHubCheckRunObservation

    @model_validator(mode="before")
    @classmethod
    def from_wire(cls, value: object) -> object:
        if isinstance(value, Mapping) and "observation" not in value:
            return {
                "observation": observation_from_wire(
                    ModelGitHubCheckRunObservation, value
                )
            }
        return value


class HandlerDetectCiRedCheckRun:
    """Turns one red check-run conclusion into a ci-run-failed event for a PR the index knows.

    The PR's armed, queued and draft facts and its peers come from the watcher observations in the
    shared index, so a PR the index has not seen (a restart, a new PR) waits for the watcher, as does
    a run whose workflow name has not arrived on the workflow-run stream. Neither decides anything
    early: the watcher path emits for the same head once it observes the red. A check the watcher
    already observed red at this head is the watcher path's event, so it is not emitted twice.
    """

    MEMORY_LIMIT = MEMORY_LIMIT
    subscribe_topic = GITHUB_CHECK_RUN_TOPIC_V1
    published_event_topics = {ModelCiRunFailedEvent: CI_RUN_FAILED_TOPIC_V1}

    def __init__(self, *, index: CiRedIndex | None = None) -> None:
        self._index = index if index is not None else PROCESS_INDEX

    async def handle(
        self, request: ModelDetectCiRedCheckRunRequest | Mapping[str, object]
    ) -> ModelHandlerOutput[None]:
        observation = (
            request
            if isinstance(request, ModelDetectCiRedCheckRunRequest)
            else ModelDetectCiRedCheckRunRequest.model_validate(request)
        ).observation
        identity = "|".join(
            (
                observation.repo,
                str(observation.pr_number),
                observation.head_sha,
                observation.check,
                str(observation.run_id or 0),
                observation.conclusion,
                observation.completed_at,
            )
        )
        source_digest = hashlib.sha256(identity.encode()).hexdigest()
        correlation_id = uuid5(NAMESPACE_URL, "onex:ci-red-check-run:" + source_digest)
        events: tuple[ModelCiRunFailedEvent, ...] = ()
        index = self._index
        entry = index.observation_for(observation.repo, observation.pr_number)
        workflow = (
            index.workflows.get((repo_short_name(observation.repo), observation.run_id))
            if observation.run_id
            else ""
        )
        if (
            observation.conclusion in RED_CHECK_CONCLUSIONS
            and entry is not None
            and entry.state == EnumPrState.OPEN
            and not entry.draft
            and workflow is not None
            and not (
                entry.head_sha == observation.head_sha
                and observation.check in entry.red_contexts
            )
        ):
            head = observation.head_sha
            short = repo_short_name(observation.repo)
            reds = index.check_reds.setdefault((short, observation.pr_number, head), {})
            reds[observation.check] = ModelPrCheckFact(
                check=observation.check,
                conclusion=observation.conclusion,
                run_id=observation.run_id or 0,
                workflow=workflow,
                completed_at=observation.completed_at,
            )
            index.check_reds.move_to_end((short, observation.pr_number, head))
            while len(index.check_reds) > self.MEMORY_LIMIT:
                index.check_reds.popitem(last=False)
            runs: dict[str, ModelPrCheckFact] = dict(reds)
            checks = set(reds)
            if entry.head_sha == head:
                checks |= set(entry.red_contexts)
                runs.update({fact.check: fact for fact in getattr(entry, "checks", ())})
            ordered = tuple(sorted(checks))
            candidate = build_ci_run_failed(
                index,
                entry,
                head_sha=head,
                base=observation.base_ref or entry.base,
                checks=ordered,
                runs=runs,
                ci_read_at=observation.completed_at,
                observed_at=observation.completed_at,
                source_digest=source_digest,
            )
            if index.record_emitted(candidate.event_id, self.MEMORY_LIMIT):
                events = (candidate,)
        return ModelHandlerOutput.for_effect(
            input_envelope_id=correlation_id,
            correlation_id=correlation_id,
            handler_id="node_pr_state_emit_effect.detect_ci_red_check_run",
            events=events,
        )
