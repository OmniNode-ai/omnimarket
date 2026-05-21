#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bus-triggered proof-of-life publisher for node_overseer_verifier.

Publishes typed proof commands to the node contract's command topic. The
runtime-owned verifier consumer performs the actual verification and emits the
completion event.

Related:
    - OMN-8035: Proof of life - run verifier and seam-parallel via onex run
    - OMN-11173: Rewrite proof_of_life_overseer_verifier.py as bus-triggered
      proof-of-life
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import yaml
from pydantic import BaseModel, ConfigDict, Field

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CONTRACT_PATH = (
    _REPO_ROOT
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_overseer_verifier"
    / "contract.yaml"
)
_OMNIMARKET_COMMAND_TOPIC_PREFIX = ".".join(("onex", "cmd", "omnimarket", ""))

CaseName = Literal["pass", "low-confidence", "negative-cost", "bad-action"]


class ModelOverseerVerifyCommand(BaseModel):
    """Wire command consumed by node_overseer_verifier."""

    model_config = ConfigDict(extra="forbid")

    correlation_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    status: str = Field(min_length=1)
    domain: str = Field(min_length=1)
    node_id: str = Field(min_length=1)
    runner_id: str | None = None
    attempt: int = Field(default=1, ge=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    cost_so_far: float | None = None
    allowed_actions: list[str] = Field(default_factory=list)
    declared_invariants: list[str] = Field(default_factory=list)
    schema_version: str = "1.0"


@dataclass(frozen=True)
class ProofCase:
    name: CaseName
    label: str
    expected_verdict: str
    command: ModelOverseerVerifyCommand


def _load_command_topic(contract_path: Path = _CONTRACT_PATH) -> str:
    """Load the verifier command topic from the node contract."""
    raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    topics = raw.get("event_bus", {}).get("subscribe_topics", [])
    command_topics = [
        topic
        for topic in topics
        if isinstance(topic, str)
        and topic.startswith(_OMNIMARKET_COMMAND_TOPIC_PREFIX)
    ]
    if len(command_topics) != 1:
        raise RuntimeError(
            f"Expected exactly one omnimarket command topic in {contract_path}, "
            f"found {command_topics!r}"
        )
    return command_topics[0]


def _make_command(case_name: CaseName, **overrides: Any) -> ModelOverseerVerifyCommand:
    defaults: dict[str, Any] = {
        "correlation_id": f"proof-of-life-{case_name}-{uuid4()}",
        "task_id": f"proof-of-life-{case_name}",
        "status": "completed",
        "domain": "overseer",
        "node_id": "node_overseer_verifier",
        "confidence": 0.92,
        "cost_so_far": 0.0042,
        "allowed_actions": ["dispatch", "complete"],
        "declared_invariants": ["cost_non_negative"],
        "schema_version": "1.0",
    }
    defaults.update(overrides)
    return ModelOverseerVerifyCommand.model_validate(defaults)


def _build_cases() -> tuple[ProofCase, ...]:
    """Build the same four proof cases the old direct verifier script exercised."""
    return (
        ProofCase(
            name="pass",
            label="PASS - valid envelope",
            expected_verdict="PASS",
            command=_make_command("pass"),
        ),
        ProofCase(
            name="low-confidence",
            label="ESCALATE - low confidence (0.12)",
            expected_verdict="ESCALATE",
            command=_make_command(
                "low-confidence",
                task_id="proof-of-life-low-confidence",
                confidence=0.12,
                cost_so_far=0.001,
                allowed_actions=["complete"],
                declared_invariants=[],
            ),
        ),
        ProofCase(
            name="negative-cost",
            label="ESCALATE - negative cost_so_far",
            expected_verdict="ESCALATE",
            command=_make_command(
                "negative-cost",
                task_id="proof-of-life-negative-cost",
                confidence=0.95,
                cost_so_far=-0.5,
                allowed_actions=["complete"],
                declared_invariants=[],
            ),
        ),
        ProofCase(
            name="bad-action",
            label="FAIL - unknown action 'delete_all'",
            expected_verdict="FAIL",
            command=_make_command(
                "bad-action",
                task_id="proof-of-life-bad-action",
                status="running",
                confidence=0.88,
                cost_so_far=0.002,
                allowed_actions=["dispatch", "delete_all"],
                declared_invariants=[],
            ),
        ),
    )


def _select_cases(selected: Sequence[CaseName]) -> tuple[ProofCase, ...]:
    cases = _build_cases()
    if not selected:
        return cases
    selected_names = set(selected)
    return tuple(case for case in cases if case.name in selected_names)


def _print_case(case: ProofCase, topic: str, dry_run: bool) -> None:
    print(f"\n{'=' * 60}")
    print(f"CASE: {case.label}")
    print(f"{'=' * 60}")
    print(f"MODE: {'dry-run' if dry_run else 'publish'}")
    print(f"TOPIC: {topic}")
    print(f"EXPECTED VERDICT: {case.expected_verdict}")
    print(json.dumps(case.command.model_dump(mode="json"), indent=2))


async def _publish_commands(
    *,
    topic: str,
    commands: Sequence[ModelOverseerVerifyCommand],
    bootstrap_servers: str | None,
) -> None:
    from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka
    from omnibase_infra.event_bus.models.config import ModelKafkaEventBusConfig

    bus = (
        EventBusKafka(
            config=ModelKafkaEventBusConfig(bootstrap_servers=bootstrap_servers)
        )
        if bootstrap_servers
        else EventBusKafka.default()
    )
    await bus.start()
    try:
        for command in commands:
            await bus.publish(
                topic=topic,
                key=None,
                value=command.model_dump_json().encode("utf-8"),
                headers=None,
            )
    finally:
        await bus.close()


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Publish node_overseer_verifier proof-of-life commands to the "
            "contract-declared event-bus command topic."
        )
    )
    parser.add_argument(
        "--case",
        action="append",
        choices=["pass", "low-confidence", "negative-cost", "bad-action"],
        default=[],
        help="Proof case to publish. Repeat to publish multiple cases. Default: all.",
    )
    parser.add_argument(
        "--topic",
        default=None,
        help="Override command topic. Default: read from node_overseer_verifier contract.",
    )
    parser.add_argument(
        "--bootstrap",
        default=os.environ.get("KAFKA_BOOTSTRAP_SERVERS", ""),
        help="Kafka bootstrap servers. Default: KAFKA_BOOTSTRAP_SERVERS.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print typed commands without publishing to Kafka.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    topic = args.topic or _load_command_topic()
    selected_cases = _select_cases(args.case)

    for case in selected_cases:
        _print_case(case, topic, dry_run=args.dry_run)

    if args.dry_run:
        print("\nDRY RUN: no proof-of-life commands were published")
        return 0

    bootstrap = str(args.bootstrap).strip()
    if not bootstrap:
        print(
            "ERROR: KAFKA_BOOTSTRAP_SERVERS is not set and --bootstrap was not provided",
            file=sys.stderr,
        )
        return 2

    print(f"\nPublishing {len(selected_cases)} command(s) at {datetime.now(tz=UTC)}")
    asyncio.run(
        _publish_commands(
            topic=topic,
            commands=[case.command for case in selected_cases],
            bootstrap_servers=bootstrap,
        )
    )
    print("ALL VERIFIER PROOF-OF-LIFE COMMANDS PUBLISHED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
