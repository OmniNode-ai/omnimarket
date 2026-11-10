# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22212: a refused secret or exfiltration request is terminal.

The quality gate's refusal-phrase rule vetoes a refusal as a weak answer, and
a weak answer climbs the ladder. For a request that asks for secrets or asks
to move data out, the refusal is the right answer, and climbing only sends the
request to a cloud rung. The request markers, the refusal verdict prefix and
the typed stop reason are declared once, in ``contract.yaml``.
"""

from __future__ import annotations

from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

_DEFAULT_CONTRACT_PATH = (
    Path(__file__).resolve().parents[2]
    / "nodes"
    / "node_delegate_skill_orchestrator"
    / "contract.yaml"
)
_KEY = "secret_request_refusal"


class ModelSecretRequestRefusal(BaseModel):
    """Contract-declared terms for ending a refused secret request on its rung."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_markers: tuple[str, ...] = Field(min_length=1)
    refusal_verdict_prefix: str = Field(min_length=1)
    terminal_failure_reason: str = Field(min_length=1)

    def is_secret_request(self, prompt: str) -> bool:
        """Whether ``prompt`` asks for a secret or to move data out."""
        lowered = prompt.lower()
        return any(marker.lower() in lowered for marker in self.request_markers)

    def refuses(self, prompt: str, failure_reasons: Iterable[str]) -> bool:
        """Whether the gate vetoed a refusal of a secret request."""
        return self.is_secret_request(prompt) and any(
            reason.startswith(self.refusal_verdict_prefix) for reason in failure_reasons
        )


@lru_cache(maxsize=4)
def load_secret_request_refusal(
    contract_path: Path = _DEFAULT_CONTRACT_PATH,
) -> ModelSecretRequestRefusal:
    """Load the declared terms, refusing a contract that declares none."""
    raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{contract_path} must contain a mapping")
    declared = raw.get(_KEY)
    if not isinstance(declared, dict):
        raise ValueError(f"{contract_path} missing {_KEY} mapping")
    return ModelSecretRequestRefusal.model_validate(declared)


__all__ = ["ModelSecretRequestRefusal", "load_secret_request_refusal"]
