# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Dispatch bindings. The host invokes canonical run-node; a publish is never a lane ACK."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from collections.abc import Mapping
from uuid import NAMESPACE_URL, uuid5

from omnimarket.models.item_state_check import (
    ModelItemDispatchReceipt,
    ModelItemRecoveryPlan,
)


class MemoryItemDispatch:
    def __init__(self, operator_action: str) -> None:
        self.operator_action = operator_action
        self.started: dict[str, ModelItemRecoveryPlan] = {}
        self.receipts: dict[str, ModelItemDispatchReceipt] = {}
        self.lines: list[str] = []
        self._lock = threading.Lock()

    def dispatch(self, plan: ModelItemRecoveryPlan) -> ModelItemDispatchReceipt:
        with self._lock:
            if plan.claim_key not in self.receipts:
                self.started[plan.claim_key] = plan
                self.receipts[plan.claim_key] = ModelItemDispatchReceipt(
                    cause=plan.cause,
                    lane=None
                    if plan.action == self.operator_action
                    else f"item-{uuid5(NAMESPACE_URL, plan.claim_key).hex}",
                    accepted=True,
                )
            return self.receipts[plan.claim_key]

    def print_line(self, line: str) -> bool:
        self.lines.append(line)
        return True


class BusItemDispatch:
    def __init__(
        self, executable: str, routes: Mapping[str, str], timeout_seconds: float
    ) -> None:
        self.executable = executable
        self.routes = dict(routes)
        self.timeout_seconds = timeout_seconds

    def dispatch(self, plan: ModelItemRecoveryPlan) -> ModelItemDispatchReceipt:
        result = subprocess.run(
            [
                self.executable,
                "run-node",
                self.routes[plan.action],
                "--input",
                plan.model_dump_json(),
            ],
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            check=True,
        )
        return ModelItemDispatchReceipt.model_validate_json(result.stdout)

    def print_line(self, line: str) -> bool:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
        return True


def memory_item_dispatch(config: Mapping[str, str]) -> object:
    return MemoryItemDispatch(config["operator_action"])


def bus_item_dispatch(config: Mapping[str, str]) -> object:
    return BusItemDispatch(
        config["executable"],
        json.loads(config["routes"]),
        float(config["timeout_seconds"]),
    )
