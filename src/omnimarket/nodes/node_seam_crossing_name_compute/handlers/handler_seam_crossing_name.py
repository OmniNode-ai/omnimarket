# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure seam crossing name handler.

Names the class of a bus crossing at a seam: rule R, then a deterministic
key check. No I/O, no clock.
"""

from __future__ import annotations

from omnimarket.models.seam_crossing_name import (
    EnumSeamCrossingDecidedBy,
    ModelSeamCrossingName,
    ModelSeamCrossingNameRequest,
    ModelSeamCrossingNameVerdict,
    ModelSeamCrossingNaming,
)
from omnimarket.nodes.node_seam_crossing_name_compute.overlay import (
    load_seam_crossing_naming,
)


class HandlerSeamCrossingName:
    """Pure, deterministic seam crossing name handler.

    The labels come from the request when it carries them, else from the labels
    injected at construction, else from the overlay or the contract's packaged config
    resolved once at construction (composition), never while naming.
    """

    def __init__(self, naming: ModelSeamCrossingNaming | None = None) -> None:
        self._naming = naming if naming is not None else load_seam_crossing_naming()

    def handle(
        self, request: ModelSeamCrossingNameRequest
    ) -> ModelSeamCrossingNameVerdict:
        naming = request.naming if request.naming is not None else self._naming
        names: list[ModelSeamCrossingName] = []
        counts: dict[str, int] = {}

        for crossing in request.crossings:
            declared = set(crossing.consumer.fields.keys())
            required = {
                name
                for name, facts in crossing.consumer.fields.items()
                if facts.required
            }
            body = set(crossing.body_keys)

            missing = sorted(required - body)
            if missing:
                name = naming.missing_required
                fields = tuple(missing)
                decided_by = EnumSeamCrossingDecidedBy.RULE_R
            else:
                undeclared = sorted(body - declared)
                if undeclared:
                    name = naming.undeclared_key
                    fields = tuple(undeclared)
                    decided_by = EnumSeamCrossingDecidedBy.KEY_CHECK
                else:
                    name = naming.clean
                    fields = ()
                    decided_by = EnumSeamCrossingDecidedBy.KEY_CHECK

            n = ModelSeamCrossingName(
                crossing_id=crossing.crossing_id,
                seam_id=crossing.seam_id,
                topic=crossing.topic,
                name=name,
                fields=fields,
                decided_by=decided_by,
            )
            names.append(n)
            counts[name] = counts.get(name, 0) + 1

        return ModelSeamCrossingNameVerdict(
            names=tuple(names),
            counts=counts,
        )
