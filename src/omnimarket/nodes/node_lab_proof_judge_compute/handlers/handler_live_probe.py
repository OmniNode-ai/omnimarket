# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerLiveProbe: the live-grep probe check as a pure function.

Definition-B compute: ``handle(request: ModelLiveProbeRequest) -> ModelLiveProbeResult``.
No I/O, no clock, no hard-coded values: the probe text comes from the request.
"""

from __future__ import annotations

import re

from omnimarket.nodes.node_lab_proof_judge_compute.models.model_live_probe import (
    PROBE_TERM_DELIMITER,
    ModelLiveProbeRequest,
    ModelLiveProbeResult,
)


class HandlerLiveProbe:
    """The live-grep probe check: pure, no I/O, no clock."""

    def handle(self, request: ModelLiveProbeRequest) -> ModelLiveProbeResult:
        """Compute the live-grep verdict for the probe's text."""
        mentions: dict[str, int] = {}
        for _, name, count in re.findall(
            r"^\s+(\S+) mentions (\S+): (\d+)$", request.probe, re.M
        ):
            name = name.removeprefix(PROBE_TERM_DELIMITER).removesuffix(
                PROBE_TERM_DELIMITER
            )
            name = re.sub(r"^\(([^|()]+)\|[^|()]+\)$", r"\1", name)
            mentions[name] = mentions.get(name, 0) + int(count)
        not_applicable = [
            n
            for line in re.findall(
                r"^live-grep not-applicable: ([^(\n]+?) \(", request.probe, re.M
            )
            for n in line.split()
        ]
        notes: list[str] = []
        if mentions:
            dead = sorted(n for n, c in mentions.items() if c == 0)
            base = not dead
            if dead:
                notes.append(
                    "changed path not live, no runtime log mentions: " + ", ".join(dead)
                )
        elif not_applicable:
            base = True
        else:
            # The probe printed no live-grep line: there is no check.
            base = None
        if not_applicable:
            notes.append(
                "changed path not applicable, runs in no runtime (no topics, "
                "no runtime profile): " + ", ".join(not_applicable)
            )
        contract_local_nodes = tuple(not_applicable)
        return ModelLiveProbeResult(
            base=base,
            contract_local_nodes=contract_local_nodes,
            notes=tuple(notes),
        )
