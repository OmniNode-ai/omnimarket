#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Publish seam-parallel proof-of-life commands through the event bus.

Related:
    - OMN-8035: Proof of life - run verifier and seam-parallel via onex run
    - OMN-11174: Rewrite proof-of-life script as a bus-triggered publisher
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID

from omnimarket.nodes.contract_topics import contract_subscribe_topics
from omnimarket.nodes.node_seam_parallel_executor.models.model_seam_task import (
    ModelSeamParallelInput,
    ModelSeamTask,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = (
    REPO_ROOT
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_seam_parallel_executor"
    / "contract.yaml"
)
SOURCE_TOOL = "proof_of_life_seam_parallel"
OMNIMARKET_COMMAND_TOPIC_PREFIX = ".".join(("onex", "cmd", "omnimarket", ""))


class ProtocolEventBusPublisher(Protocol):
    async def start(self) -> None: ...

    async def close(self) -> None: ...

    async def publish(
        self,
        topic: str,
        key: bytes | None,
        value: bytes,
        headers: object | None = None,
    ) -> None: ...


@dataclass(frozen=True)
class ProofCase:
    label: str
    command: ModelSeamParallelInput


@dataclass(frozen=True)
class PublishReceipt:
    label: str
    topic: str
    correlation_id: UUID
    payload_bytes: int
    dry_run: bool


def _default_bus_factory() -> ProtocolEventBusPublisher:
    from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka

    return EventBusKafka.default()


def _load_command_topic(contract_path: Path = CONTRACT_PATH) -> str:
    topics = contract_subscribe_topics(contract_path)
    if len(topics) != 1:
        raise ValueError(
            f"{contract_path} must declare exactly one event_bus.subscribe_topics entry"
        )
    topic = topics[0]
    if not topic.startswith(OMNIMARKET_COMMAND_TOPIC_PREFIX):
        raise ValueError(
            f"{contract_path} subscribe topic is not an OmniMarket command"
        )
    return topic


def _independent_tasks_case() -> ProofCase:
    return ProofCase(
        label="2 independent tasks",
        command=ModelSeamParallelInput(
            correlation_id=UUID("00000000-0000-0000-0000-000000000001"),
            tasks=(
                ModelSeamTask(
                    task_id="task-a",
                    callable_key="shim_a",
                    payload={"name": "OutputA"},
                ),
                ModelSeamTask(
                    task_id="task-b",
                    callable_key="shim_b",
                    payload={"name": "OutputB"},
                ),
            ),
            timeout_seconds=10.0,
        ),
    )


def _dependency_chain_case() -> ProofCase:
    return ProofCase(
        label="dependency chain",
        command=ModelSeamParallelInput(
            correlation_id=UUID("00000000-0000-0000-0000-000000000002"),
            tasks=(
                ModelSeamTask(
                    task_id="task-a",
                    callable_key="shim_a",
                    payload={},
                ),
                ModelSeamTask(
                    task_id="task-downstream",
                    callable_key="shim_downstream",
                    depends_on=("task-a",),
                    payload={},
                ),
            ),
            timeout_seconds=10.0,
        ),
    )


def build_proof_cases(case_name: str) -> tuple[ProofCase, ...]:
    cases = {
        "independent": _independent_tasks_case(),
        "dependency-chain": _dependency_chain_case(),
    }
    if case_name == "all":
        return tuple(cases.values())
    try:
        return (cases[case_name],)
    except KeyError as exc:
        raise ValueError(f"Unknown proof case: {case_name}") from exc


def _command_payload(command: ModelSeamParallelInput) -> bytes:
    return command.model_dump_json().encode("utf-8")


def _print_case(receipt: PublishReceipt, command: ModelSeamParallelInput) -> None:
    print(f"\n{'=' * 60}")
    print(f"CASE: {receipt.label}")
    print(f"{'=' * 60}")
    status = "dry_run" if receipt.dry_run else "published"
    data = {
        "status": status,
        "topic": receipt.topic,
        "correlation_id": str(receipt.correlation_id),
        "payload_bytes": receipt.payload_bytes,
        "tasks": [
            {
                "task_id": task.task_id,
                "callable_key": task.callable_key,
                "depends_on": list(task.depends_on),
                "payload": task.payload,
            }
            for task in command.tasks
        ],
        "timeout_seconds": command.timeout_seconds,
    }
    print(json.dumps(data, indent=2, default=str))


async def publish_proof_of_life(
    *,
    case_name: str = "all",
    dry_run: bool = False,
    bus_factory: Callable[[], ProtocolEventBusPublisher] = _default_bus_factory,
) -> tuple[PublishReceipt, ...]:
    topic = _load_command_topic()
    cases = build_proof_cases(case_name)
    receipts: list[PublishReceipt] = []

    if dry_run:
        for proof_case in cases:
            payload = _command_payload(proof_case.command)
            receipts.append(
                PublishReceipt(
                    label=proof_case.label,
                    topic=topic,
                    correlation_id=proof_case.command.correlation_id,
                    payload_bytes=len(payload),
                    dry_run=True,
                )
            )
        return tuple(receipts)

    bus = bus_factory()
    await bus.start()
    try:
        for proof_case in cases:
            payload = _command_payload(proof_case.command)
            await bus.publish(topic=topic, key=None, value=payload, headers=None)
            receipts.append(
                PublishReceipt(
                    label=proof_case.label,
                    topic=topic,
                    correlation_id=proof_case.command.correlation_id,
                    payload_bytes=len(payload),
                    dry_run=False,
                )
            )
    finally:
        await bus.close()

    return tuple(receipts)


async def _main_async(args: argparse.Namespace) -> int:
    receipts = await publish_proof_of_life(
        case_name=args.case,
        dry_run=args.dry_run,
    )
    cases_by_correlation = {
        proof_case.command.correlation_id: proof_case
        for proof_case in build_proof_cases(args.case)
    }
    for receipt in receipts:
        _print_case(receipt, cases_by_correlation[receipt.correlation_id].command)

    action = "COMPILED" if args.dry_run else "PUBLISHED"
    print("\n" + "=" * 60)
    print(f"ALL SEAM-PARALLEL PROOF-OF-LIFE COMMANDS {action}")
    print("=" * 60)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--case",
        choices=("all", "independent", "dependency-chain"),
        default="all",
        help="Proof-of-life command case to publish",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build and print command payload metadata without publishing to Kafka",
    )
    args = parser.parse_args(argv)

    try:
        return asyncio.run(_main_async(args))
    except Exception as exc:
        print(f"{SOURCE_TOOL}: failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
