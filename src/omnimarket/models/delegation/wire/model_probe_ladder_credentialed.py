# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The probe-ladder wire shape whose rungs also declare their credential state.

``ModelProbeRung`` is pinned by an omniclaude consumer that forbids extra keys,
so gaining a ``credential`` field there is a wire-incompatible change. That
field lives here instead, on subclasses of the released models: the base shapes
stay byte-for-byte what the pinned consumer expects, and producers that need
the credential state validate against this one (OMN-23511).
"""

from __future__ import annotations

from pydantic import Field

from omnimarket.enums.enum_probe_rung_credential import EnumProbeRungCredential
from omnimarket.models.delegation.wire.model_probe_ladder import (
    ModelProbeLadder,
    ModelProbeRung,
)


class ModelProbeRungCredentialed(ModelProbeRung):
    """One rung of a probe ladder that also declares its credential state."""

    credential: EnumProbeRungCredential = Field(
        default=EnumProbeRungCredential.NOT_REQUIRED,
        description=(
            "The state the rung declares for its provider credential. A rung "
            "that declares it absent has no value for it: the port never "
            "attempts that rung -- no provider call, the same skip production "
            "takes (OMN-23170) -- and the run moves on to the next rung."
        ),
    )


class ModelProbeLadderCredentialed(ModelProbeLadder):
    """The rungs one synthetic probe run walks, first to last, credentialed."""

    rungs: tuple[ModelProbeRungCredentialed, ...] = Field(..., min_length=1)


__all__ = [
    "ModelProbeLadderCredentialed",
    "ModelProbeRungCredentialed",
]
