# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ssh link gate: what a lossy link costs a lane, judged on sustained loss and never on one sample (OMN-17427).

What breaks an ssh lane is sustained loss. On 2026-10-01 one lab host's link lost 40-80% of packets from another lab host (FRICTION
2026-10-01T04:34:32Z: ssh timed out on most attempts, so the lane lost its log stream and its closeout) and 40% of 20 pings
from another lab host. On 2026-10-18T21:44Z one lost ping of ten (10%), one 10-ping sample, refused one lab host (a networked lab host that runs lab-work
on the bus) for every unit. So a host is

- BENCHED (``refusal``: takes no lane or worker) only when this read and the one before it are both over ``ADMIT_MAX_LINK_LOSS_PCT``
  of ``LINK_PING_COUNT`` pings (``LINK_SUSTAINED_READS`` consecutive reads); a failed ssh probe benches it elsewhere, as ever;
- DEGRADED over ``LINK_SOFT_LOSS_PCT``: it still admits, and ranking puts it after every host with a cleaner link;
- never judged for a unit delivered over the bus, or for the launching host.
"""

import re

from ..models.model_link_gate import ModelLinkGateRequest, ModelLinkGateVerdict

ADMIT_MAX_LINK_LOSS_PCT = 20.0
LINK_SOFT_LOSS_PCT = 5.0
LINK_SUSTAINED_READS = 2
LINK_PING_COUNT = 50
LINK_PING_INTERVAL_S = 0.2
LINK_PING_TIMEOUT_S = 30.0  # 50 pings at 0.2 s take about ten seconds, and a silent host adds ping's own wait for the last reply

_LOSS_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)% packet loss")


def parse_link_loss(text: str) -> float | None:
    """The percent lost from a Linux or macOS ``ping`` summary, or None when there is no summary."""
    found = _LOSS_RE.findall(text or "")
    return float(found[-1]) if found else None


def ping_argv(target: str) -> list[str]:
    """An unprivileged ping of an ssh destination's address (the ``user@`` part dropped), the same on Linux and macOS."""
    return [
        "ping",
        "-q",
        "-c",
        str(LINK_PING_COUNT),
        "-i",
        str(LINK_PING_INTERVAL_S),
        target.rsplit("@", 1)[-1],
    ]


class HandlerLinkGate:
    def handle(self, request: ModelLinkGateRequest) -> ModelLinkGateVerdict:
        loss = request.loss_pct
        if loss is None or request.local or request.bus:
            return ModelLinkGateVerdict(refusal=None, degraded=False)
        run = (loss, *request.prior[: LINK_SUSTAINED_READS - 1])
        sustained = len(run) == LINK_SUSTAINED_READS and all(
            read > ADMIT_MAX_LINK_LOSS_PCT for read in run
        )
        why = (
            (
                f"LINK-LOSS {loss:.0f}% of pings lost, over {ADMIT_MAX_LINK_LOSS_PCT:.0f}% in {LINK_SUSTAINED_READS} consecutive "
                f"reads ({', '.join(f'{read:.0f}%' for read in run)})"
            )
            if sustained
            else None
        )
        return ModelLinkGateVerdict(refusal=why, degraded=loss > LINK_SOFT_LOSS_PCT)
