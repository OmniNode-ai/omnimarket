# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reading the delegation completion bound the platform actually enforces.

The client used to decide on its own how long to wait: a hardcoded 300 second
CLI default, chosen by nobody in particular, with no relationship to how long
the runtime would keep trying. The runtime's own give-up TTL was 900 seconds,
read from an environment variable in a different repository. So a delegation
that the platform was still willing to work on for another ten minutes was
abandoned by its caller at five, and — worse — a delegation the platform had
quietly stopped working on looked, from the client, exactly the same.

``node_delegation_orchestrator``'s contract now declares that bound once
(``completion_bound.max_wall_seconds``, OMN-18296). The runtime enforces it by
emitting a real terminal event for any workflow that exceeds it. This module is
the client's half: it reads the same declaration, so ``onex cloud delegate``
stops asking at the moment the platform stops trying, and the typed error it
raises can say where its patience came from instead of quoting a number of its
own invention.

Read from the contract that ships in this package, not from the gateway: the
contract is the declaration, and a client that asked the server how long to wait
would be trusting the same surface whose silence it is trying to bound.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from omnibase_core.enums import EnumCoreErrorCode
from omnibase_core.models.errors import ModelOnexError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

_CONTRACT_PATH: Final[Path] = (
    Path(__file__).resolve().parent.parent
    / "nodes"
    / "node_delegation_orchestrator"
    / "contract.yaml"
)


class ModelDeclaredCompletionBound(BaseModel):
    """The client-side view of the contract's ``completion_bound`` block.

    Deliberately a subset. The runtime's own reader
    (``omnibase_infra.runtime.state_io.model_completion_bound``) validates the
    whole block including the restart policy and the failure attribution, none
    of which a caller can act on. What the client needs is the number it should
    wait for and the token it should name when it stops; ``extra="ignore"`` lets
    the runtime add fields to the block without breaking every installed CLI.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    max_wall_seconds: int = Field(..., gt=0)
    failure_class: str = Field(..., min_length=1)


def read_declared_completion_bound(
    contract_path: Path | None = None,
) -> ModelDeclaredCompletionBound:
    """Return the delegation completion bound declared in the node contract.

    Raises:
        ModelOnexError: the contract is missing, unreadable, or declares no
            usable bound. Deliberately fatal rather than falling back to a
            built-in number: a silent fallback is how the client came to be
            waiting 300 seconds for a 900 second platform in the first place,
            and a wrong bound that looks authoritative is worse than a refusal
            that names the file it could not read.
    """
    path = contract_path if contract_path is not None else _CONTRACT_PATH
    try:
        import yaml

        raw = yaml.safe_load(path.read_text())
    except FileNotFoundError as exc:
        raise ModelOnexError(
            f"the delegation contract is missing at {path} — the completion "
            "bound this client waits for is declared there and cannot be "
            "guessed. Reinstall the omnimarket package.",
            error_code=EnumCoreErrorCode.INVALID_INPUT,
        ) from exc
    except yaml.YAMLError as exc:
        raise ModelOnexError(
            f"the delegation contract at {path} could not be parsed: {exc}",
            error_code=EnumCoreErrorCode.INVALID_INPUT,
        ) from exc
    block = raw.get("completion_bound") if isinstance(raw, dict) else None
    if not isinstance(block, dict):
        raise ModelOnexError(
            f"the delegation contract at {path} declares no completion_bound "
            "block — this client has no declared bound to wait for and will "
            "not invent one.",
            error_code=EnumCoreErrorCode.INVALID_INPUT,
        )
    try:
        return ModelDeclaredCompletionBound.model_validate(block)
    except ValidationError as exc:
        raise ModelOnexError(
            f"the completion_bound block in {path} is invalid: {exc}",
            error_code=EnumCoreErrorCode.INVALID_INPUT,
        ) from exc


__all__: list[str] = [
    "ModelDeclaredCompletionBound",
    "read_declared_completion_bound",
]
