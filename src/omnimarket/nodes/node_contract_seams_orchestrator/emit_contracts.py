# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Emit contract YAML documents for design nodes."""

from __future__ import annotations

from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict

from omnimarket.models.contract_seams_design import (
    ModelContractSeamsDesign,
    ModelDesignCrossing,
    ModelDesignNode,
)

__all__ = [
    "ModelEmitConfig",
    "ModelEmitContractsResult",
    "ModelEmittedContract",
    "emit_contracts",
]


class ModelEmitConfig(BaseModel):
    """Configuration for emitting contract documents."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    handler_module_template: str
    handler_class_template: str
    input_model_template: str
    emitted_version: dict[str, int]
    emitted_lifecycle: str
    generated_by: str


class ModelEmittedContract(BaseModel):
    """A single emitted contract document."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node_name: str
    contract_yaml: str


class ModelEmitContractsResult(BaseModel):
    """Result of emitting contracts for a design."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    contracts: tuple[ModelEmittedContract, ...]
    design_issues: tuple[str, ...]


def _format_template(template: str, node_name: str) -> str:
    """Format a template string with node_name, stem, and pascal keys."""
    stem = node_name.removeprefix("node_")
    pascal = "".join(part.capitalize() for part in stem.split("_"))
    return template.format(node_name=node_name, stem=stem, pascal=pascal)


def _build_seams(
    node: ModelDesignNode,
    crossings: tuple[ModelDesignCrossing, ...],
) -> list[dict[str, object]]:
    """Build the seams list for a node from crossings touching it."""
    seams: list[dict[str, object]] = []
    for crossing in crossings:
        role: Literal["producer", "consumer"] | None = None
        if crossing.producer == node.node_name:
            role = "producer"
        elif crossing.consumer == node.node_name:
            role = "consumer"
        else:
            continue

        side = crossing.producer_side if role == "producer" else crossing.consumer_side
        key_fields = [
            {"name": field.name, "field_type": field.field_type}
            for field in side.key_fields
        ]
        seams.append(
            {
                "id": crossing.crossing_id,
                "seam": crossing.crossing_id,
                "role": role,
                "topic": crossing.topic,
                "envelope_model": side.envelope_model,
                "envelope_version": side.envelope_version,
                "key_fields": key_fields,
                "delivery_semantics": side.delivery_semantics.value,
            }
        )
    return seams


def emit_contracts(
    design: ModelContractSeamsDesign,
    config: ModelEmitConfig,
) -> ModelEmitContractsResult:
    """Emit contract YAML documents for each node in the design."""
    design_issues = design.issues()
    if design_issues:
        return ModelEmitContractsResult(contracts=(), design_issues=design_issues)

    contracts: list[ModelEmittedContract] = []
    for node in design.nodes:
        subscribe_topics: list[str] = [
            crossing.topic
            for crossing in design.crossings
            if crossing.consumer == node.node_name
        ]
        if design.head.node_name == node.node_name:
            subscribe_topics.append(design.head.topic)

        publish_topics: list[str] = [
            crossing.topic
            for crossing in design.crossings
            if crossing.producer == node.node_name
        ]
        if design.tail.node_name == node.node_name:
            publish_topics.append(design.tail.topic)

        document: dict[str, object] = {
            "name": node.node_name,
            "contract_name": node.node_name,
            "node_name": node.node_name,
            "contract_version": dict(config.emitted_version),
            "node_version": dict(config.emitted_version),
            "node_type": node.archetype.value,
            "lifecycle": config.emitted_lifecycle,
            "description": node.description,
            "handler": {
                "module": _format_template(
                    config.handler_module_template, node.node_name
                ),
                "class": _format_template(
                    config.handler_class_template, node.node_name
                ),
                "input_model": _format_template(
                    config.input_model_template, node.node_name
                ),
            },
            "descriptor": {"node_archetype": node.archetype.value},
            "event_bus": {
                "subscribe_topics": sorted(subscribe_topics),
                "publish_topics": sorted(publish_topics),
            },
            "seams": _build_seams(node, design.crossings),
            "metadata": {
                "design_id": design.design_id,
                "generated_by": config.generated_by,
            },
        }

        if design.head.node_name == node.node_name:
            document["runtime_dispatch"] = {"command_topic": design.head.topic}
            document["externally_produced_topics"] = [design.head.topic]

        if design.tail.node_name == node.node_name:
            document["externally_consumed_topics"] = [design.tail.topic]

        if node.state_machine is not None:
            document["state_machine"] = node.state_machine

        contract_yaml = yaml.safe_dump(document, sort_keys=False)
        contracts.append(
            ModelEmittedContract(node_name=node.node_name, contract_yaml=contract_yaml)
        )

    return ModelEmitContractsResult(contracts=tuple(contracts), design_issues=())
