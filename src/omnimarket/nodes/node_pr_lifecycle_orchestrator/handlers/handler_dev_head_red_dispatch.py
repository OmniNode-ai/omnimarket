# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ask for one cause lane per red landing-branch head cause, unless one owns it (OMN-20730).

Ruling 2026-10-08T14:27:26Z: red CI is a bus message the sweep orchestrator triages on receipt.
A dev push run that goes red names no pull request; node_pr_state_emit_effect publishes it as
``dev-head-red``. This handler decides it once and either asks for a cause lane (the
remote-lane runner with ``--cause <repo>:<check>``) or records who owns the cause:

1. a cause lane this process asked for, inside the lease;
2. the bus path's durable owner claim on the PR path's dev_head owner key
   (pr_lifecycle_ledger_entries, read through ci_red_claims);
3. a live cause CLAIM on the work ledger, a remote lane's or the landing controller's relay CLAIM.

An unreadable owner withholds the request, and so does ``act`` false. The request's
``dispatch_key`` is one per owner key and head; its receiver starts at most one lane per key,
which is what keeps a request re-sent after a restart from starting a second lane
(formal/ci_red_triage, OneStartPerIdentity with ReceiverDedup).
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml
from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
from omnibase_infra.runtime.overlay.contract_env_ref import expand_contract_env_refs

from omnimarket.events.topics import (
    DEV_CAUSE_LANE_REQUESTED_TOPIC_V1,
    DEV_HEAD_RED_DECIDED_TOPIC_V1,
    DEV_HEAD_RED_TOPIC_V1,
)
from omnimarket.models.dev_head_red import (
    EnumDevHeadRedAction,
    ModelDevCauseLaneRequest,
    ModelDevHeadRedDecided,
    ModelDevHeadRedEvent,
    dev_cause_dispatch_key,
    dev_cause_lane_name,
    dev_cause_ref,
    dev_cause_request_correlation_id,
    dev_head_owner_key,
    dev_head_red_decision_correlation_id,
)
from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.ci_red_claims import (
    CiRedClaimsUnreadError,
    ProtocolCiRedClaims,
    claims_from_contract,
)
from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.dev_cause_owners import (
    DevCauseOwnersUnreadError,
    ProtocolDevCauseOwners,
    dev_cause_owners_from_contract,
)

_Published = ModelDevCauseLaneRequest | ModelDevHeadRedDecided
_FLAG_WORDS = {"true": True, "false": False}


def dev_head_red_act_flag(value: object) -> bool:
    """The contract's act flag: a bool, or an overlay reference expanding to true or false."""
    if isinstance(value, str):
        word = expand_contract_env_refs(value)
        if word not in _FLAG_WORDS:
            raise ValueError(
                f"dev_head_red_dispatch.act must be true or false, not {word!r}"
            )
        return _FLAG_WORDS[word]
    if not isinstance(value, bool):
        raise ValueError("dev_head_red_dispatch.act must be true or false")
    return value


class HandlerDevHeadRedDispatch:
    """One decision per red; at most one cause-lane request per owner key inside its lease."""

    MEMORY_LIMIT = 4096
    subscribe_topic = DEV_HEAD_RED_TOPIC_V1
    published_event_topics = {
        ModelDevCauseLaneRequest: DEV_CAUSE_LANE_REQUESTED_TOPIC_V1,
        ModelDevHeadRedDecided: DEV_HEAD_RED_DECIDED_TOPIC_V1,
    }

    def __init__(
        self,
        *,
        act: bool | None = None,
        claims: ProtocolCiRedClaims | None = None,
        owners: ProtocolDevCauseOwners | None = None,
        lease: timedelta | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """Every argument left ``None`` is read from the contract."""
        if act is None or claims is None or owners is None or lease is None:
            with (
                Path(__file__).resolve().parents[1] / "contract.yaml"
            ).open() as stream:
                raw = yaml.safe_load(stream)
            block = raw["dev_head_red_dispatch"]
            seconds = block["lease_seconds"]
            if type(seconds) is not int or seconds <= 0:
                raise ValueError("dev_head_red_dispatch.lease_seconds must be positive")
            lease = lease if lease is not None else timedelta(seconds=seconds)
            if act is None:
                act = dev_head_red_act_flag(block["act"])
            if claims is None:
                claims = claims_from_contract(raw["ci_red_triage"]["claims"])
            if owners is None:
                owners = dev_cause_owners_from_contract(block["owners"], lease)
        self._act = act
        self._claims = claims
        self._owners = owners
        self._lease = lease
        self._now = now
        self._seen: OrderedDict[str, None] = OrderedDict()
        # owner key -> (lane, asked at): the requests this process made.
        self._dispatched: OrderedDict[str, tuple[str, datetime]] = OrderedDict()

    def _remember[K, V](self, cache: OrderedDict[K, V], key: K, value: V) -> None:
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > self.MEMORY_LIMIT:
            cache.popitem(last=False)

    async def _owner(
        self, event: ModelDevHeadRedEvent, owner_key: str, now: datetime
    ) -> tuple[str | None, list[str]]:
        """The cause's owner, and what could not be read."""
        mine = self._dispatched.get(owner_key)
        if mine is not None and now - mine[1] < self._lease:
            return f"dispatched:{mine[0]}", []
        unread: list[str] = []
        try:
            if await asyncio.to_thread(self._claims.owned, owner_key):
                return f"bus-claim:{owner_key}", []
        except CiRedClaimsUnreadError as exc:
            unread.append(f"bus claim ({exc})")
        try:
            lane = await asyncio.to_thread(
                self._owners.owner, event.repo, event.check, now
            )
            if lane is not None:
                return f"ledger-claim:{lane}", []
        except DevCauseOwnersUnreadError as exc:
            unread.append(f"work ledger ({exc})")
        return None, unread

    async def handle(
        self, request: ModelDevHeadRedEvent | Mapping[str, object]
    ) -> ModelHandlerOutput[None]:
        event = (
            request
            if isinstance(request, ModelDevHeadRedEvent)
            else ModelDevHeadRedEvent.model_validate(request)
        )
        decision_key = f"{event.repo}@{event.branch}@{event.head_sha}:{event.check}"
        events: tuple[_Published, ...] = ()
        if event.event_id in self._seen:
            self._seen.move_to_end(event.event_id)
        else:
            events = await self._decide(event, decision_key)
            self._remember(self._seen, event.event_id, None)
        correlation_id = dev_head_red_decision_correlation_id(decision_key)
        return ModelHandlerOutput.for_orchestrator(
            input_envelope_id=correlation_id,
            correlation_id=correlation_id,
            handler_id="node_pr_lifecycle_orchestrator.dev_head_red_dispatch",
            events=events,
        )

    async def _decide(
        self, event: ModelDevHeadRedEvent, decision_key: str
    ) -> tuple[_Published, ...]:
        now = self._now()
        owner_key = dev_head_owner_key(event.repo, event.branch, event.check)
        cause = dev_cause_ref(event.repo, event.check)
        owner: str | None = None
        request: ModelDevCauseLaneRequest | None = None
        if not self._act:
            action, why = EnumDevHeadRedAction.WITHHELD, "withheld:act=false"
        else:
            owner, unread = await self._owner(event, owner_key, now)
            if owner is not None:
                action, why = EnumDevHeadRedAction.OWNED, f"owned-by={owner}"
            elif unread:
                action = EnumDevHeadRedAction.WITHHELD
                why = "withheld:owners-unread " + "; ".join(unread)
            else:
                dispatch_key = dev_cause_dispatch_key(owner_key, event.head_sha)
                request = ModelDevCauseLaneRequest(
                    correlation_id=dev_cause_request_correlation_id(dispatch_key),
                    dispatch_key=dispatch_key,
                    owner_key=owner_key,
                    lane=dev_cause_lane_name(event.repo, dispatch_key),
                    repo=event.repo,
                    branch=event.branch,
                    head_sha=event.head_sha,
                    check=event.check,
                    cause=cause,
                    event_id=event.event_id,
                    observed_at=event.completed_at,
                )
                self._remember(self._dispatched, owner_key, (request.lane, now))
                action, why = EnumDevHeadRedAction.DISPATCH_CAUSE_LANE, "dispatched"
        evidence = (
            f"action={action} owner_key={owner_key} cause={cause} "
            f"conclusion={event.conclusion} {why}"
        )
        decided = ModelDevHeadRedDecided(
            correlation_id=dev_head_red_decision_correlation_id(decision_key),
            decision_key=decision_key,
            owner_key=owner_key,
            event_id=event.event_id,
            repo=event.repo,
            branch=event.branch,
            head_sha=event.head_sha,
            check=event.check,
            action=action,
            dispatch_key=request.dispatch_key if request is not None else None,
            lane=request.lane if request is not None else None,
            owner=owner,
            evidence=" ".join(evidence.splitlines()),
            observed_at=event.completed_at,
        )
        return (request, decided) if request is not None else (decided,)
