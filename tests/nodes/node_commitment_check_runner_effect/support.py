# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared builders of the commitment check runner tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.commitment import (
    ModelCommitmentLedgerRequest,
    ModelCommitmentRules,
)
from omnimarket.nodes.node_commitment_check_runner_effect.models import (
    ModelCommitmentCheckRules,
)
from omnimarket.nodes.node_commitment_ledger_compute.handlers.handler_commitment_ledger import (
    HandlerCommitmentLedger,
)

RUN_AT = datetime(2026, 11, 7, 16, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[3]
NODES = ROOT / "src/omnimarket/nodes"
CHECK = "uv run onex host-read --host h053 --expect nvme-root"
ASK = "Switch h053 to the NVMe and tell me when the host reads it"


def runner_rules(**changes: object) -> ModelCommitmentCheckRules:
    explicit = yaml.safe_load(
        (NODES / "node_commitment_check_runner_effect/overlay.example.yaml").read_text(
            encoding="utf-8"
        )
    )
    explicit.update(changes)
    return load_structured_overlay(
        "node_commitment_check_runner_effect",
        ModelCommitmentCheckRules,
        explicit=explicit,
    )


def ledger_rules() -> ModelCommitmentRules:
    return load_structured_overlay(
        "node_commitment_ledger_compute",
        ModelCommitmentRules,
        explicit=yaml.safe_load(
            (NODES / "node_commitment_ledger_compute/overlay.example.yaml").read_text(
                encoding="utf-8"
            )
        ),
    )


def open_commitment_line(
    minutes_ago: float = 180, check: str = CHECK, lane: str = "orchestrator-31d9"
) -> str:
    result = HandlerCommitmentLedger().handle(
        ModelCommitmentLedgerRequest(
            operation="build_open_row",
            rules=ledger_rules(),
            now=RUN_AT - timedelta(minutes=minutes_ago),
            lane=lane,
            ask=ASK,
            owner="host-bringup-1",
            check=check,
        )
    )
    assert result.row is not None, result.refusals
    return result.row


def commitment_id(minutes_ago: float = 180, lane: str = "orchestrator-31d9") -> str:
    return f"{(RUN_AT - timedelta(minutes=minutes_ago)):%Y-%m-%dT%H:%M:%SZ}-{lane}"


def reading_line(
    status: str, exit_code: int, minutes_ago: float, cid: str | None = None
) -> str:
    stamp = f"{(RUN_AT - timedelta(minutes=minutes_ago)):%Y-%m-%dT%H:%M:%SZ}"
    return (
        f"{stamp} | STATUS | lane=commitment-check-runner | kind=commitment | "
        f"commitment={cid or commitment_id()} | status={status} | exit={exit_code} | a reading"
    )
