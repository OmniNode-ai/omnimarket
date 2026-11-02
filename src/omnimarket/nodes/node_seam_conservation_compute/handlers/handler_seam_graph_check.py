# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Check every declared seam side against its emitter's contract snapshot."""

from collections import Counter

from omnimarket.nodes.node_seam_conservation_compute.models.model_seam_graph_check import (
    ModelSeamGraphCheckRequest,
    ModelSeamGraphCheckResult,
)


class HandlerSeamGraphCheck:
    def handle(self, request: ModelSeamGraphCheckRequest) -> ModelSeamGraphCheckResult:
        faults: list[str] = []
        counts = Counter(d.seam_id for d in request.declarations)
        expected = set(request.expected_seam_ids)
        for seam_id in sorted(expected - counts.keys()):
            faults.append(f"{seam_id}: no seam declaration")
        for seam_id in sorted(counts.keys() - expected):
            faults.append(f"{seam_id}: undeclared seam")
        for seam_id, count in sorted(counts.items()):
            if count != 1:
                faults.append(f"{seam_id}: duplicate seam declaration")
        for declaration in request.declarations:
            for side, emitter in (
                ("sender", declaration.sender_emitter),
                ("receiver", declaration.receiver_emitter),
            ):
                prefix = f"{declaration.seam_id} {side}"
                if not emitter:
                    faults.append(f"{prefix}: no emitter")
                    continue
                topics = request.emitter_topics.get(emitter)
                if topics is None:
                    faults.append(f"{prefix}: {emitter} has no contract")
                    continue
                for topic in request.required_topics:
                    if topic not in topics:
                        faults.append(f"{prefix}: {emitter} does not publish {topic}")
        return ModelSeamGraphCheckResult(faults=tuple(faults))
