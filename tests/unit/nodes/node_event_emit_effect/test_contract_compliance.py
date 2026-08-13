# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-compliance checks for node_event_emit_effect (OMN-15965 R1).

Covers:
- contract.yaml descriptor.node_archetype == "effect" (a legal
  EnumNodeArchetype member, not "service").
- The handler module exposes exactly handle(request: ModelEmitRequest) ->
  ModelEmitResult (canonical definition-B shape).
- Static scan: no ModelEventEnvelope import, no ModelHandlerOutput return
  anywhere in the handler module (OMN-14355 canon-shape ratchet,
  envelope_in_core failure mode).
- Static scan: no Plugin* base class subclassed anywhere in the node
  package (rule 7a).
- Hard constraint: no node_emit_daemon Python-module import anywhere in the
  node package (registries/topics.yaml is read by path only).
"""

from __future__ import annotations

import ast
import inspect
import typing
from pathlib import Path

import pytest
import yaml
from omnibase_core.enums.enum_node_archetype import EnumNodeArchetype

from omnimarket.nodes.node_event_emit_effect.handlers.handler_event_emit_effect import (
    HandlerEventEmitEffect,
)
from omnimarket.nodes.node_event_emit_effect.models.model_emit_request import (
    ModelEmitRequest,
)
from omnimarket.nodes.node_event_emit_effect.models.model_emit_result import (
    ModelEmitResult,
)

pytestmark = pytest.mark.unit

NODE_DIR = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_event_emit_effect"
)
CONTRACT_PATH = NODE_DIR / "contract.yaml"
HANDLER_PATH = NODE_DIR / "handlers" / "handler_event_emit_effect.py"


def _load_contract() -> dict[str, object]:
    with CONTRACT_PATH.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    assert isinstance(data, dict)
    return data


# ---------------------------------------------------------------------------
# descriptor.node_archetype
# ---------------------------------------------------------------------------


def test_node_archetype_is_effect_not_service() -> None:
    contract = _load_contract()
    descriptor = contract["descriptor"]
    assert isinstance(descriptor, dict)
    archetype = descriptor["node_archetype"]

    assert archetype != "service"
    # Must round-trip through the real enum -- proves it's a legal member.
    assert EnumNodeArchetype(archetype) is EnumNodeArchetype.EFFECT


def test_cosmetic_node_type_does_not_leak_service_either() -> None:
    """node_type is cosmetic; descriptor.node_archetype is what matters.

    Regression guard for the "two different fields, only one matters"
    finding from the dogfood emitter brief that motivated this ticket.
    """
    contract = _load_contract()
    assert contract["node_type"] == "EFFECT_GENERIC"


def test_terminal_event_is_declared_in_publish_topics() -> None:
    contract = _load_contract()
    terminal = contract["terminal_event"]
    event_bus = contract["event_bus"]
    assert isinstance(event_bus, dict)
    publish_topics = event_bus["publish_topics"]
    assert terminal in publish_topics


# Regression guard for the exact declared publish_topics set (also satisfies
# the contract-state-coverage gate: each declared "state" -- here, an
# event_bus topic -- must have a literal test reference). Keep this in sync
# with contract.yaml -- see contract.yaml's own comment for why this set is
# scoped to topics with a live consumer today rather than the full registry.
EXPECTED_PUBLISH_TOPICS = [
    "onex.cmd.omnibase-infra.delegation-request.v1",
    "onex.cmd.omniclaude.delegate-task.v1",
    "onex.cmd.omniintelligence.claude-hook-event.v1",
    "onex.cmd.omniintelligence.compliance-evaluate.v1",
    "onex.cmd.omniintelligence.session-outcome.v1",
    "onex.cmd.omniintelligence.tool-content.v1",
    "onex.cmd.omniintelligence.utilization-scoring.v1",
    "onex.evt.omniclaude.budget-cap-hit.v1",
    "onex.evt.omniclaude.circuit-breaker-tripped.v1",
    "onex.evt.omniclaude.delegation-shadow-comparison.v1",
    "onex.evt.omniclaude.llm-routing-decision.v1",
    "onex.evt.omniclaude.notification-blocked.v1",
    "onex.evt.omniclaude.notification-completed.v1",
    "onex.evt.omniclaude.pattern-enforcement.v1",
    "onex.evt.omniclaude.phase-metrics.v1",
    "onex.evt.omniclaude.prompt-submitted.v1",
    "onex.evt.omniclaude.routing-decision.v1",
    "onex.evt.omniclaude.routing-feedback.v1",
    "onex.evt.omniclaude.session-ended.v1",
    "onex.evt.omniclaude.session-outcome.v1",
    "onex.evt.omniclaude.session-started.v1",
    "onex.evt.omniclaude.skill-completed.v1",
    "onex.evt.omniclaude.skill-started.v1",
    "onex.evt.omniclaude.tool-executed.v1",
    "onex.evt.omniintelligence.llm-call-completed.v1",
    "onex.evt.omnimarket.event-emit-completed.v1",
    "onex.evt.omnimarket.event-emit-failed.v1",
    "onex.evt.omnimarket.tool-output-captured.v1",
]


def test_publish_topics_matches_expected_set_exactly() -> None:
    contract = _load_contract()
    event_bus = contract["event_bus"]
    assert isinstance(event_bus, dict)
    assert set(event_bus["publish_topics"]) == set(EXPECTED_PUBLISH_TOPICS)


def test_subscribe_topics_is_own_command_topic_only() -> None:
    """Direct def-B CLI/plugin-runtime dispatch: subscribe_topics contains only
    this node's own runtime_dispatch.command_topic, not a live bus trigger."""
    contract = _load_contract()
    event_bus = contract["event_bus"]
    assert isinstance(event_bus, dict)
    runtime_dispatch = contract["runtime_dispatch"]
    assert isinstance(runtime_dispatch, dict)
    assert event_bus["subscribe_topics"] == [runtime_dispatch["command_topic"]]


# ---------------------------------------------------------------------------
# Handler shape: canonical definition-B
# ---------------------------------------------------------------------------


def test_handler_exposes_canonical_definb_handle_signature() -> None:
    sig = inspect.signature(HandlerEventEmitEffect.handle)
    params = [p for name, p in sig.parameters.items() if name != "self"]
    assert len(params) == 1
    (request_param,) = params

    # `from __future__ import annotations` makes raw annotations strings;
    # resolve them to the real objects before comparing.
    hints = typing.get_type_hints(HandlerEventEmitEffect.handle)
    assert hints[request_param.name] is ModelEmitRequest
    assert hints["return"] is ModelEmitResult


def test_handler_has_no_other_public_dispatch_methods() -> None:
    public_methods = [
        name
        for name, member in inspect.getmembers(
            HandlerEventEmitEffect, predicate=inspect.isfunction
        )
        if not name.startswith("_")
    ]
    assert public_methods == ["handle"]


# ---------------------------------------------------------------------------
# Static scans
# ---------------------------------------------------------------------------


def _all_package_source() -> dict[Path, str]:
    return {path: path.read_text(encoding="utf-8") for path in NODE_DIR.rglob("*.py")}


def test_handler_module_has_no_envelope_or_handler_output_shape() -> None:
    source = HANDLER_PATH.read_text(encoding="utf-8")
    assert "ModelEventEnvelope" not in source
    assert "ModelHandlerOutput" not in source


def test_no_plugin_base_class_subclassed_anywhere_in_package() -> None:
    for path, source in _all_package_source().items():
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("class ") and "(" in stripped:
                bases = stripped.split("(", 1)[1].rsplit(")", 1)[0]
                assert "Plugin" not in bases, (
                    f"{path}: subclasses a Plugin* base class ({stripped!r}); "
                    "no Plugin* base class is legal in the canonical architecture "
                    "(rule 7a)."
                )


def test_no_node_emit_daemon_python_import_anywhere_in_package() -> None:
    """Hard constraint: R5 (OMN-15974) deletes node_emit_daemon's Python
    surface; R1 must not create a dependency R5 then has to break. The one
    exception -- reading registries/topics.yaml BY PATH -- is not a Python
    import.

    Parses real AST import nodes (not text matching) so mentions of
    "node_emit_daemon" in docstrings/comments don't false-positive.
    """
    for path, source in _all_package_source().items():
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "node_emit_daemon" not in alias.name, (
                        f"{path}: illegal node_emit_daemon import: {alias.name!r}"
                    )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert "node_emit_daemon" not in module, (
                    f"{path}: illegal node_emit_daemon import: {module!r}"
                )
                # A bare package import (e.g. "from omnimarket.nodes import
                # node_emit_daemon") has module="omnimarket.nodes" -- the
                # check above misses it. Inspect the imported names too.
                for alias in node.names:
                    assert alias.name != "node_emit_daemon", (
                        f"{path}: illegal node_emit_daemon import: {alias.name!r}"
                    )
