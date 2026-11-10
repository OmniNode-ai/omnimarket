# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Requests for the boot set tests, built from the figures read on the dev runtime.

Today's start (read 2026-11-09 16:22Z on the dev lane of the lab host, container started 16:18:39Z, still starting at
16:22:12Z): 1,153 contracts announced and admitted, 638 contracts to attach, 439 topic subscription updates, 1.06 GiB.
The contract names below the first six are synthetic stand-ins; the first six are the dev runtime's own priority
contracts, which is the shape of a declared set a deployment would name.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from omnimarket.models.model_runtime_boot_set import (
    ModelBootCatalogEntry,
    ModelBootObservation,
    ModelBootSetDeclaration,
    ModelRuntimeBootSetRequest,
)

CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_runtime_boot_set_compute/contract.yaml"
)
DEPLOYMENT = "dev"
CORRELATION = "boot-set-chain"
DECLARED = (
    "node_delegate_skill_orchestrator",
    "node_delegation_orchestrator",
    "node_delegation_chain_ledger_effect",
    "projection_claude_hook_events",
    "projection_hook_ledger",
    "node_contract_registry",
)
KERNEL_TOPIC = "fixture-kernel-runtime-config"
ANNOUNCED = 1153
ATTACHED_TODAY = 638
SUBSCRIBED_TODAY = 439


def topic_of(contract: str) -> str:
    return f"fixture-in-{contract.replace('_', '-')}"


def catalog() -> tuple[ModelBootCatalogEntry, ...]:
    names = list(DECLARED) + [
        f"node_fixture_{index:04d}" for index in range(ANNOUNCED - len(DECLARED))
    ]
    return tuple(
        ModelBootCatalogEntry(
            contract=name,
            subscribe_topics=(topic_of(name),),
            publish_topics=(f"fixture-out-{name.replace('_', '-')}",),
        )
        for name in names
    )


def declaration(deployment: str = DEPLOYMENT) -> ModelBootSetDeclaration:
    return ModelBootSetDeclaration(
        deployment=deployment, contracts=DECLARED, kernel_topics=(KERNEL_TOPIC,)
    )


def declared_boot() -> ModelBootObservation:
    """A boot that attaches only the declared contracts and the kernel's own topic."""
    return ModelBootObservation(
        attached_contracts=DECLARED,
        subscribed_topics=(*(topic_of(name) for name in DECLARED), KERNEL_TOPIC),
        time_to_healthy_seconds=12.0,
        memory_mib=310.0,
    )


def todays_boot() -> ModelBootObservation:
    """Today's start: 638 contracts attached and 439 subscriptions, whatever the deployment needs."""
    extra = [
        f"node_fixture_{index:04d}" for index in range(ATTACHED_TODAY - len(DECLARED))
    ]
    attached = (*DECLARED, *extra)
    topics = [topic_of(name) for name in attached][: SUBSCRIBED_TODAY - 1]
    return ModelBootObservation(
        attached_contracts=attached,
        subscribed_topics=(*topics, KERNEL_TOPIC),
        time_to_healthy_seconds=None,
        memory_mib=1086.0,
    )


def request(
    observation: ModelBootObservation,
    declared: ModelBootSetDeclaration | None,
    deployment: str = DEPLOYMENT,
) -> ModelRuntimeBootSetRequest:
    return ModelRuntimeBootSetRequest(
        correlation_id=CORRELATION,
        deployment=deployment,
        declaration=declared,
        catalog=catalog(),
        observation=observation,
    )


def load_topics(path: Path = CONTRACT) -> dict[str, str]:
    """Topics by role from the node's contract; the handler holds none."""
    return dict(yaml.safe_load(path.read_text(encoding="utf-8"))["topics"])
