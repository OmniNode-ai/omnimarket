# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Claim a red-CI owner and return a scoped command through the existing sweep path."""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Protocol, TypeVar

import yaml
from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput

from omnimarket.events.topics import (
    CI_RED_TRIAGE_DECIDED_TOPIC_V1,
    CI_RUN_FAILED_TOPIC_V1,
    PR_LIFECYCLE_ORCHESTRATOR_START_TOPIC_V1,
)
from omnimarket.handlers.cause_signature import (
    ANNOTATION_CHUNK,
    UNREAD,
    annotation_query,
    cause_key,
    first_failure_annotations,
    normalize_signature,
)
from omnimarket.models.ci_red_triage import (
    EnumCiRedAction,
    EnumCiRedClass,
    ModelCiRedClassification,
    ModelCiRedFacts,
    ModelCiRedTriageDecided,
    ModelCiRunFailedEvent,
    ci_red_cause_key,
    ci_red_decision_correlation_id,
    ci_red_owner_correlation_id,
    ci_red_owner_run_id,
    ci_red_repo_slug,
)
from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.ci_red_claims import (
    CiRedClaimsUnreadError,
    ProtocolCiRedClaims,
    claims_from_contract,
)
from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
    ModelPrLifecycleStartCommand,
)

_K = TypeVar("_K")
_V = TypeVar("_V")

OWNER_ACTIONS = {
    EnumCiRedClass.SHARED_CAUSE: EnumCiRedAction.START_CAUSE_OWNER,
    EnumCiRedClass.PR_OWN: EnumCiRedAction.START_PR_FIX,
    EnumCiRedClass.DEV_HEAD: EnumCiRedAction.START_DEV_CAUSE,
    EnumCiRedClass.RUNNER: EnumCiRedAction.RERUN_FAILED,
}


def _member_cause_keys(
    slug: str, facts: ModelCiRedFacts, checks: Iterable[str]
) -> tuple[str, ...]:
    """Annotation-level cause keys followed by check-level keys, without duplicates."""
    checks = tuple(checks)
    keys: list[str] = []
    if facts.annotations_read:
        for check in checks:
            signature = normalize_signature(check, facts.annotations.get(check))
            if signature != UNREAD:
                keys.append(cause_key(slug, signature))
    keys.extend(ci_red_cause_key(slug, check) for check in checks)
    return tuple(dict.fromkeys(keys))


class ProtocolCiRedFactsReader(Protocol):
    def read(self, event: ModelCiRunFailedEvent) -> ModelCiRedFacts: ...


class GhCiRedFactsReader:
    """Read only: newest check runs via REST GET, failure annotations via one GraphQL query per chunk.

    A failed check read leaves conclusions and base unread; a failed annotation
    read leaves annotations unread (check-level clustering).
    """

    @staticmethod
    def _checks(slug: str, ref: str) -> dict[str, str]:
        result = subprocess.run(
            [
                "gh",
                "api",
                "--method",
                "GET",
                f"repos/{slug}/commits/{ref}/check-runs?per_page=100",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
        )
        payload = json.loads(result.stdout)
        runs = payload["check_runs"]
        if not isinstance(runs, list):
            raise ValueError("check_runs must be a list")
        newest: dict[str, tuple[tuple[str, int], str]] = {}
        for run in runs:
            key = (str(run.get("started_at") or ""), int(run["id"]))
            name = str(run["name"])
            conclusion = str(run.get("conclusion") or "").lower()
            if name not in newest or key > newest[name][0]:
                newest[name] = (key, conclusion)
        return {name: conclusion for name, (_, conclusion) in newest.items()}

    @staticmethod
    def _annotations(
        event: ModelCiRunFailedEvent,
    ) -> tuple[dict[str, str] | None, dict[int, dict[str, str]]]:
        """The first failure annotation per failing check of the PR and its armed peers, each at its own head."""
        slug = ci_red_repo_slug(event.repo)
        heads = {event.pr_number: event.head_sha} | {
            peer.pr_number: peer.head_sha for peer in event.peers if peer.armed
        }
        numbers = sorted(heads)
        read: dict[int, dict[str, str]] = {}
        for start in range(0, len(numbers), ANNOTATION_CHUNK):
            chunk = numbers[start : start + ANNOTATION_CHUNK]
            query = annotation_query([(slug, number) for number in chunk])
            result = subprocess.run(
                ["gh", "api", "graphql", "-f", f"query={query}"],
                capture_output=True,
                text=True,
                check=True,
                timeout=20,
            )
            data = json.loads(result.stdout).get("data") or {}
            for i, number in enumerate(chunk):
                node = (data.get(f"a{i}") or {}).get("pullRequest")
                annotations = first_failure_annotations(node, heads[number])
                if annotations is not None:
                    read[number] = annotations
        own = read.pop(event.pr_number, None)
        return own, read

    def read(self, event: ModelCiRunFailedEvent) -> ModelCiRedFacts:
        checks: dict[str, object] = {}
        try:
            slug = ci_red_repo_slug(event.repo)
            head = self._checks(slug, event.head_sha)
            base = self._checks(slug, event.base)
            checks = {
                "check_conclusions": head,
                "base_red_checks": tuple(
                    sorted(
                        name
                        for name, conclusion in base.items()
                        if conclusion in {"failure", "timed_out"}
                    )
                ),
                "base_read": True,
            }
        except Exception:
            checks = {}
        annotations: dict[str, object] = {}
        try:
            own, peers = self._annotations(event)
            if own is not None:
                annotations = {
                    "annotations": own,
                    "peer_annotations": peers,
                    "annotations_read": True,
                }
        except Exception:
            annotations = {}
        return ModelCiRedFacts.model_validate({"event": event, **checks, **annotations})


class HandlerCiRedTriage:
    """Durable decision and owner claims; identities never read the clock.

    A decision is made once per repo, head and check, and an owner is started
    once per owner key, across runtime restarts: both are claims read back from
    the pr_lifecycle_ledger_entries projection the state reducer writes from
    ci-red-triage-decided. The bounded in-process caches only answer for the
    decisions this process made before the projection has them. A cause the
    handler claims absorbs its members, so a later PR-own decision for a member
    joins the cause. An unreadable claim store withholds every start.
    """

    MEMORY_LIMIT = 4096
    subscribe_topic = CI_RUN_FAILED_TOPIC_V1
    published_event_topics = {
        ModelPrLifecycleStartCommand: PR_LIFECYCLE_ORCHESTRATOR_START_TOPIC_V1,
        ModelCiRedTriageDecided: CI_RED_TRIAGE_DECIDED_TOPIC_V1,
    }

    def __init__(
        self,
        *,
        facts_reader: ProtocolCiRedFactsReader | None = None,
        classifier: Callable[[ModelCiRedFacts], ModelCiRedClassification] | None = None,
        act: bool | None = None,
        claims: ProtocolCiRedClaims | None = None,
    ) -> None:
        self._facts_reader = (
            facts_reader if facts_reader is not None else GhCiRedFactsReader()
        )
        self._classifier = classifier
        if act is None or claims is None:
            with (
                Path(__file__).resolve().parents[1] / "contract.yaml"
            ).open() as stream:
                block = yaml.safe_load(stream)["ci_red_triage"]
            if act is None:
                act = bool(block["act"])
            if claims is None:
                claims = claims_from_contract(block["claims"])
        self._act = act
        self._claims = claims
        self._seen_events: OrderedDict[str, str] = OrderedDict()
        self._decided: OrderedDict[str, None] = OrderedDict()
        self._owners: OrderedDict[str, str] = OrderedDict()
        self._absorbed: OrderedDict[tuple[str, int], str] = OrderedDict()

    @staticmethod
    def _remember(cache: OrderedDict[_K, _V], key: _K, value: _V, limit: int) -> None:
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > limit:
            cache.popitem(last=False)

    async def _read_claim(self, read: Callable[..., _V], *args: object) -> _V:
        return await asyncio.to_thread(read, *args)

    async def _was_decided(self, decision_key: str, unread: list[str]) -> bool:
        if decision_key in self._decided:
            self._decided.move_to_end(decision_key)
            return True
        try:
            return await self._read_claim(self._claims.decided, decision_key)
        except CiRedClaimsUnreadError:
            unread.append("decision claim")
            return False

    async def _owner_run(self, owner_key: str) -> str | None:
        """The claimed owner's run id; raises CiRedClaimsUnreadError when unread."""
        if owner_key in self._owners:
            self._owners.move_to_end(owner_key)
            return self._owners[owner_key]
        if await self._read_claim(self._claims.owned, owner_key):
            return ci_red_owner_run_id(owner_key)
        return None

    async def _absorbing_cause(
        self, slug: str, pr_number: int, cause_keys: tuple[str, ...]
    ) -> str | None:
        cached = self._absorbed.get((slug, pr_number))
        if cached is not None:
            return cached
        return await self._read_claim(
            self._claims.absorbing_cause, pr_number, cause_keys
        )

    async def handle(
        self, request: ModelCiRunFailedEvent | Mapping[str, object]
    ) -> ModelHandlerOutput[None]:
        event = (
            request
            if isinstance(request, ModelCiRunFailedEvent)
            else ModelCiRunFailedEvent.model_validate(request)
        )
        if event.event_id in self._seen_events:
            decision_key = self._seen_events[event.event_id]
            self._seen_events.move_to_end(event.event_id)
            if decision_key in self._decided:
                self._decided.move_to_end(decision_key)
            return self._output(decision_key, ())
        try:
            facts = await asyncio.to_thread(self._facts_reader.read, event)
        except Exception:
            facts = ModelCiRedFacts(event=event)
        if self._classifier is None:
            # Same lazy sibling sub-handler composition as _ensure_sub_handlers.
            # The collaborating models themselves are in the shared events package.
            from omnimarket.nodes.node_pr_lifecycle_triage_compute.handlers.handler_classify_ci_red import (
                HandlerClassifyCiRed,
            )

            self._classifier = HandlerClassifyCiRed()
        classification = self._classifier(facts)
        slug = ci_red_repo_slug(event.repo)
        decision_key = (
            f"{slug}#{event.pr_number}@{event.head_sha}:{classification.check}"
        )
        unread: list[str] = []
        events: list[ModelPrLifecycleStartCommand | ModelCiRedTriageDecided] = []
        if not await self._was_decided(decision_key, unread):
            events = await self._decide(event, facts, classification, slug, unread)
            self._remember(self._decided, decision_key, None, self.MEMORY_LIMIT)
        self._remember(
            self._seen_events, event.event_id, decision_key, self.MEMORY_LIMIT
        )
        return self._output(decision_key, tuple(events))

    async def _decide(
        self,
        event: ModelCiRunFailedEvent,
        facts: ModelCiRedFacts,
        classification: ModelCiRedClassification,
        slug: str,
        unread: list[str],
    ) -> list[ModelPrLifecycleStartCommand | ModelCiRedTriageDecided]:
        events: list[ModelPrLifecycleStartCommand | ModelCiRedTriageDecided] = []
        owner_key = classification.owner_key
        run_id = None
        action_applied = False
        start_evidence = ""
        absorbed: list[str] = []
        claims_read = True
        owner_run = None
        if event.armed:
            try:
                if classification.red_class == EnumCiRedClass.PR_OWN:
                    # A member absorbed by a claimed cause is the cause's, whatever
                    # its peers said: the detector's index is empty after a restart.
                    absorbing = await self._absorbing_cause(
                        slug,
                        event.pr_number,
                        _member_cause_keys(slug, facts, event.failing_checks),
                    )
                    if absorbing is not None:
                        owner_key = absorbing
                owner_run = await self._owner_run(owner_key)
            except CiRedClaimsUnreadError:
                unread.append("owner claim")
                claims_read = False
        if not event.armed:
            action = EnumCiRedAction.RECORD_ONLY
        elif owner_run is not None:
            action = EnumCiRedAction.JOINED_OWNER
            run_id = owner_run
        else:
            action = OWNER_ACTIONS[classification.red_class]
            run_id = ci_red_owner_run_id(owner_key)
            if classification.red_class == EnumCiRedClass.SHARED_CAUSE:
                absorbed = await self._absorbed_members(event, classification, slug)
            if not self._act:
                # A dry-run lifecycle run reads GitHub on the operator login and cannot act;
                # shadow mode starts no run, so it claims no owner either.
                start_evidence = " start=withheld:act=false"
            elif not claims_read:
                start_evidence = " start=withheld:claims-unread"
            else:
                events.append(
                    ModelPrLifecycleStartCommand(
                        run_id=run_id,
                        correlation_id=ci_red_owner_correlation_id(owner_key),
                        repos=slug,
                        pr_numbers=classification.members
                        if classification.red_class == EnumCiRedClass.SHARED_CAUSE
                        else (event.pr_number,),
                        fix_only=True,
                        dry_run=not self._act,
                    )
                )
                action_applied = True
                self._remember(self._owners, owner_key, run_id, self.MEMORY_LIMIT)
                if classification.red_class == EnumCiRedClass.SHARED_CAUSE:
                    for member in classification.members:
                        self._remember(
                            self._absorbed, (slug, member), owner_key, self.MEMORY_LIMIT
                        )
        missing = [
            check
            for check in event.failing_checks
            if not facts.check_conclusions.get(check)
        ]
        if missing:
            unread.insert(0, "head conclusions: " + ", ".join(missing))
        if not facts.base_read:
            unread.insert(1 if missing else 0, "base checks")
        if not facts.annotations_read:
            unread.insert(bool(missing) + (not facts.base_read), "annotations")
        absorbed_evidence = f" absorbed={','.join(absorbed)}" if absorbed else ""
        evidence = (
            f"class={classification.red_class} check={classification.check} action={action} "
            f"owner_key={owner_key} members={classification.members} run_id={run_id} "
            f"unread={'; '.join(unread) or 'none'}{start_evidence}{absorbed_evidence}; "
            f"{classification.reason}"
        )
        decision_key = (
            f"{slug}#{event.pr_number}@{event.head_sha}:{classification.check}"
        )
        events.append(
            ModelCiRedTriageDecided(
                correlation_id=ci_red_decision_correlation_id(decision_key),
                decision_key=decision_key,
                owner_key=owner_key,
                event_id=event.event_id,
                repo=slug,
                pr_number=event.pr_number,
                head_sha=event.head_sha,
                check=classification.check,
                red_class=classification.red_class,
                action=action,
                action_applied=action_applied,
                orchestrator_run_id=run_id,
                members=classification.members,
                initial_state=f"ci_red:{classification.red_class}",
                evidence=" ".join(evidence.splitlines()),
                observed_at=event.observed_at,
            )
        )
        return events

    async def _absorbed_members(
        self,
        event: ModelCiRunFailedEvent,
        classification: ModelCiRedClassification,
        slug: str,
    ) -> list[str]:
        """Run ids of members' PR-own owners on this check that the cause absorbs."""
        heads = {peer.pr_number: peer.head_sha for peer in event.peers}
        heads[event.pr_number] = event.head_sha
        absorbed = []
        for member in classification.members:
            if member not in heads:
                continue
            pr_own = f"{slug}#{member}@{heads[member]}:{classification.check}"
            try:
                run = await self._owner_run(pr_own)
            except CiRedClaimsUnreadError:
                continue
            if run is not None:
                absorbed.append(run)
        return absorbed

    @staticmethod
    def _output(
        decision_key: str,
        events: tuple[ModelPrLifecycleStartCommand | ModelCiRedTriageDecided, ...],
    ) -> ModelHandlerOutput[None]:
        correlation_id = ci_red_decision_correlation_id(decision_key)
        return ModelHandlerOutput.for_orchestrator(
            input_envelope_id=correlation_id,
            correlation_id=correlation_id,
            handler_id="node_pr_lifecycle_orchestrator.ci_red_triage",
            events=events,
        )
