# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build the typed companion outcome for one consumed autobind command (OMN-19832).

Wave-2 task T10 of the PR landing workflow (epic OMN-19822). The producer,
``node_pr_lifecycle_fix_effect``, answers every autobind command it consumes
with one :class:`ModelPrLandingCompanionOutcome` on
``onex.evt.omnimarket.pr-landing-companion-outcome.v1``. This module is the pure
half of that: the facts of one finished fix run in, one typed outcome out. No
I/O, no clock.

How a run maps onto a kind
--------------------------
* The run raised: ``ERROR``, with the rendered fault as ``error_reason``.
* The producer authored or re-minted a companion (its reason reads
  ``authored OCC companion Evidence-Source: OCC#<n>``): ``MINTED`` with
  ``occ_pr``. ``stamped`` is ``True`` only when the read-back verifier confirmed
  the companion, and ``None`` otherwise, which is the landing plan's
  "companion.verify if not stamped" case (revision 1, section 3). The check-run
  marker for the same run also reads ``MINTED`` (OMN-18939); both surfaces
  say what the producer did, and leave the unconfirmed stamp as ``None``
  rather than turning a successful mint into a decline that would page an
  agent.
* Every other return is ``DECLINED``, classified by the same function the T5
  marker reader uses. The check headline for ``ALREADY_BOUND`` and
  ``STAMP_REBOUND`` is ``NOOP``; the typed codes and landing decisions stay
  unchanged.

``armed`` and ``conflicting`` are never observed by the producer and stay
``None``.
"""

from __future__ import annotations

from omnimarket.events.pr_landing_companion import (
    EnumPrLandingCompanionOutcomeKind,
    ModelPrLandingCompanionOutcome,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_autobind_outcome_reader import (
    authored_companion,
    classify_companion_decline,
    primary_reason,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_command import (
    ModelPrLifecycleFixCommand,
)


def _flat(text: str) -> str:
    return " ".join(str(text).split()) or "(no reason given)"


def companion_outcome_for_fix_run(
    command: ModelPrLifecycleFixCommand,
    *,
    head_sha: str,
    fix_action: str,
    error: str | None,
    companion_verified: bool,
) -> ModelPrLandingCompanionOutcome:
    """Map one finished autobind fix run onto exactly one typed outcome.

    Args:
        command: The consumed command. Its ``op``, ``command_id`` and
            ``correlation_id`` are echoed onto the outcome.
        head_sha: The product PR head the outcome is bound to.
        fix_action: The run's action string (the producer's reason, plus the
            handler's verifier suffix when the verifier did not confirm).
        error: The rendered fault when the run raised, else ``None``.
        companion_verified: The read-back verifier's verdict.
    """
    common: dict[str, object] = {
        "op": command.op,
        "repository": command.repo,
        "pr_number": command.pr_number,
        "head_sha": head_sha,
        "correlation_id": command.correlation_id,
        "command_id": command.command_id,
    }
    if error is not None:
        return ModelPrLandingCompanionOutcome.model_validate(
            {
                **common,
                "kind": EnumPrLandingCompanionOutcomeKind.ERROR,
                "error_reason": _flat(error),
            }
        )
    primary = primary_reason(fix_action)
    minted = authored_companion(primary)
    if minted is not None:
        return ModelPrLandingCompanionOutcome.model_validate(
            {
                **common,
                "kind": EnumPrLandingCompanionOutcomeKind.MINTED,
                "occ_pr": minted,
                "stamped": True if companion_verified else None,
            }
        )
    code, occ_pr, stamped = classify_companion_decline(primary)
    return ModelPrLandingCompanionOutcome.model_validate(
        {
            **common,
            "kind": EnumPrLandingCompanionOutcomeKind.DECLINED,
            "decline_code": code,
            "decline_reason": _flat(fix_action),
            "occ_pr": occ_pr,
            "stamped": stamped,
        }
    )


__all__ = ["companion_outcome_for_fix_run"]
