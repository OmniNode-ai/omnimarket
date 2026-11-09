# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One runtime boot against the boot set its deployment declares (one-runtime plan, task T28).

The dev runtime read on 2026-11-09 announced 1,153 contracts, attached 638 and updated its topic subscriptions 439
times before it served a request, with no declared set naming what the deployment needs. The handler judges one
boot's observation against the declaration the deployment's overlay supplies:

- no declared set is FAIL, because the runtime then loads every contract it discovers (today's start);
- a declared name no contract announces is FAIL (``CONTRACT_NOT_ANNOUNCED``);
- an attached contract or a subscribed topic outside the declared set is FAIL, with the names listed;
- a declared contract the boot did not attach is FAIL, so a continuous consumer cannot be dropped silently;
- a boot that was not read is UNKNOWN, and UNKNOWN fails; there is no warning status.

The verdict also carries ``selection_env``, the value the overlay hands the runtime as ONEX_RUNTIME_NODE_SELECTION.
Nothing in this handler holds a count or a contract name.
"""

from __future__ import annotations

import json

from pydantic import ValidationError

from omnimarket.models.model_runtime_boot_set import (
    BootSetRule,
    ModelRuntimeBootSetRequest,
    ModelRuntimeBootSetVerdict,
)


def _ordered(names: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(names))


class HandlerRuntimeBootSet:
    """``handle(ModelRuntimeBootSetRequest) -> ModelRuntimeBootSetVerdict``."""

    def handle(self, request: ModelRuntimeBootSetRequest) -> ModelRuntimeBootSetVerdict:
        observation = request.observation
        declaration = request.declaration
        declared = _ordered(declaration.contracts) if declaration else ()
        announced = {entry.contract: entry for entry in request.catalog}
        loaded = _ordered(observation.attached_contracts)
        subscribed = _ordered(observation.subscribed_topics)

        declared_topics: tuple[str, ...] = ()
        if declaration is not None:
            declared_topics = _ordered(
                tuple(
                    topic
                    for name in declared
                    if name in announced
                    for topic in announced[name].subscribe_topics
                )
                + declaration.kernel_topics
            )

        unannounced = tuple(name for name in declared if name not in announced)
        excess_contracts = tuple(name for name in loaded if name not in declared)
        excess_topics = tuple(t for t in subscribed if t not in declared_topics)
        missing = tuple(name for name in declared if name not in loaded)

        rules: list[BootSetRule] = []
        unread = bool(observation.read_error) or not (loaded or subscribed)
        if unread:
            rules.append("BOOT_NOT_READ")
        elif declaration is None or not declared:
            rules.append("NO_DECLARED_SET")
        else:
            if declaration.deployment != request.deployment:
                rules.append("DECLARATION_FOR_OTHER_DEPLOYMENT")
            if unannounced:
                rules.append("CONTRACT_NOT_ANNOUNCED")
            if excess_contracts:
                rules.append("LOADED_OUTSIDE_DECLARED_SET")
            if excess_topics:
                rules.append("SUBSCRIBED_OUTSIDE_DECLARED_SET")
            if missing:
                rules.append("DECLARED_NOT_LOADED")

        status = "UNKNOWN" if unread else ("FAIL" if rules else "PASS")
        return ModelRuntimeBootSetVerdict(
            correlation_id=request.correlation_id,
            deployment=request.deployment,
            status=status,
            rules=tuple(rules),
            declared_contract_count=len(declared),
            loaded_contract_count=len(loaded),
            contracts_over_declared=len(excess_contracts) if declared else len(loaded),
            declared_topic_count=len(declared_topics),
            subscribed_topic_count=len(subscribed),
            topics_over_declared=len(excess_topics) if declared else len(subscribed),
            excess_contracts=excess_contracts,
            excess_topics=excess_topics,
            missing_contracts=missing,
            unannounced_contracts=unannounced,
            selection_env=",".join(declared),
            time_to_healthy_seconds=observation.time_to_healthy_seconds,
            memory_mib=observation.memory_mib,
            detail=_detail(request.deployment, status, rules, observation.read_error),
        )


def _detail(
    deployment: str, status: str, rules: list[BootSetRule], read_error: str
) -> str:
    if not rules:
        return f"{deployment}: boot stayed inside its declared set"
    text = f"{deployment}: {status} on {','.join(rules)}"
    return f"{text} ({read_error})" if read_error else text


def answer_command(
    payload: bytes, handler: HandlerRuntimeBootSet, topics: dict[str, str]
) -> list[tuple[str, bytes]]:
    """One command message in, the events it produces out, each as (topic, JSON bytes).

    A payload that is not a request is answered with a typed failure event, never dropped.
    """
    try:
        request = ModelRuntimeBootSetRequest.model_validate_json(payload)
    except ValidationError as exc:
        fields = sorted(
            {".".join(str(p) for p in e["loc"]) or "<document>" for e in exc.errors()}
        )
        failure = {"correlation_id": "unknown", "failure": f"bad-payload:{fields}"}
        return [(topics["boot_set_judge_failed"], json.dumps(failure).encode())]
    verdict = handler.handle(request)
    topic = topics[
        "boot_set_judged" if verdict.status == "PASS" else "boot_set_violated"
    ]
    return [(topic, verdict.model_dump_json().encode())]


__all__ = [
    "HandlerRuntimeBootSet",
    "answer_command",
]
