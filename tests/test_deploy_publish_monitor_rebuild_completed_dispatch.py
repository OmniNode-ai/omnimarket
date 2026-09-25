# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""The deploy publish-monitor can receive the deploy agent's completion event (OMN-17888).

WHAT WAS BROKEN, MEASURED ON THE .201 DEV LANE 2026-09-16
---------------------------------------------------------
``node_redeploy_deploy_effect`` declares ``onex.evt.deploy.rebuild-completed.v1`` in
``event_bus.subscribe_topics`` AND gives it a ``handler_routing`` entry that routes it to
``HandlerDeployPublishMonitor`` with ``input_model: ModelDeployPublishCommand`` — the
model of the COMMAND the node consumes on its other topic. ``handler_wiring`` subscribes
every declared ``subscribe_topics`` entry unconditionally, so the runtime opens a durable
dispatching consumer for the completion event and every message reaches ``handle()``,
which coerced its payload to the command model and raised.

Read off ``onex.dlq.omnibase-infra.events.v1`` (compose project ``omnibase-infra``,
partition 0, last 400 records of high-watermark 21,905,803) on 2026-09-16T12:35Z —
**72 records**, spanning 2026-09-11T13:12:41Z to 2026-09-16T11:28:20Z, every one of them::

    HandlerDispatchFailureError: dispatch to topic=onex.evt.deploy.rebuild-completed.v1
    returned status=handler_error with no terminal output
    (dispatcher_id=): Dispatcher
    'dispatcher.auto.node_redeploy_deploy_effect.HandlerDeployPublishMonitor
    .redeploy_deploy_publish_monitor_109d7ea0' failed:
    ValidationError: 12 validation errors for ModelDeployPublishCommand
    requested_git_ref
      Extra inputs are not permitted [type=extra_forbidden, input_value='6c540f4e...']

The consumer group for that subscription is real and was read back in the same pass:
``local.omnimarket.node_redeploy_deploy_effect.consume.1.0.0.__i.runtime-effects
.__t.onex.evt.deploy.rebuild-completed.v1``, alongside the sibling command-topic group,
out of 792 groups on the lane (the positive control on that read).

WHY THE EXISTING GATES REPORTED GREEN
-------------------------------------
``subscriber_dispatcher_resolution`` resolves ROUTES: it asks whether a declared
subscribe topic reaches a registered dispatcher for its own ``(category, message type)``.
This one does — the entry declares ``message_category: event`` and resolves cleanly. The
gate has no way to ask the next question, which is whether the handler on the other end
can accept the model the entry declares. That question is what this module adds.

THE FIX, AND WHY IT IS THIS ONE
-------------------------------
The sibling in the same FSM already sets the pattern: ``node_redeploy_orchestrator`` uses
``routing_strategy: topic_match``, routes one command and four events to ONE handler
class, and ``HandlerRedeployOrchestrator.handle`` branches on ``_event_name(event_type)``.
This contract now does the same.

``ServiceHandlerResolver.resolve`` constructs a FRESH handler instance per
``handler_routing`` entry with no cache, so the durable event arm never shares an object
with the command arm. Since OMN-18143 that is no longer a limitation to work around: the
command arm publishes and returns without a correlation-scoped subscription, and this
durable arm is where a completion is settled, from the record the command arm wrote under
``ONEX_STATE_DIR``.
"""

from __future__ import annotations

import ast
import asyncio
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.validators.subscriber_dispatcher_resolution import scan
from pydantic import ValidationError

from omnimarket.events.runtime_deployment import ModelDeployRebuildCompleted
from omnimarket.nodes.node_redeploy_deploy_effect.handlers.handler_deploy_publish_monitor import (
    TOPIC_REBUILD_COMPLETED,
    TOPIC_REBUILD_REJECTED,
    TOPIC_REBUILD_REQUESTED,
    HandlerDeployPublishMonitor,
)
from omnimarket.validators.routing_input_model_fit import (
    PEER_FENCED_CONTRACTS,
    ModelRoutingInputModelFinding,
    findings_for_contract_tree,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCAN_ROOT = _REPO_ROOT / "src" / "omnimarket"
_NODE = "node_redeploy_deploy_effect"
_CONTRACT = _SCAN_ROOT / "nodes" / _NODE / "contract.yaml"
_HANDLER_SOURCE = (
    _SCAN_ROOT / "nodes" / _NODE / "handlers" / "handler_deploy_publish_monitor.py"
)

_PUBLISH_COMMAND_TOPIC = "onex.cmd.omnimarket.redeploy-deploy-publish.v1"

# A scan that collapses returns zero findings and reads exactly like a clean tree, so the
# contract count is the positive control on every repo-wide assertion below.
_MIN_EXPECTED_CONTRACTS = 300

_EVENT_VERSION_SUFFIX_RE = re.compile(r"\.v\d+$")


def _event_name_of(topic_or_event_type: str) -> str:
    """The bare event name, by the same reduction the handler itself applies."""
    return _EVENT_VERSION_SUFFIX_RE.sub("", topic_or_event_type.strip()).rpartition(
        "."
    )[2]


def _category_for(topic: str) -> str:
    """The category a message on ``topic`` actually arrives under."""
    return {"evt": "event", "cmd": "command", "intent": "intent"}[topic.split(".")[1]]


def _wire_rebuild_completed_payload() -> dict[str, Any]:
    """The payload the deploy agent really publishes, field for field.

    ``deploy_agent.publisher.publish_result`` sends
    ``ModelRebuildCompleted.model_dump(mode="json")`` as the whole record body — a bare
    payload with no envelope and no ``event_type`` key, which is why the consume boundary
    stamps ``event_type`` from the TOPIC rather than from the body.

    All seventeen keys below are that model's wire surface: sixteen declared fields plus
    ``status``, which is a ``@computed_field`` and so IS dumped. ``ModelDeployPublishCommand``
    declares five of the seventeen names and forbids extras, which is where the live
    records' ``12 validation errors`` comes from — the count is reproduced here rather
    than quoted.
    """
    return {
        "correlation_id": "e527c19d-76c6-4e93-b6f4-54afe9946af0",
        "requested_git_ref": "dev",
        "git_sha": "6c540f4ebe1f8503d4e051dc107e9fa26abf9a30",
        "started_at": "2026-09-16T11:18:11.000000+00:00",
        "completed_at": "2026-09-16T11:28:19.000000+00:00",
        "duration_seconds": 608.0,
        "scope": "full",
        "runtime_lane": "dev",
        "image_ref": "omninode-runtime:dev",
        "image_digest": "sha256:" + "a" * 64,
        "services_restarted": ["omninode-runtime", "omninode-runtime-effects"],
        "sibling_refs": {"omnimarket": "f15ea42e"},
        "phase_results": {
            "git": "success",
            "core": "success",
            "runtime": "success",
            "verification": "success",
        },
        "errors": [],
        "health_checks": [],
        "container_residue": [],
        "status": "success",
    }


def _wire_rebuild_completed_envelope() -> ModelEventEnvelope[object]:
    """The envelope the runtime really builds for a deploy-agent completion event."""
    return ModelEventEnvelope[object](
        payload=_wire_rebuild_completed_payload(),
        correlation_id=uuid4(),
        event_type=TOPIC_REBUILD_COMPLETED,
    )


class _RecordingBus:
    """A bus that fails the test if the durable event arm performs any I/O.

    The durable arm observes a completion that belongs to some other invocation; it must
    not publish a rebuild command, and it must not open a subscription. Both are recorded
    rather than stubbed silently so an accidental re-entry into the command arm shows
    up as a named assertion failure.
    """

    def __init__(self) -> None:
        self.published: list[str] = []
        self.subscribed: list[str] = []

    async def publish(self, topic: str, **_kwargs: object) -> None:
        self.published.append(topic)

    async def subscribe(self, topic: str, **_kwargs: object) -> object:
        self.subscribed.append(topic)

        async def _unsubscribe() -> None:
            return None

        return _unsubscribe


# ---------------------------------------------------------------------------
# 1. The live defect, reproduced through the handler the runtime really dispatches to.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_wire_shaped_rebuild_completed_event_is_handled_not_rejected() -> None:
    """RED before the fix: ``handle`` coerced the event to the COMMAND model and raised.

    This is the 72-record DLQ class, reproduced with the deploy agent's own payload shape
    and the ``event_type`` the consume boundary stamps for a body that carries none.
    """
    bus = _RecordingBus()
    handler = HandlerDeployPublishMonitor(event_bus=bus)

    output = asyncio.run(handler.handle(_wire_rebuild_completed_envelope()))

    assert output is not None, "the completion event produced no handler output"
    assert not bus.published, (
        f"the durable event arm published {bus.published}; observing a completion must "
        "never re-issue a rebuild command"
    )
    assert not bus.subscribed, (
        f"the durable event arm opened subscriptions {bus.subscribed}; it must not "
        "re-enter the command arm"
    )


@pytest.mark.unit
def test_the_completion_event_is_validated_under_its_own_model() -> None:
    """The event arm is typed, not a swallow: a malformed completion still fails loudly.

    A branch that accepted anything would stop the dead-lettering by discarding the
    contract, which is the failure this ticket exists to remove rather than relocate.
    """
    bus = _RecordingBus()
    handler = HandlerDeployPublishMonitor(event_bus=bus)
    malformed = dict(_wire_rebuild_completed_payload())
    del malformed["correlation_id"]

    with pytest.raises(ValidationError):
        asyncio.run(
            handler.handle(
                ModelEventEnvelope[object](
                    payload=malformed,
                    correlation_id=uuid4(),
                    event_type=TOPIC_REBUILD_COMPLETED,
                )
            )
        )


@pytest.mark.unit
def test_the_declared_event_model_really_accepts_the_wire_payload() -> None:
    """Positive control on the two tests above: the payload is not trivially valid.

    ``ModelDeployRebuildCompleted`` ignores extras, so this asserts the fields the effect
    actually reads survive the round trip rather than merely that nothing raised.
    """
    completed = ModelDeployRebuildCompleted.model_validate(
        _wire_rebuild_completed_payload()
    )
    assert completed.correlation_id == "e527c19d-76c6-4e93-b6f4-54afe9946af0"
    assert completed.git_sha == "6c540f4ebe1f8503d4e051dc107e9fa26abf9a30"
    assert completed.services_restarted == [
        "omninode-runtime",
        "omninode-runtime-effects",
    ]


@pytest.mark.unit
def test_the_command_arm_is_unchanged_by_the_branch() -> None:
    """Falsification control: the branch must not divert the COMMAND this node exists for.

    A branch keyed on the wrong string would send the publish command down the observation
    arm, and every assertion above would still pass while the node deployed nothing.
    """
    bus = _RecordingBus()
    handler = HandlerDeployPublishMonitor(event_bus=bus)
    command_envelope = ModelEventEnvelope[object](
        payload={
            "correlation_id": str(uuid4()),
            "runtime_lane": "dev",
            "git_ref": "dev",
        },
        correlation_id=uuid4(),
        event_type=_PUBLISH_COMMAND_TOPIC,
    )

    output = asyncio.run(handler.handle(command_envelope))

    assert output is not None
    assert bus.published == [TOPIC_REBUILD_REQUESTED], (
        f"the command arm did not publish the rebuild command: {bus.published}"
    )
    assert bus.subscribed == [], (
        "the command arm opened a subscription to wait for the agent (OMN-18143 AC3); "
        f"subscribed={bus.subscribed}"
    )


# ---------------------------------------------------------------------------
# 2. The contract states the truth, and still resolves.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_completion_entry_declares_the_completion_model() -> None:
    """The declaration under test, asserted by name so the record needs no diff."""
    contract = yaml.safe_load(_CONTRACT.read_text())
    routing = contract["handler_routing"]
    assert routing["routing_strategy"] == "topic_match", (
        "two entries sharing one operation cannot be resolved by operation_match; the "
        "sibling node_redeploy_orchestrator routes the same shape by topic"
    )

    by_topic = {
        entry["topic"]: entry for entry in routing["handlers"] if entry.get("topic")
    }
    # OMN-18816 added the rejection arm on the same pattern. The set is asserted
    # exhaustively on purpose: a topic that gains a routing entry without a handler
    # branch is the OMN-17888 defect, so a new arm must be declared here deliberately
    # rather than slipping in under a subset check.
    assert set(by_topic) == {
        _PUBLISH_COMMAND_TOPIC,
        TOPIC_REBUILD_COMPLETED,
        TOPIC_REBUILD_REJECTED,
    }

    completion = by_topic[TOPIC_REBUILD_COMPLETED]
    assert completion["message_category"] == "event"
    assert completion["input_model"]["name"] == "ModelDeployRebuildCompleted"
    assert (
        completion["input_model"]["module"] == "omnimarket.events.runtime_deployment"
    ), (
        "the completion model is the shared one omnibase_infra's deploy agent publishes to"
    )

    command = by_topic[_PUBLISH_COMMAND_TOPIC]
    assert command["message_category"] == "command"
    assert command["input_model"]["name"] == "ModelDeployPublishCommand"


@pytest.mark.unit
def test_both_subscribe_topics_still_resolve_to_a_registered_dispatcher() -> None:
    """The routing-resolution gate must stay green across the strategy change.

    Resolved through the real production helpers ``_prepare_handler_wiring`` itself calls,
    so this cannot drift from the runtime.
    """
    findings, contract_count = scan(_SCAN_ROOT)
    assert contract_count >= _MIN_EXPECTED_CONTRACTS, (
        f"discovery collapsed to {contract_count} contracts; this assertion would then "
        "be vacuously green"
    )
    unresolved = {
        (getattr(f, "contract", None), getattr(f, "topic", None)): getattr(
            f, "reason", None
        )
        for f in findings
    }
    offenders = {
        topic: unresolved[(_NODE, topic)]
        for topic in (_PUBLISH_COMMAND_TOPIC, TOPIC_REBUILD_COMPLETED)
        if (_NODE, topic) in unresolved
    }
    assert not offenders, (
        f"{_NODE} subscribes to these topics but no dispatcher can receive them: "
        f"{offenders}"
    )


# ---------------------------------------------------------------------------
# 3. The gate: a declared input model that the handler cannot accept.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_input_model_fit_gate_is_clean_repo_wide() -> None:
    """No handler in the tree declares ONE input model under TWO message categories.

    A command and an event are different wire shapes. One model declared for both is a
    statement that cannot be true of any handler, and it is the exact shape that sent 72
    completion events to the DLQ while every routing gate reported the topic resolved.
    """
    findings, contract_count = findings_for_contract_tree(_SCAN_ROOT)
    assert contract_count >= _MIN_EXPECTED_CONTRACTS, (
        f"discovery collapsed to {contract_count} contracts; this assertion would then "
        "be vacuously green"
    )
    # OMN-18568: the gate now returns peer-fenced findings too, marked rather than
    # dropped, so the tree can be judged without an exemption that hides its own row.
    # Unpinned is what "clean" means; the pins are asserted separately, including the
    # rule that a pin whose contract has been fixed must be deleted.
    unpinned = [f for f in findings if f.peer_fenced_key is None]
    assert not unpinned, "\n".join(f.render() for f in unpinned)

    pinned = {f.peer_fenced_key for f in findings if f.peer_fenced_key}
    assert pinned == set(PEER_FENCED_CONTRACTS), (
        "PEER_FENCED_CONTRACTS and the live findings disagree. A pin whose contract is "
        "now clean is an exemption with nothing left to exempt and must be removed. "
        f"pinned={sorted(PEER_FENCED_CONTRACTS)} live={sorted(pinned)}"
    )


@pytest.mark.unit
def test_input_model_fit_gate_fires_on_the_shape_it_exists_for(tmp_path: Path) -> None:
    """Mutation control: the gate above is not green because it cannot fail.

    The mutant is this contract's own pre-fix shape — the completion entry re-declaring
    the COMMAND model — written to a throwaway tree so the check runs against a real
    contract file rather than a hand-built dict.
    """
    contract = yaml.safe_load(_CONTRACT.read_text())
    for entry in contract["handler_routing"]["handlers"]:
        if entry.get("topic") == TOPIC_REBUILD_COMPLETED:
            entry["input_model"]["name"] = "ModelDeployPublishCommand"
            entry["input_model"]["module"] = (
                "omnimarket.nodes.node_redeploy_deploy_effect.models."
                "model_deploy_publish_command"
            )
    mutant_node = tmp_path / "nodes" / _NODE
    mutant_node.mkdir(parents=True)
    (mutant_node / "contract.yaml").write_text(
        yaml.safe_dump(contract, sort_keys=False)
    )

    findings, contract_count = findings_for_contract_tree(tmp_path)

    assert contract_count == 1, f"the mutant tree was not read: {contract_count}"
    assert len(findings) == 1, f"expected exactly one finding, got {findings}"
    only = findings[0]
    assert isinstance(only, ModelRoutingInputModelFinding)
    assert only.contract == _NODE
    assert only.handler == "HandlerDeployPublishMonitor"
    assert only.effective_model == "ModelDeployPublishCommand"
    assert only.categories == ("command", "event")


# ---------------------------------------------------------------------------
# 4. The handler-scoped half: a declared event model is only honoured if a branch reads it.
# ---------------------------------------------------------------------------


def _handler_branch_event_names() -> frozenset[str]:
    """Every ``event_name == "..."`` literal this handler explicitly branches on.

    Parsed with ``ast`` over the handler module — the same technique the sibling guard in
    ``test_redeploy_orchestrator_dispatch_resolution`` uses — so a branch that is deleted
    or renamed moves this set. A regex would also match the literal inside a docstring.

    Deliberately scoped to this handler rather than swept repo-wide. Measured on the tree
    at ``origin/dev`` 2026-09-16: 16 contracts route more than one message category to a
    single handler class, and only ``node_redeploy_orchestrator`` and this node express
    the choice as an ``event_name ==`` comparison — the other 14 use topic matching,
    payload-type dispatch or separate typed def-B signatures. A repo-wide sweep for THIS
    idiom would report 14 handlers that are not defective, which is a broken probe rather
    than a finding. The repo-wide half of this gate is the category check above, which is
    idiom-independent.
    """
    tree = ast.parse(_HANDLER_SOURCE.read_text())

    # Module-level ``NAME = "literal"`` bindings, so a branch written against a named
    # constant resolves. The sibling guard reads bare string comparators only; this
    # handler compares against ``EVENT_REBUILD_COMPLETED``, which the module checks at
    # import against the name its contract topic reduces to. Resolving the binding keeps
    # ONE source for the literal instead of a second copy inlined to satisfy a parser.
    module_constants: dict[str, str] = {
        target.id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
        for target in node.targets
        if isinstance(target, ast.Name)
    }

    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        left = node.left
        if not (isinstance(left, ast.Name) and left.id == "event_name"):
            continue
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            if not isinstance(op, ast.Eq):
                continue
            if isinstance(comparator, ast.Constant) and isinstance(
                comparator.value, str
            ):
                names.add(comparator.value)
            elif isinstance(comparator, ast.Name) and comparator.id in module_constants:
                names.add(module_constants[comparator.id])
    return frozenset(names)


def _event_subscriptions_without_a_branch(
    subscribe_topics: tuple[str, ...], branch_names: frozenset[str]
) -> tuple[str, ...]:
    """Event-category subscribe topics that would fall through to the command arm."""
    return tuple(
        topic
        for topic in subscribe_topics
        if _category_for(topic) == "event" and _event_name_of(topic) not in branch_names
    )


@pytest.mark.unit
def test_handler_branch_names_parse_to_the_live_branch_set() -> None:
    """Positive control: a collapsed parse would make the guard below vacuous.

    Pinned both ways — an empty parse fails the guard loudly, but a SUPERSET would silence
    it, so the exact set is asserted.
    """
    assert _handler_branch_event_names() == {"rebuild-completed", "rebuild-rejected"}, (
        f"handler branch set drifted: {sorted(_handler_branch_event_names())}"
    )


@pytest.mark.unit
def test_every_event_subscription_has_an_explicit_handler_branch() -> None:
    """The invariant: no subscribed event may fall through to the command arm."""
    contract = yaml.safe_load(_CONTRACT.read_text())
    subscribe_topics = tuple(contract["event_bus"]["subscribe_topics"])
    assert TOPIC_REBUILD_COMPLETED in subscribe_topics, (
        f"contract read is wrong: {subscribe_topics}"
    )

    offenders = _event_subscriptions_without_a_branch(
        subscribe_topics, _handler_branch_event_names()
    )
    assert not offenders, (
        f"{_NODE} subscribes to these event topics with no explicit handler branch, so "
        f"every message on them is coerced to the command model: {list(offenders)}. Add "
        "a branch to HandlerDeployPublishMonitor.handle in the same change, or do not "
        "subscribe."
    )


@pytest.mark.unit
def test_the_branch_guard_fires_on_an_unbranched_event_subscription() -> None:
    """Falsification control: the guard above is not green because it cannot fail."""
    unbranched = "onex.evt.deploy.rebuild-started.v1"
    offenders = _event_subscriptions_without_a_branch(
        (_PUBLISH_COMMAND_TOPIC, TOPIC_REBUILD_COMPLETED, unbranched),
        _handler_branch_event_names(),
    )
    assert offenders == (unbranched,), (
        f"guard did not report exactly the unbranched topic: {offenders}"
    )


@pytest.mark.unit
def test_the_correlation_scoped_subscription_is_gone() -> None:
    """The durable arm is the monitoring path now (OMN-18143 AC3).

    The correlation-scoped subscription made the command arm wait for the agent, past the
    runtime's 600 s dispatch deadline on every real rebuild, and left one
    ``redeploy-deploy-effect-<corr8>`` consumer group behind per deploy: 120 of them were
    live on the dev lane at 2026-09-16T12:35Z (OMN-17888). The durable arm settles the
    completion from the publish record instead, so no per-correlation group is minted.
    """
    source = _HANDLER_SOURCE.read_text()
    assert "redeploy-deploy-effect-" not in source, (
        "the handler still mints a correlation-scoped consumer group"
    )


@pytest.mark.unit
def test_the_observation_arm_records_when_it_ran() -> None:
    """The durable arm must leave a metric, or its work is indistinguishable from a drop."""
    bus = _RecordingBus()
    handler = HandlerDeployPublishMonitor(event_bus=bus)
    before = datetime.now(UTC)

    output = asyncio.run(handler.handle(_wire_rebuild_completed_envelope()))

    assert datetime.now(UTC) >= before
    metrics = dict(output.metrics or {})
    assert metrics.get("rebuild_completed_observed") == 1.0, (
        f"the observation arm recorded no metric: {metrics}"
    )


# ---------------------------------------------------------------------------
# 5. The local ingress alias is a caller-facing NAME, so it must stay unique.
#
# OMN-17888 regression, found on the .201 dev lane by the dev-lane canary. Giving the
# completion entry its own honest input model (section 2) made the two entries' routes
# INEQUIVALENT while they still shared one ``operation``, and the operation is what the
# local-ingress alias registry keys on. ``omninode-runtime`` became boot-fatal:
#
#     ValueError: Duplicate local ingress route alias
#     'omnimarket.node_redeploy_deploy_effect.redeploy.deploy.publish_monitor'
#
# raised at omnibase_infra ``runtime_local_ingress.py`` (OMN-10081). Restart count 101,
# ``:8085/ready`` refused, and the compose-dev lab-pass receipt FAILed on ``ready_main``
# and ``health_dimensions``, which fails delivery to staging closed.
#
# WHY THE FIX IS DISTINCT OPERATIONS RATHER THAN A WIDER ALIAS KEY. The alias is the name
# a CALLER types: ``ModelLocalRuntimeIngressRequest`` carries ``command_name``/
# ``node_alias``, the alias resolves to exactly one route, and
# ``validate_runtime_local_ingress_payload`` then loads THAT route's input model to
# validate the caller's payload. A caller supplies a name, never a topic — so keying the
# alias on operation + topic would leave the ingress unable to say which model validates
# an incoming payload, and would weaken a platform-wide invariant every node depends on to
# accommodate one contract. Two entries that accept different models are two different
# operations.
#
# The sibling is the control that this is not a blanket ban: ``node_redeploy_orchestrator``
# routes five entries under ONE operation and does not trip this, because none of them
# declares a per-entry ``input_model``, so all five routes are equivalent and the registry
# deduplicates them on purpose.
# ---------------------------------------------------------------------------

_OBSERVE_OPERATION = "redeploy.deploy.rebuild_completed_observe"
_PUBLISH_MONITOR_OPERATION = "redeploy.deploy.publish_monitor"


def _operations_with_conflicting_input_models(
    contract: dict[str, Any],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Operations in one contract declared with more than one input model.

    The contract-level statement of the runtime invariant: one operation is one
    caller-facing interface, so it may name exactly one input model.
    """
    by_operation: dict[str, set[str]] = {}
    for entry in (contract.get("handler_routing") or {}).get("handlers") or []:
        if not isinstance(entry, dict):
            continue
        operation = entry.get("operation")
        # `input_model` is written either as a mapping with a `name`, or as a bare
        # string naming the model. Both shapes are live in this tree.
        declared = entry.get("input_model")
        model = declared.get("name") if isinstance(declared, dict) else declared
        if isinstance(operation, str) and operation.strip() and isinstance(model, str):
            by_operation.setdefault(operation.strip(), set()).add(model)
    return tuple(
        (operation, tuple(sorted(models)))
        for operation, models in sorted(by_operation.items())
        if len(models) > 1
    )


@pytest.mark.unit
def test_the_real_local_ingress_route_table_builds() -> None:
    """RED before the fix: this is the exact call that made the runtime boot-fatal.

    Not a re-derivation — ``discover_runtime_local_ingress_routes`` is the function the
    runtime itself calls at boot, resolving contracts from the installed package root.
    """
    from omnibase_infra.runtime.runtime_local_ingress import (
        discover_runtime_local_ingress_routes,
    )

    routes = discover_runtime_local_ingress_routes(["omnimarket"])

    # Positive control: a collapsed discovery would make the assertions below vacuous.
    assert len(routes) >= _MIN_EXPECTED_CONTRACTS, (
        f"local ingress discovery collapsed to {len(routes)} aliases"
    )


@pytest.mark.unit
def test_both_entries_resolve_to_their_own_input_model_through_the_real_registry() -> (
    None
):
    """The property the alias exists for: one name, one interface.

    The ingress validates a caller's payload with the model of the route its alias
    resolved to, so each of this contract's two operations must resolve to the model its
    own producer really sends.
    """
    from omnibase_infra.runtime.runtime_local_ingress import (
        discover_runtime_local_ingress_routes,
    )

    routes = discover_runtime_local_ingress_routes(["omnimarket"])
    prefix = f"omnimarket.{_NODE}."

    publish_monitor = routes.get(prefix + _PUBLISH_MONITOR_OPERATION)
    observe = routes.get(prefix + _OBSERVE_OPERATION)

    assert publish_monitor is not None, (
        f"{prefix + _PUBLISH_MONITOR_OPERATION} is not a local ingress alias"
    )
    assert observe is not None, (
        f"{prefix + _OBSERVE_OPERATION} is not a local ingress alias"
    )
    assert publish_monitor.input_model_name == "ModelDeployPublishCommand"
    assert observe.input_model_name == "ModelDeployRebuildCompleted"


@pytest.mark.unit
def test_no_operation_in_the_tree_declares_two_input_models() -> None:
    """The repo-wide invariant, stated where a reviewer reads it.

    Clean at every contract in the tree, with no allowlist: the one offender was this
    contract, and it is the one the regression came from.
    """
    offenders: list[str] = []
    contracts_read = 0
    for contract_path in sorted((_SCAN_ROOT / "nodes").rglob("contract.yaml")):
        document = yaml.safe_load(contract_path.read_text())
        if not isinstance(document, dict):
            continue
        contracts_read += 1
        for operation, models in _operations_with_conflicting_input_models(document):
            offenders.append(f"{document.get('name')}: {operation} -> {list(models)}")

    assert contracts_read >= _MIN_EXPECTED_CONTRACTS, (
        f"contract read collapsed to {contracts_read}; this would be vacuously green"
    )
    assert not offenders, (
        "an operation is one caller-facing local-ingress alias and may declare only one "
        f"input model; these declare more than one: {offenders}"
    )


@pytest.mark.unit
def test_the_operation_guard_fires_on_the_collapsed_pre_image() -> None:
    """Mutation control: the guard above is not green because it cannot fail.

    The mutant is this contract's own pre-image — both entries back under one operation —
    so the control reproduces the exact shape that made the runtime boot-fatal.
    """
    contract = yaml.safe_load(_CONTRACT.read_text())
    for entry in contract["handler_routing"]["handlers"]:
        entry["operation"] = _PUBLISH_MONITOR_OPERATION

    conflicts = _operations_with_conflicting_input_models(contract)

    assert conflicts == (
        (
            _PUBLISH_MONITOR_OPERATION,
            (
                "ModelDeployPublishCommand",
                "ModelDeployRebuildCompleted",
                # OMN-18816's rejection arm collapses into the same mutant. It belongs
                # in the expected tuple rather than being filtered out: the control
                # exists to prove the guard reports EVERY model an over-collapsed
                # operation would declare, and a control that ignored the newest arm
                # would go quiet on exactly the next one added.
                "ModelDeployRebuildRejected",
            ),
        ),
    ), f"guard did not report the collapsed pre-image: {conflicts}"


@pytest.mark.unit
def test_the_sibling_that_shares_one_operation_is_not_swept_up() -> None:
    """Falsification control on the invariant's SCOPE.

    ``node_redeploy_orchestrator`` routes five entries under one operation. That is legal
    precisely because none declares a per-entry ``input_model``, so every route it builds
    is equivalent and the registry deduplicates them. A guard that reported it would be
    banning the sibling pattern rather than the defect.
    """
    sibling = yaml.safe_load(
        (
            _SCAN_ROOT / "nodes" / "node_redeploy_orchestrator" / "contract.yaml"
        ).read_text()
    )
    entries = sibling["handler_routing"]["handlers"]

    # Positive control on the read: the sibling really does share one operation.
    assert len({entry["operation"] for entry in entries}) == 1
    assert len(entries) > 1

    assert _operations_with_conflicting_input_models(sibling) == ()
