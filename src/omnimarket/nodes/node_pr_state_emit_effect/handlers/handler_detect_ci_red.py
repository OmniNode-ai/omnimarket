# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Derive one red-CI event per repo/PR/head/check-set from watcher observations."""

from collections import OrderedDict
from collections.abc import Mapping
from datetime import datetime
from uuid import NAMESPACE_URL, uuid5

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
from pydantic import BaseModel, ConfigDict, model_validator

from omnimarket.events.pr_state import (
    EnumPrState,
    ModelPrCheckFact,
    ModelPrStateObservedEvent,
    pr_state_event_from_wire,
)
from omnimarket.events.topics import CI_RUN_FAILED_TOPIC_V1, PR_STATE_OBSERVED_TOPIC_V1
from omnimarket.models.ci_red_triage import (
    ModelCiRedPeer,
    ModelCiRunFailedEvent,
    ci_red_repo_slug,
    ci_run_failed_event_id,
)

MEMORY_LIMIT = 4096


def repo_short_name(repo: str) -> str:
    return repo.rpartition("/")[2]


class CiRedIndex:
    """What the detectors know, rebuilt from the bus streams after a restart.

    The runtime gives each routing entry of the node its own handler instance, while the watcher
    stream, the check-run stream and the workflow-run stream must see one index, so the handlers of
    this node share ``PROCESS_INDEX``. The index is empty after a restart; the polling backstop
    covers it.
    """

    def __init__(self) -> None:
        self.observations: OrderedDict[tuple[str, int], ModelPrStateObservedEvent] = (
            OrderedDict()
        )
        self.newest: OrderedDict[tuple[str, int], datetime] = OrderedDict()
        self.emitted: OrderedDict[str, None] = OrderedDict()
        # (repo short name, workflow run id) -> workflow name
        self.workflows: OrderedDict[tuple[str, int], str] = OrderedDict()
        # (repo short name, PR, head sha) -> red check runs the webhook reported at that head
        self.check_reds: OrderedDict[
            tuple[str, int, str], dict[str, ModelPrCheckFact]
        ] = OrderedDict()

    def clear(self) -> None:
        for held in (
            self.observations,
            self.newest,
            self.emitted,
            self.workflows,
            self.check_reds,
        ):
            held.clear()

    def observation_for(
        self, repo: str, pr_number: int
    ) -> ModelPrStateObservedEvent | None:
        for name in (repo_short_name(repo), ci_red_repo_slug(repo_short_name(repo))):
            found = self.observations.get((name, pr_number))
            if found is not None:
                return found
        return None

    def learn_workflow(self, repo: str, run_id: int, workflow: str, limit: int) -> None:
        key = (repo_short_name(repo), run_id)
        self.workflows[key] = workflow
        self.workflows.move_to_end(key)
        while len(self.workflows) > limit:
            self.workflows.popitem(last=False)

    def peers_of(
        self, repo: str, pr_number: int, checks: tuple[str, ...]
    ) -> tuple[ModelCiRedPeer, ...]:
        short = repo_short_name(repo)
        return tuple(
            ModelCiRedPeer(
                pr_number=peer.pr_number,
                head_sha=peer.head_sha,
                armed=peer.armed,
                red_contexts=peer.red_contexts,
            )
            for (peer_repo, peer_pr), peer in sorted(self.observations.items())
            if repo_short_name(peer_repo) == short
            and peer_pr != pr_number
            and peer.ci_verdict == "RED"
            and set(checks).intersection(peer.red_contexts)
        )

    def record_emitted(self, event_id: str, limit: int) -> bool:
        """True when the event is new; a repeat only refreshes its place."""
        if event_id in self.emitted:
            self.emitted.move_to_end(event_id)
            return False
        self.emitted[event_id] = None
        while len(self.emitted) > limit:
            self.emitted.popitem(last=False)
        return True


PROCESS_INDEX = CiRedIndex()


def build_ci_run_failed(
    index: CiRedIndex,
    observation: ModelPrStateObservedEvent,
    *,
    head_sha: str,
    base: str,
    checks: tuple[str, ...],
    runs: Mapping[str, ModelPrCheckFact],
    ci_read_at: str,
    observed_at: str,
    source_digest: str,
) -> ModelCiRunFailedEvent:
    """The red-CI event for ``checks`` at ``head_sha``, with its facts and peers."""
    return ModelCiRunFailedEvent(
        event_id=ci_run_failed_event_id(
            observation.repo, observation.pr_number, head_sha, checks
        ),
        repo=observation.repo,
        pr_number=observation.pr_number,
        head_sha=head_sha,
        base=base,
        armed=observation.armed,
        queued=observation.queued,
        failing_checks=checks,
        ci_read_at=ci_read_at,
        observed_at=observed_at,
        source_digest=source_digest,
        peers=index.peers_of(observation.repo, observation.pr_number, checks),
        failing_runs=tuple(runs[check] for check in checks if check in runs),
        base_red_checks=getattr(observation, "base_red_checks", ()),
        base_read=bool(getattr(observation, "base_read", False)),
    )


class ModelDetectCiRedRequest(BaseModel):
    """Validate the watcher wire payload before runtime handler dispatch."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    event: ModelPrStateObservedEvent

    @model_validator(mode="before")
    @classmethod
    def from_wire(cls, value: object) -> object:
        if isinstance(value, Mapping) and "event" not in value:
            return {"event": pr_state_event_from_wire(value)}
        return value


class HandlerDetectCiRed:
    """A bounded derivation cache rebuilt from the observation stream.

    The index is empty after a restart; the polling backstop covers it. Closed
    observations are retained only as bounded freshness markers so delayed open
    observations cannot resurrect a PR removed from the index.
    """

    MEMORY_LIMIT = MEMORY_LIMIT
    subscribe_topic = PR_STATE_OBSERVED_TOPIC_V1
    published_event_topics = {ModelCiRunFailedEvent: CI_RUN_FAILED_TOPIC_V1}

    def __init__(self, *, index: CiRedIndex | None = None) -> None:
        self._index = index if index is not None else PROCESS_INDEX

    @property
    def _observations(
        self,
    ) -> OrderedDict[tuple[str, int], ModelPrStateObservedEvent]:
        return self._index.observations

    @property
    def _newest(self) -> OrderedDict[tuple[str, int], datetime]:
        return self._index.newest

    @property
    def _emitted(self) -> OrderedDict[str, None]:
        return self._index.emitted

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
        index = self._index
        correlation_id = uuid5(NAMESPACE_URL, "onex:ci-red-observation:" + event.digest)
        events: tuple[ModelCiRunFailedEvent, ...] = ()
        key = (event.repo, event.pr_number)
        observed_at = datetime.fromisoformat(event.observed_at)
        previous = index.newest.get(key)
        if previous is None or observed_at >= previous:
            index.newest[key] = observed_at
            index.newest.move_to_end(key)
            while len(index.newest) > self.MEMORY_LIMIT:
                expired, _ = index.newest.popitem(last=False)
                index.observations.pop(expired, None)
            if event.state != EnumPrState.OPEN:
                index.observations.pop(key, None)
            else:
                index.observations[key] = event
                index.observations.move_to_end(key)
                for fact in getattr(event, "checks", ()):
                    if fact.run_id:
                        index.learn_workflow(
                            event.repo, fact.run_id, fact.workflow, self.MEMORY_LIMIT
                        )
                if not event.draft and event.ci_verdict == "RED" and event.red_contexts:
                    checks = tuple(sorted(set(event.red_contexts)))
                    event_id = ci_run_failed_event_id(
                        event.repo, event.pr_number, event.head_sha, checks
                    )
                    if index.record_emitted(event_id, self.MEMORY_LIMIT):
                        reported = index.check_reds.get(
                            (
                                repo_short_name(event.repo),
                                event.pr_number,
                                event.head_sha,
                            ),
                            {},
                        )
                        runs = {
                            **reported,
                            **{
                                fact.check: fact
                                for fact in getattr(event, "checks", ())
                            },
                        }
                        events = (
                            build_ci_run_failed(
                                index,
                                event,
                                head_sha=event.head_sha,
                                base=event.base,
                                checks=checks,
                                runs=runs,
                                ci_read_at=event.ci_read_at,
                                observed_at=event.observed_at,
                                source_digest=event.digest,
                            ),
                        )
        return ModelHandlerOutput.for_effect(
            input_envelope_id=correlation_id,
            correlation_id=correlation_id,
            handler_id="node_pr_state_emit_effect.detect_ci_red",
            events=events,
        )
