"""Lane vocabulary for the lab lane-health projection (OMN-18769).

The vocabulary is deliberately the SAME three values the lab-pass receipt
emitter already uses (``omnibase_infra`` ``scripts/ci/lab_pass_receipt.py``
``EnumLabLane``). A second, divergent lane vocabulary is how a receipt for
``compose-dev`` fails to join the census fact for the lane the census calls
``dev``: the join key has to be one word, chosen once.

Scope is the LAB, and nothing else. The ``.201`` stability-test, judge and
collaborator lanes are read-only surfaces under ``omni_home`` operating rule 12
and this projection neither reads nor keys them -- a census finding naming one
of them is dropped at the seam rather than materialized into a row a dashboard
would then invite someone to act on (OMN-18769 AC6).
"""

from __future__ import annotations

from enum import StrEnum


class EnumLabLane(StrEnum):
    """The lab lanes this projection is allowed to hold a row for."""

    #: The ``.201`` dev lane, compose project ``omnibase-infra``, ports
    #: 8085/8086. The lane census calls this lane ``dev``; the receipt emitter
    #: calls it ``compose-dev``. This enum's value is the receipt emitter's,
    #: and :func:`normalize_lane` maps the census spelling onto it.
    COMPOSE_DEV = "compose-dev"
    #: The ``k8s/onex-lab`` overlay applied to a per-candidate cluster.
    ONEX_LAB = "onex-lab"
    #: The persistent k3s lab lane (OMN-18200). A separate lane, not a second
    #: emitter on ``onex-lab``: it proves a different claim.
    ONEX_LAB_K3S = "onex-lab-k3s"


#: Spellings this projection accepts for a lab lane, mapped onto the canonical
#: value. Anything absent from this map is OUT OF SCOPE and is dropped, which
#: is the mechanism behind AC6: ``stability-test``, ``judge``, ``lakshman`` and
#: the retired ``prod`` compose project are simply not keys here, so no fact
#: naming one of them can ever reach a row.
_LANE_ALIASES: dict[str, EnumLabLane] = {
    "compose-dev": EnumLabLane.COMPOSE_DEV,
    "dev": EnumLabLane.COMPOSE_DEV,
    "omnibase-infra": EnumLabLane.COMPOSE_DEV,
    "onex-lab": EnumLabLane.ONEX_LAB,
    "onex-lab-k3s": EnumLabLane.ONEX_LAB_K3S,
}


def normalize_lane(raw: str | None) -> EnumLabLane | None:
    """Resolve a lane spelling to a canonical lab lane, or ``None``.

    ``None`` means "not a lab lane this projection holds", and the caller drops
    the fact. It never means "probably the dev lane": a fact whose lane cannot
    be named is a fact that cannot be keyed, and guessing a key is how another
    lane's drift lands on the dev lane's row.
    """
    if raw is None:
        return None
    return _LANE_ALIASES.get(raw.strip().lower())
