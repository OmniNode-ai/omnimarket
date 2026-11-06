# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read a node's refusal of a request field as "the node is older than this client".

A client that adds a field to a node's request model before the deployed node
carries it gets every request refused by the node's own validation: the terminal
reads ``status=failed`` with pydantic's ``extra_forbidden`` text. That text names
the field and nothing about the cause, so callers read it as a node fault or as
an unavailable node. This module reads the refused field names out of that text
and says the cause in one sentence. Any other failure is not reported as skew.
"""

from __future__ import annotations

import re
from typing import Final

from pydantic import JsonValue

_VALIDATION_CLASS: Final[str] = "ValidationError"
# pydantic prints "<field>\n  Extra inputs are not permitted [type=extra_forbidden".
_REFUSED_FIELD: Final[re.Pattern[str]] = re.compile(
    r"^(?P<field>[A-Za-z_][\w.]*)\n[ \t]+Extra inputs are not permitted "
    r"\[type=extra_forbidden",
    re.MULTILINE,
)


class GatewayReadError(RuntimeError):
    """The gateway gave no typed result for a request."""


class GatewayNodeOlderError(GatewayReadError):
    """The deployed gateway refused a request field this client sends."""


def refused_fields(payload: dict[str, JsonValue]) -> tuple[str, ...]:
    """The request fields the node's validation refused as unknown, in order.

    Empty unless the failure is a validation error whose every reported error is
    an ``extra_forbidden`` one: a mixed failure has another cause and is not skew.
    """
    if payload.get("failure_class") != _VALIDATION_CLASS:
        return ()
    reason = payload.get("failure_reason")
    if not isinstance(reason, str):
        return ()
    count = re.match(r"(?P<n>\d+) validation errors? for ", reason)
    fields = tuple(dict.fromkeys(m["field"] for m in _REFUSED_FIELD.finditer(reason)))
    if count is None or int(count["n"]) != len(_REFUSED_FIELD.findall(reason)):
        return ()
    return fields


def plain_sentence(what: str, fields: tuple[str, ...]) -> str:
    """One sentence for the operator: cause, the field names and the fix."""
    names = ", ".join(fields)
    return (
        f"{what}: the deployed node does not know the request field(s) {names} "
        "that this client sends, so the node is older than this client. "
        "Redeploy the node at a dev-merge tag that carries the field; "
        "retrying cannot change the answer."
    )


def failure_error(operation: str, payload: dict[str, JsonValue]) -> GatewayReadError:
    """The error for a failed terminal: skew when the node refused a field, else plain."""
    if skew := refused_fields(payload):
        return GatewayNodeOlderError(plain_sentence(f"gateway {operation}", skew))
    reason = str(payload.get("failure_reason") or "no reason")
    return GatewayReadError(f"gateway {operation} failed: {reason[:300]}")


__all__: list[str] = [
    "GatewayNodeOlderError",
    "GatewayReadError",
    "failure_error",
    "plain_sentence",
    "refused_fields",
]
