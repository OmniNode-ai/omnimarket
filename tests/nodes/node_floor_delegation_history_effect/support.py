# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared builders of the delegation history effect tests: a runtime from the fixture overlay and a fake command runner."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import yaml

from omnimarket.models.delegation.model_delegation_routing_overlay import (
    ModelDelegationRoutingOverlay,
)
from omnimarket.nodes.node_floor_delegation_history_effect.handlers.handler_floor_delegation_history import (
    load_runtime,
)
from omnimarket.nodes.node_floor_delegation_history_effect.models.model_floor_delegation_history import (
    ModelHistoryOverlay,
)

NOW = datetime(2026, 11, 14, 2, 30, tzinfo=UTC)
HERE = Path(__file__).parent
FIXTURES = HERE / "fixtures"
OVERLAY = ModelHistoryOverlay.model_validate(
    yaml.safe_load((FIXTURES / "overlay.yaml").read_text(encoding="utf-8"))
)
RT = load_runtime(overlay=OVERLAY)
ENVIRON = {RT.database.ssh_host_env: "lab-db"}


def routing_overlay() -> ModelDelegationRoutingOverlay:
    """A routing overlay with the two local arms the fixture overlay's rules name (documentation addresses, never a lab host)."""
    return ModelDelegationRoutingOverlay.model_validate(
        {
            "schema_version": "delegation_routing_overlay.v1",
            "eval_arms": {
                "spend_cap_usd": "1",
                "backends": [
                    {
                        "backend_id": "eval-local-gpu1-qwen3-8-27b-vllm",
                        "provider": "local",
                        "endpoint_url": "http://192.0.2.10:8000/v1/chat/completions",
                        "model_name": "Model-X",
                        "tier": "local_eval",
                        "explicit_pin_only": True,
                        "surface": "internal",
                        "tenant_scope": "house",
                        "capabilities": ["code_generation"],
                    },
                    {
                        "backend_id": "eval-local-gpu2-qwen3-8-27b-q3km-llamacpp",
                        "provider": "local",
                        "endpoint_url": "http://192.0.2.11:8000/v1/chat/completions",
                        "model_name": "Model-X",
                        "tier": "local_eval",
                        "explicit_pin_only": True,
                        "surface": "internal",
                        "tenant_scope": "house",
                        "capabilities": ["code_generation"],
                    },
                ],
                "arms": [
                    {
                        "backend_id": "eval-local-gpu1-qwen3-8-27b-vllm",
                        "slug": "Model-X",
                        "tier": "local_eval",
                        "price_in_usd_per_mtok": "0",
                        "price_out_usd_per_mtok": "0",
                        "context_length": 131072,
                        "task_classes": ["code_generation"],
                        "catalog_read_at": "2026-11-11T21:00:00Z",
                    },
                    {
                        "backend_id": "eval-local-gpu2-qwen3-8-27b-q3km-llamacpp",
                        "slug": "Model-X",
                        "tier": "local_eval",
                        "price_in_usd_per_mtok": "0",
                        "price_out_usd_per_mtok": "0",
                        "context_length": 131072,
                        "task_classes": ["code_generation"],
                        "catalog_read_at": "2026-11-11T21:00:00Z",
                    },
                ],
            },
        }
    )


def projection_answer(*, hours: int = 3) -> str:
    """What the database host answers: the marker, then attempt, latency and seen rows of one local model and one hosted model."""
    lines = ["history-read"]
    for h in range(hours):
        bucket = f"2026-11-14T0{h}:00:00Z"
        for backend, host, name in (
            ("eval-local-gpu1-qwen3-8-27b-vllm", "192.0.2.10", "Model-X"),
            ("harness:claude-glm:flash", "client-1", "glm-flash"),
        ):
            lines.append(
                "A "
                + json.dumps(
                    {
                        "bucket": bucket,
                        "model_name": name,
                        "backend_id": backend,
                        "host": host,
                        "task_type": "document",
                        "attempts": 4,
                        "accepted": 3,
                        "timeouts": 1,
                        "cost_usd": 0.0,
                    }
                )
            )
            lines.append(
                "L "
                + json.dumps(
                    {
                        "bucket": bucket,
                        "model_name": name,
                        "backend_id": backend,
                        "host": host,
                        "task_type": "document",
                        "latencies_ms": [100, 200, 300],
                    }
                )
            )
    lines.append(
        "S "
        + json.dumps(
            {
                "model_name": "Model-X",
                "backend_id": "eval-local-gpu1-qwen3-8-27b-vllm",
                "host": "192.0.2.10",
                "first_seen": "2026-09-01T00:00:00Z",
                "last_seen": "2026-11-14T02:00:00Z",
            }
        )
    )
    return "\n".join(lines) + "\n"


def listing(*files: tuple[str, int, str]) -> str:
    """The reports of one host as the listing script prints them."""
    return "".join(f"@@FILE {name} {mtime}\n{text}\n" for name, mtime, text in files)


class FakeRunner:
    """Answers by what the script contains: the projection query, or one report host (local ``sh -c`` or ssh to its target)."""

    def __init__(
        self,
        projection: tuple[int, str, str] | None = None,
        reports: dict[str, tuple[int, str, str]] | None = None,
    ) -> None:
        self.projection = projection or (0, projection_answer(), "")
        self.reports = reports or {}
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str], timeout: int) -> tuple[int, str, str]:
        self.calls.append(list(argv))
        text = " ".join(argv)
        if "BEGIN READ ONLY" in text:
            return self.projection
        for key, answer in self.reports.items():
            if key in text:
                return answer
        return 1, "", "no such host"
