# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""How a synthetic probe rung declares the state of its provider credential.

Production skips a rung whose credential has no value before any provider call
(OMN-23170). A probe ladder is the synthetic twin of a production ladder, so its
rungs need a typed way to declare the same fact (OMN-23511):

``NOT_REQUIRED``
    The rung declares no credential requirement, the default. The stub behind a
    probe rung answers without one, so the port attempts it as before.

``DECLARED_ABSENT``
    The rung's credential has no value. The dispatch port never attempts it --
    no provider call, the same skip production takes -- and the run moves on to
    the next rung.

Shaped like
:class:`omnimarket.models.delegation.local_credential_refusal.EnumLocalCredentialRefusalReason`:
a ``StrEnum`` whose values are the wire spelling a ladder file writes.
"""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class EnumProbeRungCredential(StrEnum):
    """How a probe rung declares the state of its provider credential."""

    NOT_REQUIRED = "not_required"
    DECLARED_ABSENT = "declared_absent"


__all__: list[str] = ["EnumProbeRungCredential"]
