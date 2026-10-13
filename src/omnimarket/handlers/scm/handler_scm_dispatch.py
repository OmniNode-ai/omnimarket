# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``onex scm`` verb table and its bus call (OMN-20913).

Every verb names one operation of a source-control node contract. Topics come
only from those contracts: the command topic is ``runtime_dispatch.command_topic``
and the terminal topics are ``runtime_dispatch.terminal_events`` (or the
contract's ``terminal_event`` when it declares no failure terminal). The call
publishes the node's typed request in an event envelope and waits for the one
terminal event naming it, by parent envelope id or correlation id. No bespoke
RPC and no new transport: the bus is the one ``onex lab-work`` and
``onex work-ledger`` already open.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from importlib import resources
from typing import Final

import yaml
from omnibase_core.event_bus.util_consumer_group import derive_service_group_id
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from pydantic import BaseModel, JsonValue

from omnimarket.delegated_test_loop.lab_run_bus import (
    ProtocolBusMessage,
    ProtocolLabRunBus,
    event_type_for,
)
from omnimarket.lab_work.bus import _bytes, _subscribe, _uuid_or_none
from omnimarket.models.scm import (
    EnumScmLocalRead,
    EnumScmNode,
    ModelScmBusReceipt,
    ModelScmLocalVerb,
    ModelScmRefusedVerb,
    ModelScmTerminal,
    ModelScmTopics,
    ModelScmVerb,
)

GROUP_SERVICE: Final[str] = "omnimarket"

_G = EnumScmNode.GATEWAY
_W = EnumScmNode.WRITE

#: The verbs ``onex scm`` serves: every operation slice 1 put on the two
#: source-control node contracts, named for the gh verb it replaces.
SCM_VERBS: Final[tuple[ModelScmVerb, ...]] = (
    ModelScmVerb(
        verb="pr-view",
        node=_G,
        operation="pr_text",
        replaces="gh pr view",
        local_read=EnumScmLocalRead.PR_VIEW,
    ),
    ModelScmVerb(
        verb="pr-status",
        node=_G,
        operation="pr_status",
        replaces="gh pr view --json mergeStateStatus",
        local_read=EnumScmLocalRead.PR_STATUS,
    ),
    ModelScmVerb(
        verb="pr-checks",
        node=_G,
        operation="ci_checks",
        replaces="gh pr checks",
        local_read=EnumScmLocalRead.PR_CHECKS,
    ),
    ModelScmVerb(
        verb="pr-list",
        node=_G,
        operation="open_prs_list",
        replaces="gh pr list",
        local_read=EnumScmLocalRead.PR_LIST,
    ),
    ModelScmVerb(
        verb="pr-merge-commit",
        node=_G,
        operation="merge_commit_sha",
        replaces="gh pr view --json mergeCommit",
        local_read=EnumScmLocalRead.PR_MERGE_COMMIT,
    ),
    ModelScmVerb(
        verb="pr-review",
        node=_G,
        operation="review_gate",
        replaces="gh pr view --json reviewDecision",
    ),
    ModelScmVerb(
        verb="pr-ticket",
        node=_G,
        operation="ticket_ref",
        replaces="gh pr view --json headRefName",
    ),
    ModelScmVerb(
        verb="branch-protection",
        node=_G,
        operation="branch_protection",
        replaces="gh api repos/<o>/<r>/branches/<b>/protection",
    ),
    ModelScmVerb(
        verb="run-list", node=_G, operation="workflow_runs", replaces="gh run list"
    ),
    ModelScmVerb(
        verb="run-view", node=_G, operation="run_jobs", replaces="gh run view"
    ),
    ModelScmVerb(
        verb="job-log",
        node=_G,
        operation="job_log_tail",
        replaces="gh api repos/<o>/<r>/actions/jobs/<id>/logs",
    ),
    ModelScmVerb(
        verb="run-download", node=_G, operation="artifacts", replaces="gh run download"
    ),
    ModelScmVerb(
        verb="release-list", node=_G, operation="releases", replaces="gh release list"
    ),
    ModelScmVerb(
        verb="pr-heads",
        node=_G,
        operation="pr_heads",
        replaces="gh pr list --state merged --json headRefName,headRefOid",
        local_read=EnumScmLocalRead.PR_HEADS,
    ),
    ModelScmVerb(
        verb="pr-create", node=_W, operation="pr_create", replaces="gh pr create"
    ),
    ModelScmVerb(verb="pr-edit", node=_W, operation="pr_edit", replaces="gh pr edit"),
    ModelScmVerb(
        verb="pr-ready", node=_W, operation="pr_ready", replaces="gh pr ready"
    ),
    ModelScmVerb(
        verb="pr-close", node=_W, operation="pr_close", replaces="gh pr close"
    ),
    ModelScmVerb(
        verb="pr-comment", node=_W, operation="pr_comment", replaces="gh pr comment"
    ),
    ModelScmVerb(
        verb="workflow-run",
        node=_W,
        operation="workflow_dispatch",
        replaces="gh workflow run",
    ),
)

#: Verbs no node contract has an operation for: answered from the canonical
#: clones and the watcher state alone. The measured gh read refusals they
#: replace (the shared call log) are PR commit lists, file contents, one commit
#: and one commit's check runs.
SCM_LOCAL_VERBS: Final[tuple[ModelScmLocalVerb, ...]] = (
    ModelScmLocalVerb(
        verb="pr-commits",
        read=EnumScmLocalRead.PR_COMMITS,
        replaces="gh api repos/<o>/<r>/pulls/<n>/commits",
    ),
    ModelScmLocalVerb(
        verb="file-read",
        read=EnumScmLocalRead.FILE_CONTENTS,
        replaces="gh api repos/<o>/<r>/contents/<path>",
    ),
    ModelScmLocalVerb(
        verb="commit-view",
        read=EnumScmLocalRead.COMMIT_VIEW,
        replaces="gh api repos/<o>/<r>/commits/<ref>",
    ),
    ModelScmLocalVerb(
        verb="commit-checks",
        read=EnumScmLocalRead.COMMIT_CHECKS,
        replaces="gh api repos/<o>/<r>/commits/<sha>/check-runs",
    ),
)

LANDING_CONTROLLER: Final[str] = (
    "the landing controller (hand the PR off with /omni:pr-handoff, needs=land)"
)

#: Verbs with no node operation. Not invented here: refused, naming where the
#: work is done today.
SCM_REFUSED_VERBS: Final[tuple[ModelScmRefusedVerb, ...]] = tuple(
    ModelScmRefusedVerb(verb=verb, handled_by=LANDING_CONTROLLER)
    for verb in (
        "pr-merge",
        "merge",
        "arm",
        "pr-arm",
        "rerun",
        "run-rerun",
        "pr-rerun",
        "update-branch",
        "pr-update-branch",
    )
)


def verb(name: str) -> ModelScmVerb:
    """The served verb named ``name``."""
    for spec in SCM_VERBS:
        if spec.verb == name:
            return spec
    raise KeyError(name)


def load_contract(node: EnumScmNode) -> dict[str, JsonValue]:
    loaded = yaml.safe_load(
        resources.files(f"omnimarket.nodes.{node.value}")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    if not isinstance(loaded, dict):
        raise ValueError(f"{node.value} contract is not a mapping")
    return loaded


def load_topics(node: EnumScmNode) -> ModelScmTopics:
    """The node's ``runtime_dispatch`` topics, as its contract declares them."""
    contract = load_contract(node)
    dispatch = contract.get("runtime_dispatch")
    if not isinstance(dispatch, dict):
        raise ValueError(f"{node.value} contract declares no runtime_dispatch")
    terminals = dispatch.get("terminal_events")
    if isinstance(terminals, dict):
        return ModelScmTopics(
            command=str(dispatch["command_topic"]),
            success=str(terminals["success"]),
            failure=str(terminals["failure"]),
        )
    return ModelScmTopics(
        command=str(dispatch["command_topic"]),
        success=str(contract["terminal_event"]),
    )


def contract_operations(node: EnumScmNode) -> tuple[str, ...]:
    """The ``operations`` names the node contract declares."""
    operations = load_contract(node).get("operations")
    if not isinstance(operations, list):
        return ()
    return tuple(
        str(entry["name"])
        for entry in operations
        if isinstance(entry, dict) and "name" in entry
    )


class ScmBusCaller:
    """Publish one node command and await the terminal event naming it."""

    def __init__(self, bus: ProtocolLabRunBus) -> None:
        self._bus = bus
        # One group per call, never shared: every caller sees every terminal
        # and picks out its own.
        self._group = (
            f"{derive_service_group_id('scm_command_client', service=GROUP_SERVICE)}"
            f".{uuid.uuid4().hex[:12]}"
        )

    async def call(
        self,
        *,
        node: EnumScmNode,
        request: BaseModel,
        correlation_id: uuid.UUID,
        timeout_s: float,
    ) -> ModelScmTerminal:
        """Raises ``TimeoutError`` when no terminal names the command in time."""
        topics = load_topics(node)
        envelope = ModelEventEnvelope[dict[str, object]](
            payload=request.model_dump(mode="json"),
            correlation_id=correlation_id,
            event_type=event_type_for(topics.command),
        )
        future: asyncio.Future[ModelScmTerminal] = (
            asyncio.get_running_loop().create_future()
        )

        def on_terminal(topic: str) -> Callable[[ProtocolBusMessage], Awaitable[None]]:
            async def on_message(message: ProtocolBusMessage) -> None:
                if future.done():
                    return
                try:
                    raw = json.loads(message.value)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return
                if not isinstance(raw, dict):
                    return
                payload = raw.get("payload")
                payload_dict = payload if isinstance(payload, dict) else {}
                names_it = (
                    _uuid_or_none(raw.get("parent_envelope_id")) == envelope.envelope_id
                    or _uuid_or_none(raw.get("correlation_id")) == correlation_id
                    or _uuid_or_none(payload_dict.get("correlation_id"))
                    == correlation_id
                )
                if not names_it:
                    return
                partition = getattr(message, "partition", None)
                offset = getattr(message, "offset", None)
                future.set_result(
                    ModelScmTerminal(
                        receipt=ModelScmBusReceipt(
                            correlation_id=correlation_id,
                            command_topic=topics.command,
                            command_envelope_id=envelope.envelope_id,
                            terminal_topic=topic,
                            terminal_envelope_id=_uuid_or_none(raw.get("envelope_id")),
                            terminal_partition=(
                                partition if isinstance(partition, int) else None
                            ),
                            terminal_offset=(
                                str(offset) if offset is not None else None
                            ),
                        ),
                        failed=topic == topics.failure,
                        payload=payload_dict,
                    )
                )

            return on_message

        unsubscribes: list[Callable[[], Awaitable[None]]] = []
        try:
            for topic in (topics.success, topics.failure):
                if topic is None:
                    continue
                unsubscribes.append(
                    await _subscribe(
                        self._bus, topic, on_terminal(topic), self._group, "latest"
                    )
                )
            await self._bus.publish(
                topics.command, str(correlation_id).encode("utf-8"), _bytes(envelope)
            )
            return await asyncio.wait_for(future, timeout=timeout_s)
        finally:
            for unsubscribe in unsubscribes:
                await unsubscribe()


__all__: list[str] = [
    "LANDING_CONTROLLER",
    "SCM_LOCAL_VERBS",
    "SCM_REFUSED_VERBS",
    "SCM_VERBS",
    "ScmBusCaller",
    "contract_operations",
    "load_topics",
    "verb",
]
