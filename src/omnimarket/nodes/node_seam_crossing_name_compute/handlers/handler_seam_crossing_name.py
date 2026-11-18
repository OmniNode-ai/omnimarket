# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# unimported-handler-allow: HandlerSeamCrossingName is resolved at boot from handler_routing in contract.yaml, by module path, not by a Python import
"""Pure seam crossing name handler.

Names the class of a bus crossing at a seam: rule R, then a deterministic
key check. No I/O, no clock. The consumer facts used are required,
has_default, extra_forbidden and has_adapter.
"""

from __future__ import annotations

from omnimarket.models.seam_crossing_name import (
    EnumSeamCrossingDecidedBy,
    ModelSeamCrossingName,
    ModelSeamCrossingNameRequest,
    ModelSeamCrossingNameVerdict,
)


class HandlerSeamCrossingName:
    """Pure, deterministic seam crossing name handler."""

    def handle(
        self, request: ModelSeamCrossingNameRequest
    ) -> ModelSeamCrossingNameVerdict:
        naming = request.naming
        names: list[ModelSeamCrossingName] = []
        counts: dict[str, int] = {}
        adapter_advisory = 0

        for crossing in request.crossings:
            declared = set(crossing.consumer.fields.keys())
            required = {
                name
                for name, facts in crossing.consumer.fields.items()
                if facts.required and not facts.has_default
            }
            body = set(crossing.body_keys)

            missing = sorted(required - body)
            if missing:
                name = naming.missing_required
                fields = tuple(missing)
                decided_by = EnumSeamCrossingDecidedBy.RULE_R
            else:
                undeclared = sorted(body - declared)
                if undeclared and crossing.consumer.extra_forbidden:
                    name = naming.undeclared_key
                    fields = tuple(undeclared)
                    decided_by = EnumSeamCrossingDecidedBy.KEY_CHECK
                else:
                    name = naming.clean
                    fields = ()
                    decided_by = EnumSeamCrossingDecidedBy.KEY_CHECK

            has_adapter = crossing.consumer.has_adapter
            if has_adapter:
                adapter_advisory += 1

            n = ModelSeamCrossingName(
                crossing_id=crossing.crossing_id,
                seam_id=crossing.seam_id,
                topic=crossing.topic,
                name=name,
                fields=fields,
                decided_by=decided_by,
                consumer_has_adapter=has_adapter,
            )
            names.append(n)
            counts[name] = counts.get(name, 0) + 1

        return ModelSeamCrossingNameVerdict(
            names=tuple(names),
            counts=counts,
            adapter_advisory=adapter_advisory,
            terminal_failure_cause=None,
        )
