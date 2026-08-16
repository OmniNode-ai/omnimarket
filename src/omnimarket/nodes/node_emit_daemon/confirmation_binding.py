# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Per-tier confirmation-strategy binding, with fail-fast (OMN-15861).

The durability tier already declared per topic in ``registries/topics.yaml``
decides how strong a durability proof that traffic needs:

* ``DUTY_CRITICAL`` -> a readback (or projection) confirmation. Nothing weaker
  is acceptable, because these records are commands and terminal evidence whose
  loss is unrecoverable.
* ``TELEMETRY`` -> no confirmation at all. Bounded loss is correct by design for
  high-volume metrics, there is no outbox record to truncate, and a readback
  round trip per telemetry event is a real cost the platform already flags
  (``composition.py`` keeps ``/ready`` cheap for exactly this reason). The
  publisher loop therefore never consults a strategy for that tier, so this
  module does not bind one.

The binding **fails fast at construction time**, not at publish time. A
duty-critical tier wired to ``PublishReturnOnlyStrategy`` is precisely the
invariant-7 violation this ticket removes; discovering it at runtime means the
daemon has already been acking unconfirmed records in production. This mirrors
the existing fail-fast in ``event_registry.py``, which refuses to load a fan-out
rule with no declared tier rather than defaulting one.
"""

from __future__ import annotations

from omnibase_infra.event_bus.confirmation import STRATEGY_NAME_PUBLISH_RETURN_ONLY
from omnibase_infra.protocols.protocol_confirmation_strategy import (
    ProtocolConfirmationStrategy,
)

from omnimarket.nodes.node_emit_daemon.models.model_durability import (
    EnumDurabilityTier,
)


class DurabilityPolicyError(RuntimeError):
    """Raised when a tier is bound to a strategy too weak for it.

    A configuration error, deliberately fatal at wiring time.
    """


def build_confirmation_bindings(
    *,
    duty_critical: ProtocolConfirmationStrategy,
) -> dict[EnumDurabilityTier, ProtocolConfirmationStrategy]:
    """Bind the confirmation strategy for duty-critical traffic.

    Only ``DUTY_CRITICAL`` appears in the returned mapping. ``TELEMETRY`` is
    absent on purpose rather than bound to a weak strategy: the publisher loop
    does not consult a strategy for that tier at all, because bounded loss is
    already its declared-correct behaviour and there is no outbox record to
    truncate. Binding it would imply a durability decision the loop never makes.

    Args:
        duty_critical: Strategy for duty-critical traffic. MUST NOT be
            ``PublishReturnOnlyStrategy``.

    Returns:
        A ``{DUTY_CRITICAL: strategy}`` mapping.

    Raises:
        DurabilityPolicyError: If ``duty_critical`` is publish-return-only.
    """
    if duty_critical.name == STRATEGY_NAME_PUBLISH_RETURN_ONLY:
        raise DurabilityPolicyError(
            "duty_critical traffic cannot be bound to "
            f"'{STRATEGY_NAME_PUBLISH_RETURN_ONLY}': acking a duty-critical "
            "outbox record on the publish return alone is the exact false "
            "durable claim OMN-15861 removes. Bind a readback- or "
            "projection-backed strategy instead."
        )

    return {EnumDurabilityTier.DUTY_CRITICAL: duty_critical}


__all__: list[str] = [
    "DurabilityPolicyError",
    "build_confirmation_bindings",
]
