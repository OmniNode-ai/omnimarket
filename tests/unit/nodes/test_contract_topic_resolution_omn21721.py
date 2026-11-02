"""Slice modules take their topics from the owning node contract (OMN-21721)."""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest
import yaml

_SRC = Path(__file__).resolve().parents[3] / "src" / "omnimarket"
_NODES = _SRC / "nodes"
_TOPIC_LITERAL = re.compile(r"[\"']onex\.(?:cmd|evt|dlq|intent|snapshot)\.")

# module -> {constant: (owning node, contract key path, suffix)}
_SLICE: dict[str, dict[str, tuple[str, tuple[str, ...], str]]] = {
    "adapters.codex.topics": {
        "TOPIC_CODEX_PATTERN_B_DISPATCH_COMMAND": (
            "node_pattern_b_broker",
            ("event_bus", "publish_topics"),
            ".pattern-b-dispatch.v1",
        ),
        "TOPIC_CODEX_PATTERN_B_DISPATCH_COMPLETED": (
            "node_pattern_b_broker",
            ("event_bus", "subscribe_topics"),
            ".pattern-b-dispatch-completed.v1",
        ),
        "TOPIC_CODEX_DELEGATE_SKILL_COMMAND": (
            "node_delegate_skill_orchestrator",
            ("event_bus", "subscribe_topics"),
            ".delegate-skill.v1",
        ),
        "TOPIC_CODEX_DELEGATE_SKILL_COMPLETED": (
            "node_delegate_skill_orchestrator",
            ("event_bus", "publish_topics"),
            ".delegate-skill-completed.v1",
        ),
        "TOPIC_CODEX_DELEGATE_SKILL_FAILED": (
            "node_delegate_skill_orchestrator",
            ("event_bus", "publish_topics"),
            ".delegate-skill-failed.v1",
        ),
    },
    "nodes.node_data_flow_sweep.handlers.handler_data_flow_sweep": {
        "NODE_INTROSPECTION_TOPIC": (
            "node_data_flow_sweep",
            ("probed_topics",),
            ".node-introspection.v1",
        ),
        "PATTERN_LEARNED_TOPIC": (
            "node_data_flow_sweep",
            ("probed_topics",),
            ".pattern-learned.v1",
        ),
        "ROUTING_DECISION_TOPIC": (
            "node_data_flow_sweep",
            ("probed_topics",),
            ".routing-decision.v1",
        ),
    },
    "nodes.node_filesystem_crawler_effect.models.model_filesystem_crawler_config": {
        name: (
            "node_filesystem_crawler_effect",
            ("event_bus", "publish_topics"),
            suffix,
        )
        for name, suffix in (
            ("TOPIC_DOCUMENT_CHANGED", "document-changed.v1"),
            ("TOPIC_DOCUMENT_DISCOVERED", "document-discovered.v1"),
            ("TOPIC_DOCUMENT_INDEXED", "document-indexed.v1"),
            ("TOPIC_DOCUMENT_REMOVED", "document-removed.v1"),
        )
    },
    "nodes.node_kreuzberg_parse_effect.models.model_kreuzberg_parse_config": {
        "TOPIC_DOCUMENT_INDEXED": (
            "node_kreuzberg_parse_effect",
            ("event_bus", "publish_topics"),
            "document-indexed.v1",
        ),
        "TOPIC_DOCUMENT_PARSE_FAILED": (
            "node_kreuzberg_parse_effect",
            ("event_bus", "publish_topics"),
            "document-parse-failed.v1",
        ),
    },
    "nodes.node_merge_sweep_compute.handlers.handler_merge_sweep": {
        "TOPIC_MERGE_SWEEP_START": (
            "node_merge_sweep_compute",
            ("event_bus", "subscribe_topics"),
            "merge-sweep-start.v1",
        ),
        "TOPIC_MERGE_SWEEP_COMPLETED": (
            "node_merge_sweep_compute",
            ("event_bus", "publish_topics"),
            "merge-sweep-completed.v1",
        ),
    },
    "nodes.node_merge_sweep_triage_orchestrator.handlers.handler_triage": {
        name: (
            "node_merge_sweep_triage_orchestrator",
            ("event_bus", "publish_topics"),
            suffix,
        )
        for name, suffix in (
            ("TOPIC_AUTO_MERGE_ARM", ".pr-auto-merge-arm.v1"),
            ("TOPIC_REBASE", ".pr-rebase.v1"),
            ("TOPIC_CI_RERUN", ".pr-ci-rerun.v1"),
            ("TOPIC_THREAD_REPLY", ".pr-thread-reply.v1"),
            ("TOPIC_CONFLICT_HUNK", ".pr-conflict-hunk.v1"),
            ("TOPIC_CI_FIX", ".pr-ci-fix.v1"),
        )
    },
    "nodes.node_projection_llm_cost.handlers.handler_projection_llm_cost": {
        "TOPIC_LLM_CALL_COMPLETED": (
            "node_projection_llm_cost",
            ("event_bus", "subscribe_topics"),
            "llm-call-completed.v1",
        ),
    },
    "nodes.node_session_bootstrap.handlers.handler_session_bootstrap": {
        "_TOPIC_SESSION_CRON_HEALTH_VIOLATION": (
            "node_session_bootstrap",
            ("event_bus", "publish_topics"),
            "session-cron-health-violation.v1",
        ),
    },
}


def _declared(node: str, keys: tuple[str, ...]) -> list[str]:
    raw: object = yaml.safe_load(
        (_NODES / node / "contract.yaml").read_text(encoding="utf-8")
    )
    for key in keys:
        assert isinstance(raw, dict)
        raw = raw[key]
    assert isinstance(raw, list)
    return [str(t) for t in raw]


@pytest.mark.unit
@pytest.mark.parametrize("module", sorted(_SLICE))
def test_constants_are_declared_by_the_owning_contract(module: str) -> None:
    loaded = importlib.import_module(f"omnimarket.{module}")
    for constant, (node, keys, suffix) in _SLICE[module].items():
        value = getattr(loaded, constant)
        assert value in _declared(node, keys)
        assert value.endswith(suffix)


@pytest.mark.unit
@pytest.mark.parametrize("module", sorted(_SLICE))
def test_module_holds_no_topic_literal(module: str) -> None:
    source = (_SRC / f"{module.replace('.', '/')}.py").read_text(encoding="utf-8")
    assert _TOPIC_LITERAL.search(source) is None


@pytest.mark.unit
def test_platform_readiness_required_topics_are_the_contract_probed_topics() -> None:
    from omnimarket.nodes.node_platform_readiness.handlers import (
        handler_platform_readiness as mod,
    )

    assert (
        _declared("node_platform_readiness", ("probed_topics",))
        == mod.SOW_PHASE2_REQUIRED_TOPICS
    )
    source = Path(mod.__file__).read_text(encoding="utf-8")
    assert _TOPIC_LITERAL.search(source) is None


@pytest.mark.unit
def test_probed_topics_are_not_event_bus_subscriptions() -> None:
    for node in ("node_data_flow_sweep", "node_platform_readiness"):
        probed = set(_declared(node, ("probed_topics",)))
        subscribed = set(_declared(node, ("event_bus", "subscribe_topics")))
        assert probed
        assert not probed & subscribed
