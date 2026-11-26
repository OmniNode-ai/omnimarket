# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The operation topic pairs and the dev guard's lane, read from the contracts that declare them.

No topic string lives in a handler. The provision node declares a ``topics`` block (one requested and one completed topic
per operation); the gateway key node declares each command as a ``handler_routing`` route and each answer as a
``published_events`` entry. A contract is located beside this node under ``nodes/``, never imported, so no private handler or model of the other
node is loaded. A contract that cannot be found or that lacks an operation is a typed refusal, never a guess.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import yaml
from pydantic import BaseModel, ConfigDict

PROVISION_NODE: Final[str] = "node_tenant_provision_effect"
KEY_NODE: Final[str] = "node_tenant_api_key_effect"
# The nodes directory this package installs beside this node: a contract is a sibling node's file, located by path and
# never imported, so no private handler or model of the other node is loaded.
_NODES_ROOT: Final[Path] = Path(__file__).resolve().parents[2]


class ContractUnavailableError(RuntimeError):
    """The contract of a node this command asks is not installed, or does not declare the operation."""


class ModelOperationTopics(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    node_id: str
    requested: str
    completed: str


def _contract(node_id: str) -> dict[str, Any]:
    path = _NODES_ROOT / node_id / "contract.yaml"
    if not path.is_file():
        raise ContractUnavailableError(
            f"{node_id} is not installed in this package: its contract declares the topics this command publishes on"
        )
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ContractUnavailableError(f"the contract of {node_id} is not a mapping")
    return raw


def provision_topics(operation: str) -> ModelOperationTopics:
    """The requested and completed topic of one ``topics`` entry of the provision node (``tenant_provision``, ``tenant_read``)."""
    block = _contract(PROVISION_NODE).get("topics", {}).get(operation)
    if not isinstance(block, dict) or not {"requested", "completed"} <= set(block):
        raise ContractUnavailableError(
            f"{PROVISION_NODE} declares no topic pair for {operation}"
        )
    return ModelOperationTopics(
        node_id=PROVISION_NODE,
        requested=str(block["requested"]),
        completed=str(block["completed"]),
    )


def key_topics(operation: str, answer_event: str) -> ModelOperationTopics:
    """The topic pair of one gateway key command: its route by ``operation`` and its answer by ``answer_event``."""
    raw = _contract(KEY_NODE)
    routes = raw.get("handler_routing", {}).get("handlers", [])
    requested = next(
        (r.get("topic") for r in routes if r.get("operation") == operation), None
    )
    events = raw.get("published_events", [])
    completed = next(
        (e.get("topic") for e in events if e.get("event_type") == answer_event), None
    )
    if not requested or not completed:
        raise ContractUnavailableError(
            f"{KEY_NODE} declares no topic pair for {operation}"
        )
    return ModelOperationTopics(
        node_id=KEY_NODE, requested=str(requested), completed=str(completed)
    )


def dev_guard_lane() -> str:
    """The lane a dev-only secret write is allowed on, from the gateway key node's ``dev_only.lane``."""
    lane = _contract(KEY_NODE).get("dev_only", {}).get("lane")
    if not isinstance(lane, str) or not lane:
        raise ContractUnavailableError(f"{KEY_NODE} declares no dev_only.lane")
    return lane


__all__: list[str] = [
    "KEY_NODE",
    "PROVISION_NODE",
    "ContractUnavailableError",
    "ModelOperationTopics",
    "dev_guard_lane",
    "key_topics",
    "provision_topics",
]
