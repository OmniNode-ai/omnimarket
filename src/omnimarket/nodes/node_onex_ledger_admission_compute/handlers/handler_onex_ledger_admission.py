# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Definition-B decisions of the onex-ledger wrapper (OMN-20686).

The retired onex_ledger.py mixed these decisions with environment reads, file reads and the
subprocess that runs ``onex-ledger``. Here the caller reads those and passes the facts; the handler
returns what the wrapper does, behaviour-preserving: a bad budget is a usage error (64), a TERMINAL row
without its delegation cells is refused (65) with nothing written, no valid omnibase_internal clone is
an error (2), and otherwise the wrapper runs onex-ledger in the selected clone, with the text it prints
if the budget runs out (124).
"""

from __future__ import annotations

import math
import re

from omnimarket.nodes.node_onex_ledger_admission_compute.models.model_onex_ledger_admission import (
    ModelOnexLedgerAdmissionRequest,
    ModelOnexLedgerAdmissionResult,
)

PROJECT_NAME = "omnibase-internal"
PATH_ENV = "OMNIBASE_INTERNAL_PATH"

# The declared reasons a lane may give for delegating nothing (OMN-17427).
DELEGATION_REASONS = (
    "no-text-or-code",
    "route-refused:<status>",
    "route-unavailable:<run id>",
    "read-only-lane",
)
DELEGATION_REASON_RE = re.compile(
    r"no-text-or-code|read-only-lane|route-refused:[A-Za-z0-9._-]+|route-unavailable:[A-Za-z0-9._:-]+"
)
# The runner's own reason when a lane's final text has no valid DELEGATION line. It never closes a
# lane as done: only with outcome=rejected-no-delegation, or failed for a lane that also failed.
REJECTED_OUTCOME = "rejected-no-delegation"
NO_LINE_REASON = "no-delegation-line"
NO_LINE_OUTCOMES = (REJECTED_OUTCOME, "failed")
EXIT_REFUSED = 65
REFUSAL_FIX = (
    "FIX: run the lane's delegation step and pass its cells (delegated=<n> runs=<ids>), or "
    f"delegated=0 delegation_reason=<{'|'.join(DELEGATION_REASONS)}>"
)


def terminal_delegation_refusal(cells: dict[str, str]) -> str | None:
    """Why a TERMINAL's cells do not carry the delegation step, or None when they do."""
    if (
        cells.get("actor", "").startswith(("script:", "launchd:"))
        or cells.get("model") == "none"
    ):
        return (
            None  # a machine writer delegates nothing and is exempt, as in the grammar
        )
    if cells.get("delegation", "").startswith("na:"):
        return None  # the typed not-applicable form; the grammar checks its slug
    raw = cells.get("delegated", "").rstrip(",;.")
    if not raw:
        return (
            "TERMINAL carries no delegated= cell: close with delegated=<n> runs=<ids>, or delegated=0 "
            f"delegation_reason=<one of {', '.join(DELEGATION_REASONS)}>"
        )
    try:
        count = int(raw)
    except ValueError:
        return f"delegated={raw} is not a count"
    if count >= 1:
        if any(
            cells.get(k, "").strip(" ,;.") not in ("", "none")
            for k in ("runs", "codex", "jev")
        ):
            return None
        return f"delegated={count} names no runs= (or codex=/jev=) ids that back it"
    reason = cells.get("delegation_reason", "").strip(" ,;.")
    if reason == NO_LINE_REASON:
        if cells.get("outcome") in NO_LINE_OUTCOMES:
            return None
        return (
            f"delegation_reason={NO_LINE_REASON} closes a lane only with outcome={REJECTED_OUTCOME} "
            f"(or failed), never outcome={cells.get('outcome') or 'none'}"
        )
    if DELEGATION_REASON_RE.fullmatch(reason):
        return None
    return (
        f"delegated=0 needs delegation_reason= from the declared set ({', '.join(DELEGATION_REASONS)}), "
        f"got {reason or 'none'!r}"
    )


def _tokens(cells: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for cell in cells:
        for token in cell.split():
            key, eq, value = token.partition("=")
            if eq and key and key not in out:
                out[key] = value
    return out


def row_argv_cells(argv: list[str]) -> dict[str, str] | None:
    """The cells a ``row TERMINAL`` argv builds, or None when the argv is not one."""
    if len(argv) < 2 or argv[0] != "row" or argv[1] != "TERMINAL":
        return None
    named = {"--actor": "actor", "--model": "model", "--outcome": "outcome"}
    kvs: list[str] = []
    cells: dict[str, str] = {}
    i = 2
    while i < len(argv):
        flag, eq, inline = argv[i].partition("=")
        if flag != "--kv" and flag not in named:
            i += 1
            continue
        value = inline if eq else (argv[i + 1] if i + 1 < len(argv) else "")
        if flag == "--kv":
            kvs.append(value)
        else:
            cells[named[flag]] = value
        i += 1 if eq else 2
    return {**_tokens(kvs), **cells}


def terminal_refusals(argv: list[str], append_rows_text: str | None) -> list[str]:
    """The delegation refusals for the rows this argv would write (OMN-17427)."""
    cells = row_argv_cells(argv)
    if cells is not None:
        why = terminal_delegation_refusal(cells)
        return [why] if why else []
    lines: list[str] = []
    if len(argv) >= 2 and argv[0] == "append-rows":
        if append_rows_text is None:
            return []  # onex-ledger reports the unreadable file itself
        lines = append_rows_text.splitlines()
    elif "--append" in argv[:-1]:
        lines = [
            argv[argv.index("--append") + 1]
        ]  # the legacy ``<ledger> --append "<row>"`` form
    found: list[str] = []
    for line in lines:
        parts = [p.strip() for p in line.split(" | ")]
        if len(parts) > 2 and parts[1] == "TERMINAL":
            why = terminal_delegation_refusal(_tokens(parts[2:]))
            if why:
                found.append(f"{parts[0]} TERMINAL: {why}")
    return found


def _no_project_message(request: ModelOnexLedgerAdmissionRequest) -> str:
    override_display = request.override_resolved if request.path_override else "<unset>"
    if request.default_resolved is None:
        default_display = "<unavailable: OMNI_HOME is unset>"
    elif request.path_override:
        default_display = (
            f"{request.default_resolved} (not selected because {PATH_ENV} is set)"
        )
    else:
        default_display = request.default_resolved
    return (
        "onex-ledger resolver: no valid omnibase_internal clone. "
        f"{PATH_ENV} tried: {override_display}; "
        f"$OMNI_HOME/../omnibase_internal tried: {default_display}. "
        'Expected a directory containing pyproject.toml with [project] name = "omnibase-internal".'
    )


def _budget(raw: str) -> float | None:
    """The positive finite budget in seconds; ValueError when ``raw`` is set but not one."""
    if not raw:
        return None
    budget = float(raw)
    if not math.isfinite(budget) or budget <= 0:
        raise ValueError(raw)
    return budget


class HandlerOnexLedgerAdmission:
    """Stateless compute: what the onex-ledger wrapper does with an argv and the facts read."""

    def handle(
        self, request: ModelOnexLedgerAdmissionRequest
    ) -> ModelOnexLedgerAdmissionResult:
        try:
            budget = _budget(request.budget_raw)
        except ValueError:
            return ModelOnexLedgerAdmissionResult(
                action="usage-error",
                exit_code=64,
                stderr_lines=[
                    "onex-ledger resolver: ONEX_LEDGER_BUDGET_S must be a positive number of seconds, "
                    f"got {request.budget_raw}"
                ],
            )
        refusals = terminal_refusals(request.argv, request.append_rows_text)
        if refusals:
            return ModelOnexLedgerAdmissionResult(
                action="refuse",
                exit_code=EXIT_REFUSED,
                stdout_lines=[
                    *(
                        f"REFUSED {EXIT_REFUSED} delegation-cells: {why}"
                        for why in refusals
                    ),
                    REFUSAL_FIX,
                ],
            )
        selected = (
            request.override_resolved
            if request.path_override
            else request.default_resolved
        )
        if selected is None or request.project_names.get(selected) != PROJECT_NAME:
            return ModelOnexLedgerAdmissionResult(
                action="no-project",
                exit_code=2,
                stderr_lines=[_no_project_message(request)],
            )
        verb = request.argv[0] if request.argv else "?"
        return ModelOnexLedgerAdmissionResult(
            action="run",
            project=selected,
            budget_s=budget,
            timeout_stdout=None
            if budget is None
            else f"REFUSED 124 budget: onex-ledger {verb} exceeded {budget:g}s",
            timeout_stderr=(
                None
                if budget is None
                else f"RETRY 124 budget: onex-ledger {verb} exceeded its {budget:g}s budget and was stopped; "
                "rows it printed OK for landed, the rest did not"
            ),
        )
