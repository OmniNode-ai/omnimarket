# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15504: the handler's own wall-clock bound, declared in the contract.

``HandlerDelegateSkill`` runs inside an auto-wired consumer whose poll loop is
serial: the loop awaits each dispatch before requesting the next record. A
handler that outruns the consumer's ``max_poll_interval_ms`` is therefore not
merely slow — aiokafka evicts the consumer mid-handle, the subsequent commit is
refused with ``UnknownMemberIdError``, the offset never advances, and the same
records are redelivered on rejoin. That cycle does not self-heal: it is a
livelock, and a container restart does not clear it because the committed
offset lives in the broker.

The bound therefore belongs to the handler, not to any one dispatch port. The
runtime port has a wait of its own; the local in-process port has none at all,
so a bound that lived in a port would hold on one transport and not the other.

The value is declared in ``contract.yaml`` rather than hardcoded, and the
loader enforces the one relationship that matters: it must be **strictly
less** than the runtime port's own wait. Equality is what the live defect was
made of — ``wait_timeout_seconds: 300`` against a ``max_poll_interval_ms``
default of ``300000``, a handler whose worst case landed on the eviction
deadline exactly.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

_DEFAULT_CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"
_BUDGET_KEY = "handler_execution_budget"
_PORT_CONFIG_KEY = "delegation_runtime_dispatch"
_PORT_WAIT_KEY = "wait_timeout_seconds"


class ModelDelegateSkillHandlerBudget(BaseModel):
    """Contract-derived wall-clock bound for one ``handle()`` invocation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_handler_duration_seconds: int = Field(ge=1, le=3600)


def load_handler_execution_budget(
    contract_path: Path = _DEFAULT_CONTRACT_PATH,
) -> ModelDelegateSkillHandlerBudget:
    """Load the handler budget from contract.yaml, refusing an inert one.

    Raises ``ValueError`` when the declaration is missing, when the runtime
    port's wait cannot be read, or when the budget does not strictly pre-empt
    that wait. Each of those is a bound that would not bind, and a bound that
    does not bind reads downstream exactly like one that does.
    """
    raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{contract_path} must contain a mapping")

    declared = raw.get(_BUDGET_KEY)
    if not isinstance(declared, dict):
        raise ValueError(f"{contract_path} missing {_BUDGET_KEY} mapping")

    budget = ModelDelegateSkillHandlerBudget.model_validate(declared)

    port_config = raw.get(_PORT_CONFIG_KEY)
    if not isinstance(port_config, dict) or _PORT_WAIT_KEY not in port_config:
        raise ValueError(
            f"{contract_path} missing {_PORT_CONFIG_KEY}.{_PORT_WAIT_KEY}, so the "
            f"{_BUDGET_KEY} margin cannot be verified"
        )
    port_wait = int(port_config[_PORT_WAIT_KEY])

    if budget.max_handler_duration_seconds >= port_wait:
        raise ValueError(
            f"{_BUDGET_KEY}.max_handler_duration_seconds "
            f"({budget.max_handler_duration_seconds}s) must be strictly less than "
            f"{_PORT_CONFIG_KEY}.{_PORT_WAIT_KEY} ({port_wait}s); a handler bound "
            "that does not pre-empt the port's own wait never fires, which is "
            "how the delegate-skill consumer livelocked (OMN-15504)"
        )

    return budget


__all__ = [
    "ModelDelegateSkillHandlerBudget",
    "load_handler_execution_budget",
]
