# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Per-tier confirmation-strategy binding, with fail-fast (OMN-15861).

The durability tier already declared per topic in ``registries/topics.yaml``
decides how strong a durability proof that traffic needs:

* ``DUTY_CRITICAL`` -> a readback (or projection) confirmation. Nothing weaker
  is acceptable, because these records are commands and terminal evidence whose
  loss is unrecoverable.
* ``TELEMETRY`` -> publish-return is fine. Bounded loss is correct by design for
  high-volume metrics, and a readback round trip per telemetry event is a real
  cost the platform already flags (``composition.py`` keeps ``/ready`` cheap for
  exactly this reason).

The binding **fails fast at construction time**, not at publish time. A
duty-critical tier wired to ``PublishReturnOnlyStrategy`` is precisely the
invariant-7 violation this ticket removes; discovering it at runtime means the
daemon has already been acking unconfirmed records in production. This mirrors
the existing fail-fast in ``event_registry.py``, which refuses to load a fan-out
rule with no declared tier rather than defaulting one.
"""

from __future__ import annotations

from omnibase_infra.event_bus.confirmation import (
    STRATEGY_NAME_PUBLISH_RETURN_ONLY,
    PublishReturnOnlyStrategy,
)
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
    telemetry: ProtocolConfirmationStrategy | None = None,
) -> dict[EnumDurabilityTier, ProtocolConfirmationStrategy]:
    """Bind one confirmation strategy per durability tier.

    Args:
        duty_critical: Strategy for duty-critical traffic. MUST NOT be
            ``PublishReturnOnlyStrategy``.
        telemetry: Strategy for telemetry traffic. Defaults to
            ``PublishReturnOnlyStrategy`` -- the cheap path, explicitly named.

    Returns:
        A tier -> strategy mapping covering every member of
        ``EnumDurabilityTier``. Exhaustive on purpose: a missing tier would have
        to be defaulted at publish time, and a defaulted durability policy is
        how the unexamined weak path gets reintroduced.

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

    bindings: dict[EnumDurabilityTier, ProtocolConfirmationStrategy] = {
        EnumDurabilityTier.DUTY_CRITICAL: duty_critical,
        EnumDurabilityTier.TELEMETRY: telemetry or PublishReturnOnlyStrategy(),
    }

    missing = set(EnumDurabilityTier) - set(bindings)
    if missing:
        raise DurabilityPolicyError(
            f"no confirmation strategy bound for tier(s): "
            f"{sorted(t.value for t in missing)}"
        )
    return bindings


__all__: list[str] = [
    "DurabilityPolicyError",
    "build_confirmation_bindings",
]
