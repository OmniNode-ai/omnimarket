# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the occ-autobind outcome marker line as a typed companion outcome (OMN-19827).

``occ_autobind_outcome`` posts every consumed autobind command's disposition as
a check-run on the product PR head whose first summary line is::

    occ-autobind-outcome: <KIND> repo=<owner/name> pr=<n> correlation_id=<id> reason=<text>

This module is the reader beside that writer. It maps one marker line, plus the
head SHA of the check-run it was read from, onto exactly one
:class:`~omnimarket.events.pr_landing_companion.ModelPrLandingCompanionOutcome`,
so the PR landing workflow consumes one typed outcome whether it came from this
surface or from the bus, where the producer publishes it since wave-2 task T10
(OMN-19832).

Pure and deterministic: a string in, a model out, no I/O. A line that is not a
marker raises ``ValueError``; it is never guessed at.

What the reason text does and does not tell the reader
--------------------------------------------------------
* ``kind`` is the marker's own word, except that ``NOOP`` maps to the existing
  typed ``DECLINED`` with ``ALREADY_BOUND`` or ``STAMP_REBOUND`` (OMN-18939).
* ``occ_pr`` is read only where the producer's reason names the companion
  (authored, already bound, stamp rebound).
* ``stamped`` is ``True`` where the reason proves the product body names the
  companion (a verified MINTED, already bound, stamp rebound) and ``None``
  otherwise. ``armed`` and ``conflicting`` are never on the marker line and stay
  ``None``.

Measured on the 2026-09-26 corpus (``tests/fixtures/pr_landing/companion_outcome``):
no line said MINTED. Every successful mint was posted as DECLINED with the
suffix ``OCC companion NOT verified: no OCC companion verifier wired``, which
this reader classifies as ``AUTHORED_UNVERIFIED`` and still names the companion.
Since OMN-18939, a mint prints MINTED even without stamp verification; the
unverified suffix keeps its ``stamped`` field unknown, as on the bus outcome.
"""

from __future__ import annotations

import re
from uuid import UUID

from omnimarket.events.pr_landing_companion import (
    EnumPrLandingCompanionDeclineCode,
    EnumPrLandingCompanionOp,
    EnumPrLandingCompanionOutcomeKind,
    ModelPrLandingCompanionOutcome,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_autobind_outcome import (
    OUTCOME_MARKER_PREFIX,
)

_MARKER_RE = re.compile(
    rf"^{re.escape(OUTCOME_MARKER_PREFIX)} "
    r"(?P<kind>MINTED|NOOP|DECLINED|ERROR) "
    r"repo=(?P<repo>\S+) "
    r"pr=(?P<pr>\d+) "
    r"correlation_id=(?P<cid>\S+) "
    r"reason=(?P<reason>.+)$"
)

# The fix handler appends this to the adapter's own reason when its read-back
# verifier does not confirm the companion (handler_pr_lifecycle_fix.handle).
_UNVERIFIED_SUFFIX = " | OCC companion NOT verified:"

_AUTHORED_RE = re.compile(r"^authored OCC companion Evidence-Source: OCC#(\d+)\b")
_ALREADY_BOUND_RE = re.compile(
    r"^no-op: \S+ (?:already bound to|already carries exactly one stamp "
    r"naming the proven companion) OCC#(\d+)\b"
)
_REBOUND_RE = re.compile(
    r"^rebound evidence-source stamp on \S+ to the proven companion OCC#(\d+)\b"
)
_SKIP_RE = re.compile(r"^skip:([A-Z_]+)\b")
_DRY_RUN_PREFIX = "[dry-run]"


def primary_reason(reason: str) -> str:
    """The producer's own reason, without the handler's unverified suffix."""
    return reason.split(_UNVERIFIED_SUFFIX, 1)[0]


def authored_companion(primary: str) -> int | None:
    """The companion an ``authored OCC companion`` reason names, else None."""
    match = _AUTHORED_RE.match(primary)
    return int(match.group(1)) if match else None


def classify_companion_decline(
    primary: str,
) -> tuple[EnumPrLandingCompanionDeclineCode, int | None, bool | None]:
    """Return (decline code, companion named by the reason, stamp observed).

    Shared by this reader and by the typed bus outcome the producer publishes
    (``companion_outcome``, OMN-19832), so both surfaces classify one producer
    reason identically.
    """
    if match := _AUTHORED_RE.match(primary):
        return (
            EnumPrLandingCompanionDeclineCode.AUTHORED_UNVERIFIED,
            int(match.group(1)),
            None,
        )
    if match := _ALREADY_BOUND_RE.match(primary):
        return (
            EnumPrLandingCompanionDeclineCode.ALREADY_BOUND,
            int(match.group(1)),
            True,
        )
    if match := _REBOUND_RE.match(primary):
        return (
            EnumPrLandingCompanionDeclineCode.STAMP_REBOUND,
            int(match.group(1)),
            True,
        )
    if primary.startswith(_DRY_RUN_PREFIX):
        return EnumPrLandingCompanionDeclineCode.DRY_RUN, None, None
    if match := _SKIP_RE.match(primary):
        code = match.group(1)
        if code in EnumPrLandingCompanionDeclineCode.__members__:
            return EnumPrLandingCompanionDeclineCode(code), None, None
    return EnumPrLandingCompanionDeclineCode.UNCLASSIFIED, None, None


def companion_outcome_from_autobind_marker(
    marker_line: str,
    *,
    head_sha: str,
    op: EnumPrLandingCompanionOp = EnumPrLandingCompanionOp.DERIVE,
) -> ModelPrLandingCompanionOutcome:
    """Map one occ-autobind outcome marker line onto exactly one typed outcome.

    Args:
        marker_line: The first line of the ``occ-autobind / outcome`` check-run
            summary.
        head_sha: The head SHA of the check-run the line was read from. The
            marker line does not carry it; the check-run is bound to it.
        op: The operation the command carried. Every marker line written before
            the ``op`` field existed answers a ``derive``.

    Raises:
        ValueError: the line is not an occ-autobind outcome marker.
    """
    match = _MARKER_RE.match(marker_line.strip())
    if match is None:
        raise ValueError(
            f"not an occ-autobind outcome marker line: {marker_line[:120]!r}"
        )
    marker_kind = match.group("kind")
    kind = EnumPrLandingCompanionOutcomeKind(
        "DECLINED" if marker_kind == "NOOP" else marker_kind
    )
    cid_raw = match.group("cid")
    correlation_id = None if cid_raw == "unknown" else UUID(cid_raw)
    reason = match.group("reason").strip()
    primary = primary_reason(reason)

    common: dict[str, object] = {
        "kind": kind,
        "op": op,
        "repository": match.group("repo"),
        "pr_number": int(match.group("pr")),
        "head_sha": head_sha,
        "correlation_id": correlation_id,
    }
    if kind is EnumPrLandingCompanionOutcomeKind.ERROR:
        return ModelPrLandingCompanionOutcome.model_validate(
            {**common, "error_reason": reason}
        )
    if kind is EnumPrLandingCompanionOutcomeKind.MINTED:
        authored = _AUTHORED_RE.match(primary)
        return ModelPrLandingCompanionOutcome.model_validate(
            {
                **common,
                "occ_pr": int(authored.group(1)) if authored else None,
                # The headline records authoring, not stamp verification.
                # Legacy verified markers carry no unverified suffix.
                "stamped": None if _UNVERIFIED_SUFFIX in reason else True,
            }
        )
    code, occ_pr, stamped = classify_companion_decline(primary)
    return ModelPrLandingCompanionOutcome.model_validate(
        {
            **common,
            "decline_code": code,
            "decline_reason": reason,
            "occ_pr": occ_pr,
            "stamped": stamped,
        }
    )


__all__ = [
    "authored_companion",
    "classify_companion_decline",
    "companion_outcome_from_autobind_marker",
    "primary_reason",
]
