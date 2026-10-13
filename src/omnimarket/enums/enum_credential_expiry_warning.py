# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Why a registered provider key's expiry date was flagged (OMN-20991)."""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class EnumCredentialExpiryWarning(StrEnum):
    """A warning registration returns beside the reference, never a refusal.

    Every company-paid key expires on January 1 (operator ruling
    2026-10-11T16:02:35Z), so one date covers the whole pool and one alert
    covers it before it lapses. A key registered with no expiry, or with any
    other date, is still stored: the provider decides when a key stops working,
    and refusing a valid key would only push it around the script path. The
    warning makes the mismatch visible at the moment someone can fix it.
    """

    EXPIRY_ABSENT = "EXPIRY_ABSENT"
    """No expiry was submitted, so nothing can alert before the key lapses."""

    EXPIRY_NOT_JANUARY_1 = "EXPIRY_NOT_JANUARY_1"
    """The submitted expiry is a date other than January 1 (UTC)."""


__all__: list[str] = ["EnumCredentialExpiryWarning"]
