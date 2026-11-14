# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read one host command's output into typed clone findings and a host outcome (OMN-24198).

Both host commands (the dirty-converge tick and its ``--observe`` form) print one line per clone:

    CONVERGED    <clone> refused 150s: <reason> [alarm: delivered]     (act: the tick's own line)
    WOULD_MOVE   <clone>: <reason>                                      (observe)

Lines that match no known status (the sync engine's own output) are not findings. A host whose command
exited non-zero with no clone named, or whose observe command named no clone at all, is a FAILED host:
output nothing could be read from is never read as clean.
"""

from __future__ import annotations

import re

from omnimarket.models.model_convergence_orchestrator import (
    REFUSAL_STATUSES,
    EnumConvergenceCloneStatus,
    EnumConvergenceHostOutcome,
    EnumConvergenceMode,
    ModelConvergenceCloneFinding,
)

_LINE = re.compile(
    r"^(?P<status>[A-Z_]+)\s+(?P<clone>\S+?)(?: refused \d+s)?: ?(?P<reason>.*?)"
    r"(?: \[alarm: [^\]]*\])?\s*$"
)
_STATUS = {status.value.upper(): status for status in EnumConvergenceCloneStatus}


def parse_findings(output: str) -> tuple[ModelConvergenceCloneFinding, ...]:
    """Every clone line of the output, in order; other lines are skipped."""
    findings: list[ModelConvergenceCloneFinding] = []
    for line in output.splitlines():
        match = _LINE.match(line)
        if match is None or match["status"] not in _STATUS:
            continue
        findings.append(
            ModelConvergenceCloneFinding(
                clone=match["clone"],
                status=_STATUS[match["status"]],
                reason=match["reason"].strip()[:2000],
            )
        )
    return tuple(findings)


def host_outcome(
    mode: EnumConvergenceMode,
    exit_code: int | None,
    findings: tuple[ModelConvergenceCloneFinding, ...],
) -> tuple[EnumConvergenceHostOutcome, str]:
    """The outcome of a command that ran to an exit code, and a detail line when it is not a clean read."""
    statuses = {f.status for f in findings}
    if statuses & REFUSAL_STATUSES:
        return EnumConvergenceHostOutcome.REFUSED, ""
    if EnumConvergenceCloneStatus.FAILED in statuses:
        return (
            EnumConvergenceHostOutcome.FAILED,
            "a clone could not be read or converged",
        )
    if exit_code != 0:
        return (
            EnumConvergenceHostOutcome.FAILED,
            f"the command exited {exit_code} with no clone named",
        )
    if mode is EnumConvergenceMode.OBSERVE:
        if not findings:
            return (
                EnumConvergenceHostOutcome.FAILED,
                "the observe command named no clone: nothing was read",
            )
        if EnumConvergenceCloneStatus.WOULD_MOVE in statuses:
            return EnumConvergenceHostOutcome.OBSERVED_PENDING, ""
        return EnumConvergenceHostOutcome.CLEAN, ""
    if EnumConvergenceCloneStatus.CONVERGED in statuses:
        return EnumConvergenceHostOutcome.CONVERGED, ""
    return EnumConvergenceHostOutcome.CLEAN, ""


__all__: list[str] = ["host_outcome", "parse_findings"]
