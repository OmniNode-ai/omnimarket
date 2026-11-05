# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-22403: a criterion may not be bound to a local_done_gate evidence item.

The hosted receipt gate runs a local_done_gate item neither at the head nor at
the merge base, so it has no control and refuses a bound one. An empty binds_ac
binds nothing and is the dev convention.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

SCOPED_ITEM_REFUSAL = (
    "a local_done_gate item may not carry binds_ac; "
    "scoped local_done_gate, which this hosted check runs neither at "
    "the head nor at the merge base"
)


def scoped_item_binding_violation(item: Mapping[str, Any]) -> str | None:
    """Return the refusal text when item is a local_done_gate item with a non-empty binds_ac."""
    if item.get("execution_scope") == "local_done_gate" and item.get("binds_ac"):
        return SCOPED_ITEM_REFUSAL
    return None
