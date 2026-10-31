# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read one config block of a node's contract.yaml into a frozen model (OMN-21623).

A node that used to hold a timeout, a search path or a remote in code declares the
value in a named block of its own contract and receives the validated model at
construction. A string value may be a ``${env.VAR}`` / ``${env.VAR:default}``
overlay reference, expanded through the sanctioned overlay boundary before
validation, so a host's own overlay sets a deployment fact while the shipped value
stays in the contract.

A missing block, a missing key, an unknown key or a malformed value raises
:class:`ContractBlockError`; there is no fallback value, because a default in the
loader would be the same hard-coded value one layer down.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from omnibase_infra.runtime.overlay.contract_env_ref import expand_contract_env_refs
from pydantic import BaseModel, ValidationError


class ContractBlockError(ValueError):
    """A contract's config block is missing or malformed."""


def _expand(value: object) -> object:
    if isinstance(value, str):
        return expand_contract_env_refs(value)
    if isinstance(value, list):
        return [_expand(item) for item in value]
    return value


def load_contract_block[ModelT: BaseModel](
    contract_path: Path, key: str, model: type[ModelT]
) -> ModelT:
    """Read the ``key`` mapping of ``contract_path`` and validate it as ``model``."""
    document = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    block = document.get(key) if isinstance(document, dict) else None
    if not isinstance(block, dict):
        raise ContractBlockError(f"{contract_path} has no {key!r} mapping")
    try:
        return model.model_validate({k: _expand(v) for k, v in block.items()})
    except ValidationError as exc:
        raise ContractBlockError(f"{contract_path} {key}: {exc}") from exc


__all__ = ["ContractBlockError", "load_contract_block"]
