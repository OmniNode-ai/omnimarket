# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Claim a red-CI owner and return a scoped command through the existing sweep path."""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
from collections import OrderedDict
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

import yaml
from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput

from omnimarket.events.topics import (
    CI_RED_TRIAGE_DECIDED_TOPIC_V1,
    CI_RUN_FAILED_TOPIC_V1,
    PR_LIFECYCLE_ORCHESTRATOR_START_TOPIC_V1,
)
from omnimarket.models.ci_red_triage import (
    EnumCiRedAction,
    EnumCiRedClass,
    ModelCiRedClassification,
    ModelCiRedFacts,
    ModelCiRedTriageDecided,
    ModelCiRunFailedEvent,
    ci_red_repo_slug,
)
from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
    ModelPrLifecycleStartCommand,
)

OWNER_ACTIONS = {
    EnumCiRedClass.SHARED_CAUSE: EnumCiRedAction.START_CAUSE_OWNER,
    EnumCiRedClass.PR_OWN: EnumCiRedAction.START_PR_FIX,
    EnumCiRedClass.DEV_HEAD: EnumCiRedAction.START_DEV_CAUSE,
    EnumCiRedClass.RUNNER: EnumCiRedAction.RERUN_FAILED,
}


class ProtocolCiRedFactsReader(Protocol):
    def read(self, event: ModelCiRunFailedEvent) -> ModelCiRedFacts: ...


class GhCiRedFactsReader:
    """Read newest check runs via GET only; a failed read leaves facts unread."""

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

    def read(self, event: ModelCiRunFailedEvent) -> ModelCiRedFacts:
        try:
            slug = ci_red_repo_slug(event.repo)
            head = self._checks(slug, event.head_sha)
            base = self._checks(slug, event.base)
            return ModelCiRedFacts(
                event=event,
                check_conclusions=head,
                base_red_checks=tuple(
                    sorted(
                        name
                        for name, conclusion in base.items()
                        if conclusion in {"failure", "timed_out"}
                    )
                ),
                base_read=True,
            )
        except Exception:
            return ModelCiRedFacts(event=event)


class HandlerCiRedTriage:
    """In-process bounded owner/decision caches; identities never read the clock."""

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
    ) -> None:
        self._facts_reader = (
            facts_reader if facts_reader is not None else GhCiRedFactsReader()
        )
        self._classifier = classifier
        if act is None:
            with (
                Path(__file__).resolve().parents[1] / "contract.yaml"
            ).open() as stream:
                contract = yaml.safe_load(stream)
            act = bool(contract["ci_red_triage"]["act"])
        self._act = act
        self._seen_events: OrderedDict[str, str] = OrderedDict()
        self._decided: OrderedDict[str, None] = OrderedDict()
        self._owners: OrderedDict[str, str] = OrderedDict()

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
            correlation_id = uuid5(
                NAMESPACE_URL, "onex:ci-red-decision:" + decision_key
            )
            return ModelHandlerOutput.for_orchestrator(
                input_envelope_id=correlation_id,
                correlation_id=correlation_id,
                handler_id="node_pr_lifecycle_orchestrator.ci_red_triage",
                events=(),
            )
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
        correlation_id = uuid5(NAMESPACE_URL, "onex:ci-red-decision:" + decision_key)
        events: list[ModelPrLifecycleStartCommand | ModelCiRedTriageDecided] = []
        if decision_key in self._decided:
            self._decided.move_to_end(decision_key)
        else:
            owner_key = classification.owner_key
            run_id = None
            action_applied = False
            if not event.armed:
                action = EnumCiRedAction.RECORD_ONLY
            elif owner_key in self._owners:
                action = EnumCiRedAction.JOINED_OWNER
                run_id = self._owners[owner_key]
                self._owners.move_to_end(owner_key)
            else:
                action = OWNER_ACTIONS[classification.red_class]
                run_id = "ci-red-" + hashlib.sha256(owner_key.encode()).hexdigest()[:16]
                events.append(
                    ModelPrLifecycleStartCommand(
                        run_id=run_id,
                        correlation_id=uuid5(
                            NAMESPACE_URL, "onex:ci-red-owner:" + owner_key
                        ),
                        repos=slug,
                        pr_numbers=classification.members
                        if classification.red_class == EnumCiRedClass.SHARED_CAUSE
                        else (event.pr_number,),
                        fix_only=True,
                        dry_run=not self._act,
                    )
                )
                self._owners[owner_key] = run_id
                while len(self._owners) > self.MEMORY_LIMIT:
                    self._owners.popitem(last=False)
                action_applied = True
            unread = []
            missing = [
                check
                for check in event.failing_checks
                if not facts.check_conclusions.get(check)
            ]
            if missing:
                unread.append("head conclusions: " + ", ".join(missing))
            if not facts.base_read:
                unread.append("base checks")
            evidence = (
                f"class={classification.red_class} check={classification.check} action={action} "
                f"owner_key={owner_key} members={classification.members} run_id={run_id} "
                f"unread={'; '.join(unread) or 'none'}; {classification.reason}"
            )
            events.append(
                ModelCiRedTriageDecided(
                    correlation_id=correlation_id,
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
            self._decided[decision_key] = None
            while len(self._decided) > self.MEMORY_LIMIT:
                self._decided.popitem(last=False)
        self._seen_events[event.event_id] = decision_key
        while len(self._seen_events) > self.MEMORY_LIMIT:
            self._seen_events.popitem(last=False)
        return ModelHandlerOutput.for_orchestrator(
            input_envelope_id=correlation_id,
            correlation_id=correlation_id,
            handler_id="node_pr_lifecycle_orchestrator.ci_red_triage",
            events=tuple(events),
        )
