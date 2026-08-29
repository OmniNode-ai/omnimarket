# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Prove the alert channel alive between alerts (OMN-15600).

Epic OMN-16776 Phase 1, gate item 5 — the last one open after 2026-08-29T05:35Z.
The epic's own statement of what the live 05:27Z delivery did *not* establish::

    It does NOT satisfy item 5, which asks for a *checker* that reports FAILURE
    when pointed at a known-dead channel, proven RED first.  Until it does, a
    channel that goes dead between alerts still goes dead silently.

Canonical definition-B shape
----------------------------
``handle(request: ModelAlertChannelProbeTrigger) ->
ModelAlertChannelLivenessResult``: typed payload in, typed payload out.  No
event envelope in the core, no ``ModelHandlerOutput``.  The runtime publishes
the returned model as this node's terminal event.

Why this rides the heartbeat instead of owning a schedule
---------------------------------------------------------
Epic OMN-16776 forbids polling scripts, daemons, ``/metrics`` endpoints and new
files under ``scripts/**`` outright, and names the carrier: the heartbeat the
runtime **already** emits.  So this node subscribes to
``onex.evt.platform.node-heartbeat.v1`` and throttles itself to the
contract-declared ``probe_interval_seconds``.  It owns no timer, no loop and no
thread.

The property that buys is the one the epic cares about: **the signal dies with
the thing it measures**.  A separate poller would keep reporting on a runtime
that is gone.  A heartbeat-carried check stops producing verdicts exactly when
the runtime stops producing heartbeats, and the absence of the terminal event
is then itself the finding.

Why a failure is not raised
---------------------------
A DEAD channel is a *result*, not a malformed input.  Raising would DLQ the
heartbeat onto a topic named for malformed input and destroy the verdict in
order to report it.  The verdict rides the terminal event — a surface
independent of the Slack channel being judged, which is the circularity that
made the original outage invisible: the only thing that would have told anyone
alerting was broken was the alerting.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from omnimarket.nodes.node_alert_channel_liveness_effect.handlers.classify_channel_probe import (
    classify_channel_probe,
)
from omnimarket.nodes.node_alert_channel_liveness_effect.handlers.slack_channel_prober import (
    ProtocolAlertChannelProber,
    SlackAlertChannelProber,
)
from omnimarket.nodes.node_alert_channel_liveness_effect.models import (
    ModelAlertChannelLivenessResult,
    ModelAlertChannelObservation,
    ModelAlertChannelProbeTrigger,
    ModelAlertChannelVerdict,
    load_liveness_policy,
)

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

_CONTRACT_PATH = Path(__file__).parent.parent / "contract.yaml"


class HandlerAlertChannelLiveness:
    """Probe the alert channel on the existing tick and classify what it says."""

    def __init__(
        self,
        *,
        prober: ProtocolAlertChannelProber | None = None,
        clock: Callable[[], float] | None = None,
        contract_path: Path | None = None,
    ) -> None:
        """Self-assemble the cadence and the probe from this node's contract.

        Args:
            prober: The read-only observation. Defaults to the real Slack
                prober built from the contract's own secret refs and timeout;
                the hermetic tests inject a fake so they drive this exact
                handler rather than a stand-in.
            clock: Wall clock used for the interval throttle, injectable so the
                cadence is tested by advancing time rather than sleeping on it.
            contract_path: Contract to self-assemble from. Overridable so a
                test can point the node at a modified copy and watch the
                cadence change with no code edit.

        Raises:
            AlertChannelLivenessPolicyError: The contract declares no usable
                ``liveness_policy``. Fail closed — a checker that invents its
                own cadence is running a schedule nobody declared.
        """
        path = contract_path or _CONTRACT_PATH
        self._contract_path = path
        self._policy = load_liveness_policy(path)
        self._clock = clock or time.time
        self._prober = prober or SlackAlertChannelProber(
            path, timeout_seconds=self._policy.probe_timeout_seconds
        )
        #: Wall-clock bucket of the last completed probe. The ONLY state this
        #: node keeps, and it is a throttle rather than a verdict cache: losing
        #: it on restart re-probes one interval early, which is harmless. A
        #: verdict is never carried across ticks.
        self._last_probe_bucket: int | None = None

    @property
    def policy(self) -> object:
        """The contract-declared cadence this node assembled for itself."""
        return self._policy

    def handle(
        self, request: ModelAlertChannelProbeTrigger
    ) -> ModelAlertChannelLivenessResult:
        """Probe the alert channel unless this interval is already proven.

        Args:
            request: A runtime heartbeat. Nothing in the verdict depends on its
                contents — see :class:`ModelAlertChannelProbeTrigger`.

        Returns:
            The result the runtime publishes as this node's terminal event:
            whether a probe ran, the classified verdict when one did, and
            whether a non-healthy verdict was surfaced.
        """
        interval = self._policy.probe_interval_seconds
        bucket = int(self._clock()) // interval
        if self._last_probe_bucket is not None and bucket <= self._last_probe_bucket:
            return ModelAlertChannelLivenessResult(
                probed=False,
                verdict=None,
                probe_interval_seconds=interval,
                failure_surfaced=False,
            )

        self._last_probe_bucket = bucket
        verdict = self._probe()

        failure = not verdict.healthy
        if failure:
            logger.error(
                "ALERT CHANNEL %s: %s (service=%s node=%s slack_error=%s). "
                "Alerts published to this channel would not reach anyone.",
                verdict.status.value,
                verdict.reason,
                request.service_name,
                request.node_id,
                verdict.slack_error,
            )
        else:
            logger.info(
                "Alert channel LIVE: %s (service=%s)",
                verdict.reason,
                request.service_name,
            )

        return ModelAlertChannelLivenessResult(
            probed=True,
            verdict=verdict,
            probe_interval_seconds=interval,
            failure_surfaced=failure,
        )

    def _probe(self) -> ModelAlertChannelVerdict:
        """Observe the channel and classify it, never raising.

        A prober that blows up is classified PROBE_ERROR here rather than
        propagated. The distinction matters: propagating would lose the one
        fact worth keeping — that the channel could not be proven — and would
        route the heartbeat to a malformed-input DLQ, which it is not.
        """
        try:
            observation = self._prober.probe()
        except Exception as exc:
            observation = ModelAlertChannelObservation(
                credentials_present=True,
                transport_error=f"{type(exc).__name__}: {exc}",
            )
        return classify_channel_probe(observation)


__all__ = ["HandlerAlertChannelLiveness"]
