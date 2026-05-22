# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Golden chain generator handler — deterministic, AST-based, pure compute."""

from __future__ import annotations

import ast
import hashlib
import json
import re
from datetime import UTC, datetime

import yaml

from omnimarket.nodes.node_golden_chain_generator.models.model_generation_request import (
    ModelGoldenChainGenerationRequest,
)
from omnimarket.nodes.node_golden_chain_generator.models.model_generation_result import (
    EnumGenerationStatus,
    ModelGoldenChainGenerationResult,
)
from omnimarket.nodes.node_golden_chain_generator.models.model_golden_chain_entry import (
    ModelGoldenChainEntry,
)

_TOPIC_PATTERN = re.compile(r"^onex\.(cmd|evt)\.[\w-]+\.[\w-]+\.v\d+$")
_UNKNOWN_TOPIC = "UNKNOWN"
_UNKNOWN_NODE = "UNKNOWN"


def _canonical_json(chain: tuple[ModelGoldenChainEntry, ...]) -> str:
    return json.dumps(
        [e.model_dump(mode="json") for e in chain],
        sort_keys=True,
        separators=(",", ":"),
    )


def _chain_hash(chain: tuple[ModelGoldenChainEntry, ...]) -> str:
    return hashlib.sha256(_canonical_json(chain).encode("utf-8")).hexdigest()


def _parse_contract(contract_yaml: str) -> dict[str, object]:
    try:
        parsed = yaml.safe_load(contract_yaml)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _extract_topics_from_contract(
    contract: dict[str, object],
) -> tuple[list[str], list[str]]:
    """Return (subscribe_topics, publish_topics) declared in contract event_bus."""
    event_bus = contract.get("event_bus", {})
    if not isinstance(event_bus, dict):
        return [], []

    raw_sub = event_bus.get("subscribe_topics", [])
    raw_pub = event_bus.get("publish_topics", [])

    subscribe_topics = list(raw_sub) if isinstance(raw_sub, list) else []
    publish_topics = list(raw_pub) if isinstance(raw_pub, list) else []
    return subscribe_topics, publish_topics


def _node_name_from_contract(contract: dict[str, object]) -> str:
    name = contract.get("name", "")
    return str(name) if name else _UNKNOWN_NODE


def _extract_topics_from_ast(test_source: str) -> list[str]:
    """
    Walk the AST of generated test source looking for string literals that
    match the canonical onex topic pattern (onex.{cmd|evt}.service.event.vN).

    Returns only literals that are syntactically constant — dynamic
    composition (f-strings, concatenation, variables) produces no entries.
    """
    if not test_source.strip():
        return []

    try:
        tree = ast.parse(test_source)
    except SyntaxError:
        return []

    topics: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            val: str = node.value
            if _TOPIC_PATTERN.match(val):
                topics.append(val)

    # Deduplicate while preserving discovery order
    seen: set[str] = set()
    deduped: list[str] = []
    for t in topics:
        if t not in seen:
            seen.add(t)
            deduped.append(t)
    return deduped


def _build_chain_from_contract(
    subscribe_topics: list[str],
    publish_topics: list[str],
    node_name: str,
    ast_topics: list[str],
) -> tuple[ModelGoldenChainEntry, ...]:
    """
    Primary source: contract declared topics.
    Secondary: AST-extracted topic literals not already declared (appended
    with UNKNOWN source, since AST cannot determine the emitting node).
    Dynamic or ambiguous paths are never inferred.
    """
    entries: list[ModelGoldenChainEntry] = []
    seq = 0

    # Contract subscribe → entry for the incoming command
    for topic in subscribe_topics:
        entries.append(
            ModelGoldenChainEntry(
                sequence=seq,
                event_type="command",
                topic=topic,
                source_node=_UNKNOWN_NODE,
            )
        )
        seq += 1

    # Contract publish → entry emitted by this node
    for topic in publish_topics:
        entries.append(
            ModelGoldenChainEntry(
                sequence=seq,
                event_type="event",
                topic=topic,
                source_node=node_name,
            )
        )
        seq += 1

    # AST supplement: topics found in test source not already covered
    declared = {e.topic for e in entries}
    for topic in ast_topics:
        if topic not in declared:
            # Cannot determine emitting node from AST alone → UNKNOWN
            entries.append(
                ModelGoldenChainEntry(
                    sequence=seq,
                    event_type=_UNKNOWN_TOPIC,
                    topic=topic,
                    source_node=_UNKNOWN_NODE,
                )
            )
            seq += 1

    return tuple(entries)


class HandlerGoldenChainGenerator:
    """Derive the expected golden chain from contract + AST. Pure, no I/O."""

    def handle(
        self, request: ModelGoldenChainGenerationRequest
    ) -> ModelGoldenChainGenerationResult:
        contract = _parse_contract(request.contract_yaml)
        subscribe_topics, publish_topics = _extract_topics_from_contract(contract)
        node_name = _node_name_from_contract(contract)

        ast_topics = _extract_topics_from_ast(request.test_source)

        chain = _build_chain_from_contract(
            subscribe_topics, publish_topics, node_name, ast_topics
        )

        ch = _chain_hash(chain)
        generated_at = datetime.now(tz=UTC).isoformat()

        return ModelGoldenChainGenerationResult(
            status=EnumGenerationStatus.OK,
            expected_chain=chain,
            chain_hash=ch,
            contract_hash=request.contract_hash,
            generator_version=request.generator_version,
            template_hash=request.template_hash,
            generation_profile_hash=request.generation_profile_hash,
            generated_at=generated_at,
        )


__all__ = ["HandlerGoldenChainGenerator"]
