# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Settings the node reads from its own contract file, never from code."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"


def load_database_dsn_env(contract_path: Path = CONTRACT_PATH) -> str:
    """The environment variable name the contract's `tenant_identity.database.dsn_env` declares."""
    raw: Any = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    name = raw["tenant_identity"]["database"]["dsn_env"]
    if not isinstance(name, str) or not name:
        raise ValueError("tenant_identity.database.dsn_env must be a non-empty string")
    return name


__all__: list[str] = ["CONTRACT_PATH", "load_database_dsn_env"]
