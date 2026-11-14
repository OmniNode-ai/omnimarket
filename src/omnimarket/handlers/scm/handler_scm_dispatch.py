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
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from pydantic import BaseModel, JsonValue

from omnimarket.delegated_test_loop.lab_run_bus import (
    ProtocolBusMessage,
    ProtocolLabRunBus,
    event_type_for,
)
from omnimarket.handlers.scm.scm_terminal_match import terminal_payload
from omnimarket.lab_work.bus import _bytes, _uuid_or_none
from omnimarket.models.scm import (
    EnumScmLocalRead,
    EnumScmNode,
    ModelScmBusReceipt,
    ModelScmTerminal,
    ModelScmTopics,
    ModelScmVerb,
)
from omnimarket.models.scm.model_scm_gateway_verbs import (
    SCM_GATEWAY_VERBS as SCM_GATEWAY_VERBS,
)
from omnimarket.models.scm.model_scm_local_verbs import (
    SCM_LOCAL_VERBS as SCM_LOCAL_VERBS,
)
from omnimarket.models.scm.model_scm_refused_verbs import (
    SCM_REFUSED_VERBS as SCM_REFUSED_VERBS,
)
from omnimarket.nodes.node_lab_work_unit_effect.protocols import require_ungrouped

_G = EnumScmNode.GATEWAY
_W = EnumScmNode.WRITE

#: The verbs ``onex scm`` serves: every operation slice 1 put on the two
#: source-control node contracts, named for the gh verb it replaces.
SCM_VERBS: Final[tuple[ModelScmVerb, ...]] = (
    ModelScmVerb(
        verb="runner-group-get",
        node=_W,
        operation="runner_group_get",
        replaces="gh api --method GET /orgs/{o}/actions/runner-groups/{id}",
    ),
    *(
        ModelScmVerb(verb=v, node=_G, operation=op, replaces=gh, local_read=read)
        for v, op, gh, read in (
            ("pr-view", "pr_text", "gh pr view", EnumScmLocalRead.PR_VIEW),
            (
                "pr-status",
                "pr_status",
                "gh pr view --json mergeStateStatus",
                EnumScmLocalRead.PR_STATUS,
            ),
            ("pr-checks", "ci_checks", "gh pr checks", EnumScmLocalRead.PR_CHECKS),
            ("pr-list", "open_prs_list", "gh pr list", EnumScmLocalRead.PR_LIST),
            (
                "pr-merge-commit",
                "merge_commit_sha",
                "gh pr view --json mergeCommit",
                EnumScmLocalRead.PR_MERGE_COMMIT,
            ),
            ("run-list", "workflow_runs", "gh run list", None),
            ("run-view", "run_jobs", "gh run view", None),
            ("job-log-fill", "job_log_fill", "gh job log", None),
            ("run-download", "artifacts", "gh run download", None),
            ("release-list", "releases", "gh release list", None),
            (
                "pr-heads",
                "pr_heads",
                "gh pr list --state merged --json headRefName,headRefOid",
                EnumScmLocalRead.PR_HEADS,
            ),
            # OMN-21146: the reads node_dod_verify sends the gateway.
            ("open-pr-facts", "open_pr_facts", "gh pr list", None),
        )
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
        verb="pr-thread-reply",
        node=_W,
        operation="pr_thread_reply",
        replaces="gh api --method POST /repos/{o}/{r}/pulls/{n}/comments/{id}/replies",
    ),
    ModelScmVerb(
        verb="pr-thread-resolve",
        node=_W,
        operation="pr_thread_resolve",
        replaces="gh api graphql resolveReviewThread",
    ),
    ModelScmVerb(
        verb="pr-review-post",
        node=_W,
        operation="pr_review_post",
        replaces="gh api --method POST /repos/{o}/{r}/pulls/{n}/reviews",
    ),
    ModelScmVerb(
        verb="workflow-run",
        node=_W,
        operation="workflow_dispatch",
        replaces="gh workflow run",
    ),
    ModelScmVerb(
        verb="check-run-create",
        node=_W,
        operation="check_run_create",
        replaces="gh api --method POST /repos/{o}/{r}/check-runs",
    ),
    ModelScmVerb(
        verb="pr-label",
        node=_W,
        operation="pr_label",
        replaces="gh api --method POST /repos/{o}/{r}/issues/{n}/labels",
    ),
    ModelScmVerb(
        verb="variable-set",
        node=_W,
        operation="repo_variable_set",
        replaces="gh variable set",
    ),
    ModelScmVerb(
        verb="variable-get",
        node=_W,
        operation="repo_variable_get",
        replaces="gh variable get",
    ),
    ModelScmVerb(
        verb="runner-list",
        node=_W,
        operation="runner_list",
        replaces="gh api --method GET /orgs/{o}/actions/runners",
    ),
    ModelScmVerb(
        verb="runner-remove",
        node=_W,
        operation="runner_remove",
        replaces="gh api --method DELETE /orgs/{o}/actions/runners/{id}",
    ),
    *SCM_GATEWAY_VERBS,
)


def verb(name: str) -> ModelScmVerb:
    """The served verb named ``name``."""
    return {spec.verb: spec for spec in SCM_VERBS}[name]


def load_contract(node: EnumScmNode) -> dict[str, JsonValue]:
    package = resources.files(f"omnimarket.nodes.{node.value}")
    loaded = yaml.safe_load((package / "contract.yaml").read_text(encoding="utf-8"))
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
        command=str(dispatch["command_topic"]), success=str(contract["terminal_event"])
    )


def contract_operations(node: EnumScmNode) -> tuple[str, ...]:
    """The ``operations`` names the node contract declares."""
    operations = load_contract(node).get("operations")
    return tuple(
        str(entry["name"])
        for entry in (operations if isinstance(operations, list) else ())
        if isinstance(entry, dict) and "name" in entry
    )


class ScmBusCaller:
    """Publish one node command and await the terminal event naming it."""

    def __init__(self, bus: ProtocolLabRunBus) -> None:
        # No consumer group (OMN-21818): every caller sees every terminal and picks out
        # its own, so it reads them from the log end and leaves nothing on the broker.
        self._bus = require_ungrouped(bus)

    async def call(
        self,
        *,
        node: EnumScmNode,
        request: BaseModel,
        correlation_id: uuid.UUID,
        timeout_s: float,
    ) -> ModelScmTerminal:
        """Raises ``TimeoutError`` when no terminal names the command in time."""
        from omnimarket.handlers.scm.handler_scm_consumer_diagnostics import (
            ScmNoTerminalError,
            diagnose_scm_timeout,
        )

        topics = load_topics(node)
        try:
            return await self.call_topics(
                topics=topics,
                request=request,
                correlation_id=correlation_id,
                timeout_s=timeout_s,
            )
        except TimeoutError:
            raise ScmNoTerminalError(
                await diagnose_scm_timeout(self._bus, node, topics.command)
            ) from None

    async def call_topics(
        self,
        *,
        topics: ModelScmTopics,
        request: BaseModel,
        correlation_id: uuid.UUID,
        timeout_s: float,
    ) -> ModelScmTerminal:
        """Shared contract-topic transport for SCM and ticket commands."""
        envelope = ModelEventEnvelope[dict[str, object]](
            payload=request.model_dump(mode="json"),
            correlation_id=correlation_id,
            event_type=event_type_for(topics.command),
        )
        loop = asyncio.get_running_loop()
        future: asyncio.Future[ModelScmTerminal] = loop.create_future()

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
                body = terminal_payload(raw, envelope.envelope_id, correlation_id)
                if body is None:
                    return
                part = getattr(message, "partition", None)
                off = getattr(message, "offset", None)
                future.set_result(
                    ModelScmTerminal(
                        receipt=ModelScmBusReceipt(
                            correlation_id=correlation_id,
                            command_topic=topics.command,
                            command_envelope_id=envelope.envelope_id,
                            terminal_topic=topic,
                            terminal_envelope_id=_uuid_or_none(raw.get("envelope_id")),
                            terminal_partition=part if isinstance(part, int) else None,
                            terminal_offset=str(off) if off is not None else None,
                        ),
                        failed=topic == topics.failure,
                        payload=body,
                    )
                )

            return on_message

        unsubscribes: list[Callable[[], Awaitable[None]]] = []
        try:
            for topic in filter(None, (topics.success, topics.failure)):
                unsubscribes.append(
                    await self._bus.subscribe_ungrouped(topic, on_terminal(topic))
                )
            await self._bus.publish(
                topics.command, str(correlation_id).encode("utf-8"), _bytes(envelope)
            )
            return await asyncio.wait_for(future, timeout=timeout_s)
        finally:
            for unsubscribe in unsubscribes:
                await unsubscribe()
