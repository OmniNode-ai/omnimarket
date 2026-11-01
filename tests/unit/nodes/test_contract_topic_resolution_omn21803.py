"""Handler topics come from the owning node contract (OMN-21803)."""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.contract_topics import contract_topic

_NODES = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "nodes"
_TOPIC_LITERAL = re.compile(r"[\"']onex\.(?:cmd|evt|dlq|intent|snapshot)\.")

# module -> {constant: (event_bus section, suffix)}
_SLICE: dict[str, dict[str, tuple[str, str]]] = {
    "node_code_embedding_effect.handlers.handler_code_embedding_effect": {
        "TOPIC_CODE_ENTITIES_EXTRACTED": (
            "subscribe_topics",
            "code-entities-extracted.v1",
        ),
        "TOPIC_CODE_EMBEDDED": ("publish_topics", "code-embedded.v1"),
    },
    "node_code_enrichment_effect.handlers.handler_code_enrichment_effect": {
        "TOPIC_CODE_ENTITIES_EXTRACTED": (
            "subscribe_topics",
            "code-entities-extracted.v1",
        ),
        "TOPIC_CODE_ENRICHED": ("publish_topics", "code-enriched.v1"),
    },
    "node_intent_event_consumer_effect.models.model_consumer_config": {
        "TOPIC_INTENT_CLASSIFIED": ("subscribe_topics", "intent-classified.v1"),
        "TOPIC_INTENT_CLASSIFIED_DLQ": ("publish_topics", "intent-classified-dlq.v1"),
        "TOPIC_INTENT_STORED": ("publish_topics", "intent-stored.v1"),
    },
    "node_merge_sweep_state_reducer.handlers.handler_sweep_state": {
        "TOPIC_STATE_REDUCED": ("publish_topics", "merge-sweep-state-reduced.v1"),
        "TOPIC_SWEEP_COMPLETED": ("publish_topics", "merge-sweep-completed.v1"),
    },
    "node_model_router.handlers.handler_model_router": {
        "TOPIC_MODEL_ROUTING_DEGRADED": ("publish_topics", "model-routing-degraded.v1"),
        "TOPIC_MODEL_LLM_ROUTE_RESOLVED": (
            "publish_topics",
            "model-llm-route-resolved.v1",
        ),
        "TOPIC_MODEL_LLM_ROUTE_REJECTED": (
            "publish_topics",
            "model-llm-route-rejected.v1",
        ),
    },
    "node_overnight.handlers.overseer_tick": {
        "TOPIC_OVERSEER_TICK": ("publish_topics", "overseer-tick.v1"),
    },
    "node_review_thread_reconciler.handlers.handler_review_thread_reconciler": {
        "TOPIC_EVT_THREAD_REOPENED": ("publish_topics", "review-thread-reopened.v1"),
    },
}


def _declared(module: str, section: str) -> list[str]:
    contract = _NODES / module.split(".")[0] / "contract.yaml"
    raw = yaml.safe_load(contract.read_text(encoding="utf-8"))
    topics = raw["event_bus"][section]
    assert isinstance(topics, list)
    return [str(t) for t in topics]


@pytest.mark.unit
@pytest.mark.parametrize("module", sorted(_SLICE))
def test_constants_are_declared_by_the_owning_contract(module: str) -> None:
    loaded = importlib.import_module(f"omnimarket.nodes.{module}")
    for constant, (section, suffix) in _SLICE[module].items():
        value = getattr(loaded, constant)
        assert value in _declared(module, section)
        assert value.endswith(suffix)


@pytest.mark.unit
@pytest.mark.parametrize("module", sorted(_SLICE))
def test_module_holds_no_topic_literal(module: str) -> None:
    source = (_NODES / f"{module.replace('.', '/')}.py").read_text(encoding="utf-8")
    assert _TOPIC_LITERAL.search(source) is None


@pytest.mark.unit
def test_contract_topic_refuses_a_missing_or_ambiguous_suffix(tmp_path: Path) -> None:
    contract = tmp_path / "contract.yaml"
    contract.write_text(
        yaml.safe_dump(
            {"event_bus": {"publish_topics": ["a.x-done.v1", "b.x-done.v1", "c.y.v1"]}}
        ),
        encoding="utf-8",
    )
    assert contract_topic(contract, "publish_topics", "y.v1") == "c.y.v1"
    with pytest.raises(ValueError, match="found 0"):
        contract_topic(contract, "publish_topics", "z.v1")
    with pytest.raises(ValueError, match="found 2"):
        contract_topic(contract, "publish_topics", "x-done.v1")
    with pytest.raises(ValueError, match="unsupported"):
        contract_topic(contract, "dlq_topics", "y.v1")
