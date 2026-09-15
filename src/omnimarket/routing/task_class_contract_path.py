# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Shared task-class contract path authority (OMN-17802).

The ONE derivation of where the delegation task-class contract lives. Both the
routing authority (``node_delegation_routing_reducer``, which parses the file
for the escalation policy and the required quality bar) and the delegation
orchestrator (``node_delegation_orchestrator``, which records the file's sha256
as the v2 terminal's ``escalation_config_hash``) import from here, so neither
node depends on the other's handler package.

This mirrors :mod:`omnimarket.routing.routing_tiers_path` deliberately. That
module exists because the orchestrator once re-derived the tiers path with its
own ``Path(__file__).parent`` walk, got the depth wrong, and silently nulled a
provenance hash on every terminal. Adding a second private walk for a second
config file would re-open exactly that defect, so the second file gets the same
treatment as the first: one derivation, imported by both readers.
"""

from __future__ import annotations

from pathlib import Path

#: Env key a contract overlay / deployment binds to pin the task-class contract.
TASK_CLASS_CONTRACT_PATH_ENV_KEY = "TASK_CLASS_CONTRACT_PATH"

# ``.parent`` x2 from ``src/omnimarket/routing/task_class_contract_path.py``
# lands on ``src/omnimarket`` -> ``src/omnimarket/configs/``, the single
# committed copy of this file in the repo.
TASK_CLASS_CONTRACT_PACKAGED_DEFAULT_PATH = (
    Path(__file__).parent.parent / "configs" / "task_class_contracts.v1.yaml"
)

__all__ = [
    "TASK_CLASS_CONTRACT_PACKAGED_DEFAULT_PATH",
    "TASK_CLASS_CONTRACT_PATH_ENV_KEY",
]
