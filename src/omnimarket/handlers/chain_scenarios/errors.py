# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The assertion error a scenario run raises, with a stable code per failed check."""

from __future__ import annotations

from enum import StrEnum


class EnumChainAssertionCode(StrEnum):
    """Which check of a scenario failed; recorded in the kept mutation receipts."""

    SILENCE = "SILENCE"
    EVENT_ORDER = "EVENT_ORDER"
    EVENT_FIELDS = "EVENT_FIELDS"
    CORRELATION = "CORRELATION"
    GOLDEN_ENDS_IN_FAILURE = "GOLDEN_ENDS_IN_FAILURE"
    FAILURE_EVENT = "FAILURE_EVENT"
    LEDGER_ROW = "LEDGER_ROW"
    READING = "READING"
    STATE_UPDATE = "STATE_UPDATE"
    BARE_RETRY = "BARE_RETRY"
    HANDLER_ERROR = "HANDLER_ERROR"


class ChainScenarioAssertionError(AssertionError):
    """A scenario expectation did not hold."""

    def __init__(self, code: EnumChainAssertionCode, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code


__all__ = ["ChainScenarioAssertionError", "EnumChainAssertionCode"]
