# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The topic of each role, read from the contract's ``topic_roles`` (OMN-26721).

Handlers name a role, never a topic: the contract is the one place a topic is spelled.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

CONTRACT_PATH = Path(__file__).resolve().parent / "contract.yaml"

ROLE_INGRESS = "ingress"
ROLE_STEP_COMPLETED = "step_completed"
ROLE_TRANSITION = "transition"
ROLE_TERMINAL = "terminal"
ROLE_LEDGER_APPEND = "ledger_append"
ROLE_LEDGER_QUERY = "ledger_query"
ROLE_LAB_PROOF = "lab_proof"
ROLE_REPO_WRITE = "repo_write"
ROLE_HANDOFF = "handoff"
ROLE_HELP_REQUESTED = "help_requested"


@lru_cache(maxsize=1)
def topic_roles() -> dict[str, str]:
    """Role to topic, as the contract declares them."""
    text = CONTRACT_PATH.read_text(encoding="utf-8")  # node-purity-ok: own contract
    roles = yaml.safe_load(text)["topic_roles"]
    return {str(role): str(topic) for role, topic in roles.items()}


def topic_for(role: str) -> str:
    """The topic of ``role``; an undeclared role is a defect, never a default."""
    try:
        return topic_roles()[role]
    except KeyError as error:
        msg = f"role {role!r} is not declared in the contract's topic_roles"
        raise KeyError(msg) from error


__all__: list[str] = [
    "CONTRACT_PATH",
    "ROLE_HANDOFF",
    "ROLE_HELP_REQUESTED",
    "ROLE_INGRESS",
    "ROLE_LAB_PROOF",
    "ROLE_LEDGER_APPEND",
    "ROLE_LEDGER_QUERY",
    "ROLE_REPO_WRITE",
    "ROLE_STEP_COMPLETED",
    "ROLE_TERMINAL",
    "ROLE_TRANSITION",
    "topic_for",
    "topic_roles",
]
