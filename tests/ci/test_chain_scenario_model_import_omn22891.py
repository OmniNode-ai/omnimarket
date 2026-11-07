# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22891: omnimarket's locked omnibase_core carries the chain scenario subcontract model.

The chain scenario generator (a node of omnimarket) builds on omnibase_core's chain scenario model and validator.
Core added them in omnibase_core#2269; a lock below that commit raises ModuleNotFoundError on the import.
"""

from __future__ import annotations

import importlib

import pytest

pytestmark = pytest.mark.unit

CHAIN_MODEL_MODULE = (
    "omnibase_core.models.contracts.subcontracts.model_chain_scenario_subcontract"
)


def test_chain_scenario_model_imports_in_the_locked_environment() -> None:
    """Red while the lock names a core tag below omnibase_core#2269; green once core and infra moved together."""
    module = importlib.import_module(CHAIN_MODEL_MODULE)
    assert module.__name__ == CHAIN_MODEL_MODULE
