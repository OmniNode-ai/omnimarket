# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Where a resolved secret VALUE actually came from (OMN-18695)."""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class EnumSecretSource(StrEnum):
    """The surface that answered a secret reference.

    Recorded on the call result and on the receipt because a resolver that
    reads the right place is otherwise unobservable: a run that resolved a
    customer's key from their own store and a run that resolved it from a
    leftover environment variable produce byte-identical output. This enum is
    what makes the difference readable after the fact.

    It names a SOURCE, never a value, and there is deliberately no member for
    "resolved but we do not know from where" -- every resolution path in
    :mod:`omnimarket.inference.secret_store_resolver` reports one of these.
    """

    LOCAL_STORE = "store"
    """This machine's own local secret store -- the customer's own key."""

    LANE_SECRET_STORE = "lane_secret_store"
    """The lane's declared secret mapping (Infisical or a lane-configured source)."""

    ENVIRONMENT = "environment"
    """A process environment variable.

    Forbidden for a provider credential on the local path (OMN-18695) and kept
    as a member precisely so a resolution that still happens this way is
    RECORDED as such rather than silently reported as a store read. Reachable
    today only for non-provider references such as a CI token.
    """


__all__ = ["EnumSecretSource"]
