# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""The ticket-creation gate as the omnibase_core validator, run in process (OMN-22850).

``node_create_ticket`` used to run the installed omniclaude guard script as a subprocess.
This guard calls ``omnibase_core.validation.checker_ticket_creation`` directly with the
rule rows of the core contract, so the gate no longer depends on a plugin being installed
on the host. The refusal keeps the guard's shape: one ``* [<code>] <field>: <reason>`` line
per finding, in order, so ``guard_finding_codes`` reads the codes back from the reason.
"""

from __future__ import annotations

import os
from pathlib import Path

from omnibase_core.validation.checker_rule_parameters import rule_string
from omnibase_core.validation.checker_ticket_creation import (
    check_ticket_creation,
    rows_of,
)
from omnibase_core.validation.validator_ticket_creation import (
    ValidatorTicketCreation,
    ledger_line_lookup,
)

from omnimarket.handlers.handler_lane_intake import ModelLaneIntakePolicy
from omnimarket.nodes.node_create_ticket.handlers.handler_create_ticket import (
    ModelTicketGuardDecision,
)

__all__ = ["CORE_GUARD_ID", "CoreTicketGuard", "core_lane_intake_policy"]

#: The ``guard_path`` a decision of this guard carries: the validator, not a file.
CORE_GUARD_ID = "omnibase_core.validation.checker_ticket_creation"

_LEDGER_PATH_ENV = "ONEX_LEDGER_PATH"


def core_lane_intake_policy() -> ModelLaneIntakePolicy:
    """The lane-intake label and stamp prefix, read from the core contract's ``lane_intake`` row."""
    contract = ValidatorTicketCreation()._load_contract()  # noqa: SLF001
    row = rows_of(contract)["lane_intake"]
    return ModelLaneIntakePolicy(
        label=rule_string(row, "label"), stamp_prefix=rule_string(row, "stamp_prefix")
    )


class CoreTicketGuard:
    """Judges a ``save_issue`` payload with the omnibase_core ticket-creation checker."""

    def __init__(
        self, lane: str | None = None, ledger_path: Path | None = None
    ) -> None:
        self.lane = lane
        if ledger_path is None:
            named = os.environ.get(_LEDGER_PATH_ENV, "").strip()
            ledger_path = Path(named) if named else None
        self.ledger_path = ledger_path

    def check(self, tool_input: dict[str, object]) -> ModelTicketGuardDecision:
        contract = ValidatorTicketCreation()._load_contract()  # noqa: SLF001
        result = check_ticket_creation(
            tool_input,
            contract,
            lane=self.lane,
            ledger_line_exists=ledger_line_lookup(self.ledger_path),
        )
        if not result.findings:
            return ModelTicketGuardDecision(admitted=True, guard_path=CORE_GUARD_ID)
        reason = "\n".join(
            f"  * [{f.code}] {f.field}: {f.reason} ({f.fix})" for f in result.findings
        )
        return ModelTicketGuardDecision(
            admitted=False,
            reason=reason,
            guard_path=CORE_GUARD_ID,
            finding_codes=[f.code for f in result.findings],
        )
