# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Which records behind a refused journal head prove the broker is reachable (OMN-19998).

The hook-emit drainer cannot tell a refused head from a dead broker by counting failures: under a count-only rule a
long outage would dead-letter the whole backlog one record at a time. So unpublishability is measured. If a record
BEHIND the head goes out, the broker is reachable and the head is genuinely refused. If none does, the fault is not
specific to the head and nothing is moved. The guard is a publish that succeeds, never an exception type: the
transport masked a permanent ACL refusal as a TimeoutError for three days (OMN-19073).

The probe asks about DIFFERENT event classes when the batch holds them (OMN-17427). A record of the head's own class
fails for the head's own reason, so publishing it measures the class, not the broker: one refused class read as
"broker unreachable" for six and a half hours on 2026-10-06/07 and held 24,530 authorized records behind it. One
class is not enough either. A lab host whose broker login held no grant on two classes (prompt.submitted and
content.captured) had a head of the first and a first different class of the second, so the probe failed for the
second class's own reason and the hook events behind both stayed unpublished (h053, 2026-11-06, OMN-19998). The probe
therefore asks up to ``max_classes`` different classes, one record per class and none of a class the process already
proved refused. When no different class is left to ask (the whole batch is the head's class, or every other class is already
proved refused), the record directly behind is still the probe, which keeps a
dead broker a stall rather than a dead-letter run, and a dead broker pays at most ``max_classes`` failed publishes on
a cycle that is already backing off.

Pure: the same request gives the same result, and the drainer does the publishing.
"""

from __future__ import annotations

from ..models import ModelHookDrainProbeRequest, ModelHookDrainProbeResult


class HandlerHookDrainProbe:
    """Choose the probe records; never publish."""

    def handle(self, request: ModelHookDrainProbeRequest) -> ModelHookDrainProbeResult:
        pending = request.pending_event_types
        if len(pending) < 2:
            return ModelHookDrainProbeResult(
                probe_order=(), reason="fewer-than-two-pending"
            )
        head = pending[0]
        skip = set(request.refused_event_types)
        asked: list[str] = []
        order: list[int] = []
        for index in range(1, len(pending)):
            event_type = pending[index]
            if event_type == head or event_type in skip or event_type in asked:
                continue
            asked.append(event_type)
            order.append(index)
            if len(asked) >= request.max_classes:
                break
        if order:
            return ModelHookDrainProbeResult(
                probe_order=tuple(order), reason="different-classes"
            )
        return ModelHookDrainProbeResult(probe_order=(1,), reason="directly-behind")
