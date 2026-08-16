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
- contract.yaml compatibility_publish_topics (OMN-15971): the exact declared
  set, the derived invariant that every cross-domain publish topic appears in
  it, and a real cross-boundary drive of the runtime's own discovery gate
  (omnibase_infra _parse_contract -> _contract_targets_active_runtime_packages)
  under the live onex-dev allowlist -- with the field-removed control proving
  the test would go red if the field were dropped.
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


# ---------------------------------------------------------------------------
# compatibility_publish_topics (OMN-15971)
#
# This node fans out into package domains it does not own (omniclaude,
# omniintelligence). The runtime's auto-wiring discovery gate skips any
# contract whose publish_topics reach an inactive package domain, which on the
# onex-dev lane (ONEX_ACTIVE_RUNTIME_PACKAGES=omnibase_infra,omnimarket) meant
# the whole contract was never wired. compatibility_publish_topics is the
# runtime's own per-contract escape hatch for exactly this shape. If it is
# dropped or drifts behind publish_topics, the node silently stops being wired
# with no test failure anywhere -- these tests are what make that loud.
# ---------------------------------------------------------------------------

# The onex-dev allowlist as read live from the deployed effects pod.
ONEX_DEV_ACTIVE_PACKAGES = "omnibase_infra,omnimarket"

EXPECTED_COMPATIBILITY_PUBLISH_TOPICS = [
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
]


def test_compatibility_publish_topics_matches_expected_set_exactly() -> None:
    """Pins the declared field so it cannot silently drop or drift.

    A bare `assert "compatibility_publish_topics" in contract` would pass on an
    empty list, which is exactly as broken as a missing key -- the gate would
    skip the contract either way. Compare the set.
    """
    contract = _load_contract()
    assert "compatibility_publish_topics" in contract, (
        "compatibility_publish_topics was removed from contract.yaml; without "
        "it the runtime discovery gate skips this whole contract on any lane "
        "where omniclaude/omniintelligence are not active packages."
    )
    declared = contract["compatibility_publish_topics"]
    assert isinstance(declared, list)
    assert set(declared) == set(EXPECTED_COMPATIBILITY_PUBLISH_TOPICS)


def test_compatibility_topics_are_a_subset_of_publish_topics() -> None:
    """No stale entries: declaring a compatibility topic this node never
    publishes would quietly widen the escape hatch beyond the real fan-out."""
    contract = _load_contract()
    event_bus = contract["event_bus"]
    assert isinstance(event_bus, dict)
    declared = contract["compatibility_publish_topics"]
    assert isinstance(declared, list)
    assert set(declared) <= set(event_bus["publish_topics"])


def test_every_inactive_publish_topic_is_declared_compatible() -> None:
    """The derived invariant, computed with the runtime's OWN topic-activity
    function rather than a hand-copied domain list.

    This is the test that catches the real regression shape: someone adds a
    new cross-domain publish topic to publish_topics and does not add it to
    compatibility_publish_topics, so discovery starts skipping the contract
    again.
    """
    from omnibase_infra.utils.util_runtime_packages import (
        get_active_runtime_packages,
        is_runtime_topic_active,
    )

    contract = _load_contract()
    event_bus = contract["event_bus"]
    assert isinstance(event_bus, dict)
    publish_topics = event_bus["publish_topics"]
    assert isinstance(publish_topics, list)
    declared_raw = contract["compatibility_publish_topics"]
    assert isinstance(declared_raw, list)
    declared = set(declared_raw)

    active = get_active_runtime_packages(ONEX_DEV_ACTIVE_PACKAGES)
    inactive = {
        topic for topic in publish_topics if not is_runtime_topic_active(topic, active)
    }

    assert inactive, (
        "no publish topic resolves as inactive under the onex-dev allowlist -- "
        "this test has stopped proving anything; re-derive it against the live "
        "ONEX_ACTIVE_RUNTIME_PACKAGES value before deleting it."
    )
    missing = inactive - declared
    assert not missing, (
        f"publish topics reach inactive package domains but are not declared "
        f"in compatibility_publish_topics: {sorted(missing)}. The runtime "
        f"discovery gate will skip this entire contract."
    )


def test_discovery_gate_wires_this_contract_under_onex_dev_allowlist() -> None:
    """Real cross-boundary seam drive, not a re-implementation.

    Parses contract.yaml with the runtime's own ``_parse_contract`` and runs
    the actual ``_contract_targets_active_runtime_packages`` gate under the
    live onex-dev allowlist. The second half is the control: strip
    compatibility_publish_topics off the parsed contract and the same gate
    must return False -- proving this test fails if the field is dropped,
    rather than passing vacuously.
    """
    from omnibase_infra.runtime.auto_wiring.discovery import (
        _contract_targets_active_runtime_packages,
        _parse_contract,
    )
    from omnibase_infra.utils.util_runtime_packages import get_active_runtime_packages

    parsed = _parse_contract(
        contract_path=CONTRACT_PATH,
        entry_point_name="node_event_emit_effect",
        package_name="omnimarket",
        package_version="0.0.0",
    )
    active = get_active_runtime_packages(ONEX_DEV_ACTIVE_PACKAGES)

    assert set(parsed.compatibility_publish_topics) == set(
        EXPECTED_COMPATIBILITY_PUBLISH_TOPICS
    ), "the runtime parser did not read the contract's compatibility topics"

    assert _contract_targets_active_runtime_packages(parsed, active) is True, (
        "the deployed discovery gate would skip node_event_emit_effect on the "
        "onex-dev lane"
    )

    without_compat = parsed.model_copy(update={"compatibility_publish_topics": ()})
    assert _contract_targets_active_runtime_packages(without_compat, active) is False, (
        "control failed: the gate accepts this contract even without "
        "compatibility_publish_topics, so the assertion above proves nothing"
    )


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


# ---------------------------------------------------------------------------
# OMN-16048: envelope enrichment is contract-declared, not implicit
# ---------------------------------------------------------------------------


def test_contract_declares_envelope_enrichment_block() -> None:
    contract = _load_contract()
    enrichment = contract.get("envelope_enrichment")
    assert isinstance(enrichment, dict), (
        "contract.yaml must declare envelope_enrichment: the injected fields "
        "and the partition-key computation are wire contract (OMN-16048 "
        "ruled REPLICATE), not handler implementation detail."
    )
    assert (
        enrichment["applied_by"]
        == "omnimarket.nodes.node_event_emit_effect.enrichment.inject_metadata"
    )


def test_contract_injected_fields_match_the_implementation() -> None:
    """The declared field list and enrichment.ENRICHMENT_FIELDS must agree.

    This is the anti-drift assertion: adding a field to inject_metadata
    without declaring it (or vice versa) fails here.
    """
    from omnimarket.nodes.node_event_emit_effect.enrichment import (
        ENRICHMENT_FIELDS,
        SCHEMA_VERSION,
        SESSION_ID_ENV_VAR,
        UNCONDITIONAL_ENRICHMENT_FIELDS,
    )

    contract = _load_contract()
    enrichment = contract["envelope_enrichment"]
    assert isinstance(enrichment, dict)
    declared = enrichment["injected_fields"]
    assert isinstance(declared, list)

    by_name = {entry["name"]: entry for entry in declared}
    assert set(by_name) == set(ENRICHMENT_FIELDS)
    assert tuple(entry["name"] for entry in declared) == ENRICHMENT_FIELDS

    assert by_name["schema_version"]["value"] == SCHEMA_VERSION
    assert by_name["session_id"]["env_var"] == SESSION_ID_ENV_VAR

    # Only the two seam-backed fields are marked generated.
    generated = {name for name, e in by_name.items() if e["generated"]}
    assert generated == {"correlation_id", "emitted_at"}
    assert set(UNCONDITIONAL_ENRICHMENT_FIELDS) <= set(ENRICHMENT_FIELDS)


def test_contract_declares_registry_driven_partition_key_and_transforms() -> None:
    from omnimarket.nodes.node_event_emit_effect.enrichment import TRANSFORM_REGISTRY

    contract = _load_contract()
    enrichment = contract["envelope_enrichment"]
    assert isinstance(enrichment, dict)

    partition_key = enrichment["partition_key"]
    assert "partition_key_field" in partition_key["source"]
    assert partition_key["override"] == "ModelEmitRequest.partition_key"

    transform = enrichment["per_topic_transform"]
    assert set(transform["supported"]) == set(TRANSFORM_REGISTRY)


def test_handler_exposes_the_declared_determinism_seam() -> None:
    """contract.yaml names the two seam parameters; they must actually exist."""
    contract = _load_contract()
    enrichment = contract["envelope_enrichment"]
    assert isinstance(enrichment, dict)
    seam = enrichment["determinism_seam"]

    params = inspect.signature(HandlerEventEmitEffect.__init__).parameters
    for declared in seam.values():
        param_name = declared.rsplit(".", 1)[-1]
        assert param_name in params, (
            f"contract declares determinism seam {declared!r} but "
            f"HandlerEventEmitEffect.__init__ has no {param_name!r} parameter"
        )
