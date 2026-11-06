# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compare census and alarm aliases without a host table in code."""

from __future__ import annotations

from .models import ModelLaneCensusAliasParityRequest, ModelLaneCensusAliasParityResult


class HandlerLaneCensusAliasParity:
    def handle(
        self, request: ModelLaneCensusAliasParityRequest
    ) -> ModelLaneCensusAliasParityResult:
        # Preserve the original manifest-to-alarm host-id conversion and exact
        # alias comparison. A new host changes deployment data on both sides.
        declared = {
            f"h{name.removeprefix('lab-')}": aliases
            for name, aliases in request.manifest_hosts.items()
        }
        alarm = {host: (alias,) for host, alias in request.alarm_aliases.items()}
        return ModelLaneCensusAliasParityResult(
            matches=declared == alarm,
            missing_from_alarm=tuple(sorted(declared.keys() - alarm.keys())),
            missing_from_census=tuple(sorted(alarm.keys() - declared.keys())),
            different_aliases=tuple(
                sorted(
                    host
                    for host in declared.keys() & alarm.keys()
                    if declared[host] != alarm[host]
                )
            ),
        )
