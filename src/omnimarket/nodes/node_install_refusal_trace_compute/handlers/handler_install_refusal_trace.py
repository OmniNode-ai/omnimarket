# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Classify the hand-off's refusals of one window by the cause its record states.

The hand-off (handoff_row.sh, install-first check) refuses a PR that changes what runs on a
host unless its proof names the live head installed on a lab host and observed running. Each
refusal prints one ``handoff_row: <repo>#<n> NOT-READY <reason>`` line. This handler reads those
lines and counts them per cause. A cause is named only from the record's own words: a record
that matches no declared class is listed as unclassified, and a declared class no record reached
is listed as unread, never reported as zero.
"""

import re
from dataclasses import dataclass

from omnimarket.nodes.node_install_refusal_trace_compute.models import (
    ModelInstallRefusalTraceRequest,
    ModelInstallRefusalTraceResult,
    ModelRefusalClassCount,
)


@dataclass(frozen=True)
class _CauseClass:
    cause: str
    install_proof: bool
    pattern: re.Pattern[str]


# Order matters: the first class whose pattern matches names the record's cause. The specific
# install-proof reasons come before the general "carries installed=" one.
CAUSE_CLASSES: tuple[_CauseClass, ...] = (
    _CauseClass(
        "installed-revision-unread",
        True,
        re.compile(r"proof is not enough:.*\bUNREAD\b|installed=\S+.*\bUNREAD\b", re.S),
    ),
    _CauseClass(
        "installed-not-live-head",
        True,
        re.compile(
            r"proof is not enough: installed=\S+ is not the live head\b|installed=\S+.*does not contain it"
        ),
    ),
    _CauseClass(
        "observation-names-no-run",
        True,
        re.compile(r"proof is not enough: installed=\S+ names no run"),
    ),
    _CauseClass(
        "no-installed-claim",
        True,
        re.compile(
            r"proof is not enough: no Lab line, lab comment or STATUS row carries installed="
        ),
    ),
    _CauseClass(
        "no-lab-line",
        True,
        re.compile(r"NOT-READY the body has no lab line"),
    ),
    _CauseClass(
        "draft-not-ready",
        False,
        re.compile(r"NOT-READY isDraft=true"),
    ),
    _CauseClass(
        "body-misses-title-ticket",
        False,
        re.compile(r"NOT-READY body cites .* it must include the title ticket"),
    ),
)
# Two printings of the same refusal: the script's own ``handoff_row: <x>#<n> NOT-READY`` line, and the
# lane TERMINAL rows' ``handoff refused for <x>#<n> head=<sha> rc=4: NOT-READY`` wording.
_REFUSAL_LINE = re.compile(
    r"handoff_row:\s+\S+#\d+\s+NOT-READY\b"
    r"|handoff refused for\s+\S+#\d+\b[^\n]*?\bNOT-READY\b"
)


class HandlerInstallRefusalTraceCompute:
    def handle(
        self, request: ModelInstallRefusalTraceRequest
    ) -> ModelInstallRefusalTraceResult:
        by_cause: dict[str, list[str]] = {c.cause: [] for c in CAUSE_CLASSES}
        unclassified: list[str] = []
        for record in request.records:
            if not _REFUSAL_LINE.search(record.text):
                unclassified.append(record.source)
                continue
            for cls in CAUSE_CLASSES:
                if cls.pattern.search(record.text):
                    by_cause[cls.cause].append(record.source)
                    break
            else:
                unclassified.append(record.source)
        proof = {c.cause: c.install_proof for c in CAUSE_CLASSES}
        counts = tuple(
            sorted(
                (
                    ModelRefusalClassCount(
                        cause=cause,
                        install_proof=proof[cause],
                        count=len(sources),
                        sources=tuple(sources),
                    )
                    for cause, sources in by_cause.items()
                    if sources
                ),
                key=lambda c: (-c.count, c.cause),
            )
        )
        return ModelInstallRefusalTraceResult(
            window_start=request.window_start,
            window_end=request.window_end,
            total=len(request.records),
            install_proof_total=sum(c.count for c in counts if c.install_proof),
            classes=counts,
            unread_classes=tuple(
                c.cause for c in CAUSE_CLASSES if not by_cause[c.cause]
            ),
            unclassified=tuple(unclassified),
        )
