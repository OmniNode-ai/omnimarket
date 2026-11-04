# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure typed argv builder and JSON request validation for ledger rows."""

from __future__ import annotations

import re

from ..models.model_ledger_rows_build import (
    ModelLedgerRowsBuildRequest,
    ModelLedgerRowsBuildResult,
)

OPERATOR_ONLY_KINDS = ("RULING", "OPERATOR-CONSENT")


class RowError(Exception):
    """A row cannot be built from the supplied arguments."""


def _text_argv(text: str | None) -> list[str]:
    """``--text VALUE`` as one argv entry -- never shell-joined, so a quote, a pipe substitute or
    a newline inside ``text`` reaches ``onex-ledger`` exactly as written."""
    return ["--text", text] if text else []


def build_row_argv(
    kind: str,
    *,
    lane: str,
    ticket: str | None = None,
    tickets: list[str] | None = None,
    actor: str | None = None,
    model: str | None = None,
    text: str | None = None,
    extra: dict[str, str | list[str] | None] | None = None,
) -> list[str]:
    """The ``row KIND ...`` argv this module would run, for a caller that wants to inspect or
    print it (``--print-only``) without a subprocess. Raises :class:`RowError` for an operator-only
    kind or a missing ``lane``."""
    kind = kind.upper()
    if kind in OPERATOR_ONLY_KINDS:
        raise RowError(
            f"a {kind} row goes through /omni:operator-ruling, which keeps the words verbatim "
            "and adds the OUT OF SCOPE floor; this typed builder never writes one"
        )
    if not lane.strip():
        raise RowError("lane is required and may not be blank")
    argv = ["row", kind, "--lane", lane]
    for one in tickets or ([ticket] if ticket else []):
        argv += ["--ticket", one]
    if actor:
        argv += ["--actor", actor]
    if model:
        argv += ["--model", model]
    for name, value in (extra or {}).items():
        if value is None:
            continue
        for one_value in (
            [value] if isinstance(value, str) else value
        ):  # a list repeats the flag
            argv += [f"--{name}", str(one_value)]
    argv += _text_argv(text)
    return argv


def build_request_argv(request: object) -> list[str]:
    """Build a machine row from JSON fields, without skill-header or shell parsing.

    The JSON shape matches ``build_row_argv``. ``extra`` holds ledger flag names
    without leading dashes; a list repeats a flag (notably ``kv`` accounting cells).
    Values stay strings, including numeric flag values. Ledger guards still decide
    whether a well-typed request is an admissible row.
    """
    if not isinstance(request, dict):
        raise RowError("request must be a JSON object")
    fields = {"kind", "lane", "ticket", "tickets", "actor", "model", "text", "extra"}
    if set(request) - fields:
        raise RowError(
            "unknown request fields: " + ", ".join(sorted(set(request) - fields))
        )
    for name in ("kind", "lane"):
        if not isinstance(request.get(name), str):
            raise RowError(f"{name} must be a string")
    if request["kind"].upper() not in ("CLAIM", "STATUS", "TERMINAL", "FRICTION"):
        raise RowError("request kind must be CLAIM, STATUS, TERMINAL or FRICTION")
    for name in ("ticket", "actor", "model", "text"):
        if request.get(name) is not None and not isinstance(request[name], str):
            raise RowError(f"{name} must be a string or null")
    tickets = request.get("tickets")
    if tickets is not None and (
        not isinstance(tickets, list)
        or not all(isinstance(value, str) for value in tickets)
    ):
        raise RowError("tickets must be a list of strings or null")
    if request.get("ticket") and tickets:
        raise RowError("pass ticket or tickets, not both")
    extra = request.get("extra")
    if extra is not None:
        if not isinstance(extra, dict):
            raise RowError("extra must be an object or null")
        for name, value in extra.items():
            if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9-]*", name):
                raise RowError(
                    "extra keys must be ledger flag names without leading dashes"
                )
            if (
                value is not None
                and not isinstance(value, str)
                and not (
                    isinstance(value, list)
                    and all(isinstance(item, str) for item in value)
                )
            ):
                raise RowError(
                    f"extra.{name} must be a string, list of strings or null"
                )
    return build_row_argv(**request)


class HandlerLedgerRowsBuild:
    """Stateless argv construction; the caller owns every effect."""

    def handle(
        self, request: ModelLedgerRowsBuildRequest
    ) -> ModelLedgerRowsBuildResult:
        try:
            if request.request_mode == "json" or request.request_data is not None:
                argv = build_request_argv(request.request_data)
            else:
                argv = build_row_argv(
                    request.kind,
                    lane=request.lane,
                    ticket=request.ticket,
                    tickets=request.tickets,
                    actor=request.actor,
                    model=request.model,
                    text=request.text,
                    extra=request.extra,
                )
        except RowError as exc:
            return ModelLedgerRowsBuildResult(
                operation=request.operation, admitted=False, refusal=str(exc)
            )
        return ModelLedgerRowsBuildResult(
            operation=request.operation, admitted=True, argv=argv
        )
