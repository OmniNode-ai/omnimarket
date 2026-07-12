# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fail-closed effect assertions for artifact-producing jobs (RT-5).

Co-located COPY of ``omnibase_infra.utils.util_producer_effect_assertion`` (the
canonical home, landed under OMN-14467). omnimarket pins omnibase_infra to a
frozen git rev, so the canonical util is NOT importable here until infra
publishes a release that market can pin — so this repo carries its own copy.

TODO(OMN-14470): de-dup once omnibase_infra publishes a release market can pin —
delete this module and import from ``omnibase_infra.utils.util_producer_effect_assertion``.
Do NOT pin infra's PR-branch head to close this early (a feature-branch head is
deleted on merge; ref: never_pin_a_feature_branch_head).

The invariant, stated once and shared by every producer (deploy trigger, publish
step, pin cascade, OCC publisher):

    "Ran successfully" is NOT a completion signal for a producer.
    "Produced N>0, and here it is" is.

Emitting zero — whether because a required precondition is missing or because the
emit itself delivered nothing — must FAIL CLOSED (non-zero, red), never skip
green. This module is pure: no I/O, no environment reads, no topic literals.

Ticket: OMN-14470 (RT-5 mirror); sibling OMN-14467; epic OMN-13674.
"""

from __future__ import annotations

from collections.abc import Mapping

__all__ = [
    "ProducerZeroOutputError",
    "assert_producer_emitted",
    "require_producer_preconditions",
]


class ProducerZeroOutputError(RuntimeError):
    """A producer expected to emit >=1 artifact emitted zero.

    Raised by :func:`require_producer_preconditions` (a precondition for emitting
    is missing, so the producer cannot emit) and by :func:`assert_producer_emitted`
    (the emit completed but delivered nothing). Callers convert this into a
    non-zero process exit — a producer that emits nothing must go RED.
    """


def require_producer_preconditions(
    *,
    artifact: str,
    preconditions: Mapping[str, object],
) -> None:
    """Fail closed when a required precondition for emitting ``artifact`` is absent.

    ``preconditions`` maps a human-readable name (e.g. an env var) to its resolved
    value. A value that is falsy (empty string, ``None``, ``0``) means the
    producer cannot emit its artifact — that is zero output, not a reason to skip
    green.

    Raises:
        ProducerZeroOutputError: if any precondition value is falsy. The message
            names every missing precondition and the artifact that would not be
            produced.
    """
    missing = [name for name, value in preconditions.items() if not value]
    if missing:
        raise ProducerZeroOutputError(
            f"producer for {artifact!r} cannot emit: missing required "
            f"precondition(s) {', '.join(missing)}. A producer that emits nothing "
            f"must fail closed (RT-5), not skip green."
        )


def assert_producer_emitted(
    produced_count: int,
    *,
    artifact: str,
    detail: str = "",
) -> None:
    """Fail closed when a producer emitted fewer than one artifact.

    Call this *after* the emit with the number of artifacts actually delivered
    (messages published, tags pushed, PRs opened, receipts written). ``0`` (or a
    negative count) is the silent-producer failure and must go RED.

    Raises:
        ProducerZeroOutputError: if ``produced_count < 1``.
    """
    if produced_count < 1:
        suffix = f" ({detail})" if detail else ""
        raise ProducerZeroOutputError(
            f"producer for {artifact!r} emitted {produced_count} artifact(s); "
            f"expected at least 1{suffix}. 'Ran successfully' is not completion "
            f"for a producer — producing zero must fail closed (RT-5)."
        )
