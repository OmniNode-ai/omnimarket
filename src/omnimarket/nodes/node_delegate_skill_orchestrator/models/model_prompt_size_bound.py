# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21325: the declared size bound on a delegate-skill prompt.

A 300027-byte summarization prompt reached a model and completed because the
only size check (the per-backend grounding budget) applies when the chosen
backend declares one. This bound is declared once, in ``contract.yaml``, and
enforced in ``HandlerDelegateSkill``, which the in-process port and the
deployed consumer both run, so both lanes refuse the same request.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

_DEFAULT_CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"
_BOUND_KEY = "prompt_size_bound"


class ModelPromptSizeBound(BaseModel):
    """Contract-declared maximum prompt size, in UTF-8 bytes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_prompt_bytes: int = Field(strict=True, ge=1)


def load_prompt_size_bound(
    contract_path: Path = _DEFAULT_CONTRACT_PATH,
) -> ModelPromptSizeBound:
    """Load the prompt size bound, refusing a contract that declares none."""
    raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{contract_path} must contain a mapping")
    declared = raw.get(_BOUND_KEY)
    if not isinstance(declared, dict):
        raise ValueError(f"{contract_path} missing {_BOUND_KEY} mapping")
    return ModelPromptSizeBound.model_validate(declared)


__all__ = ["ModelPromptSizeBound", "load_prompt_size_bound"]
