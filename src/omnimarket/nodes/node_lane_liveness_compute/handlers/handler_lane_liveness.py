# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerLaneLiveness — lane verdicts over the cloud hook ledger (OMN-18609).

Pure and deterministic: every read happens in the caller, every decision
happens here. That split is what lets the fail-closed rules below be tested
against a window that has already happened, with no cluster and no database.

THE ORDER OF THE RULES IS THE DESIGN.

1. **Relay first.** Ask whether the capture relay was delivering at all before
   asking anything about a lane. A relay outage and the simultaneous death of
   every lane are the same signature in ``public.hook_events``: no rows. On
   2026-09-17 the local drainer died unsupervised at 15:31Z and the table went
   from ``15:20:54.543159Z`` straight to ``16:01:46.740871Z``, a hole of 40
   minutes 52 seconds, while every lane on the fleet was working normally
   throughout. A naive last-event-age rule reports the entire fleet DROPPED
   there. The relay threshold is deliberately SHORTER than the lane threshold
   so it trips first; a guard that arrives after the verdict it guards is not a
   guard.

2. **A terminal row outranks silence**, and survives relay silence, because it
   is ledger evidence rather than hook evidence. A lane that said it finished
   did not drop, whatever the relay was doing.

3. **No attribution means no answer.** Events written before the emitter
   carried a lane name cannot support a per-lane reading at any threshold: the
   absence of events "for lane X" in such a window is the absence of the
   concept, not the absence of the lane. Those windows report UNOBSERVABLE,
   with the ledger facts preserved in the reason and the basis.

4. **DROPPED requires hook evidence, always.** It is the narrow, actionable
   conclusion: a CLAIM row, no TERMINAL row, a relay demonstrably carrying
   other traffic, and this lane silent past the threshold. It is never reached
   from the hand ledger alone. A claim with no terminal row is the PRE-EXISTING
   un-terminated-claim signal -- the one that produced 3,144 `died_no_terminal`
   and 969 unattributed verdicts in the lane-registry reconcile -- and
   restating it under a new name would make this reader a second source of the
   noise it was built to replace.

WHY NOT ``session_id``. It is not a unit of work. One ``session_id`` covered
79,343 of the table's 83,067 rows across nine days, because it delimits a
RESUMED Claude Code session. ``correlation_id``, ``run_id`` and ``entity_id``
carry that same value, so none of the four is a key. The lane is the key, and
making the lane reach the event was the prerequisite half of this ticket.
"""

from __future__ import annotations

from datetime import datetime

from omnimarket.nodes.node_lane_liveness_compute.models.model_lane_liveness import (
    EnumEvidenceBasis,
    EnumLaneVerdict,
    EnumRelayState,
    ModelLaneLivenessReport,
    ModelLaneLivenessRequest,
    ModelLaneObservation,
    ModelLaneVerdict,
)


def _elapsed_seconds(later: datetime, earlier: datetime) -> int:
    """Whole seconds between two instants, floored at zero.

    Floored because a clock skew that puts an event marginally after the window
    end must not become a negative silence that reads as freshness.
    """
    return max(0, int((later - earlier).total_seconds()))


class HandlerLaneLiveness:
    """Turn lane observations into verdicts. No I/O, no clock, no network."""

    def handle(self, payload: ModelLaneLivenessRequest) -> ModelLaneLivenessReport:
        """Compute one report over one window.

        Args:
            payload: the window, the per-lane observations and the relay facts.
                Named ``payload`` so the RuntimeLocal adapter passes the
                validated request positionally rather than keyword-fanning its
                fields (the OMN-13276 shape).

        Returns:
            A report whose every verdict names the evidence it rests on.
        """
        request = payload
        relay_state, relay_silent_seconds = self._relay_state(request)

        verdicts = tuple(
            self._verdict(observation, request, relay_state)
            for observation in request.observations
        )

        return ModelLaneLivenessReport(
            window_start=request.window_start,
            window_end=request.window_end,
            relay_state=relay_state,
            relay_last_event_at=request.relay_last_event_at,
            relay_event_count=request.relay_event_count,
            relay_silent_seconds=relay_silent_seconds,
            lane_attribution_available=request.lane_attribution_available,
            verdicts=verdicts,
        )

    # -- rule 1 ------------------------------------------------------------

    def _relay_state(
        self, request: ModelLaneLivenessRequest
    ) -> tuple[EnumRelayState, int | None]:
        """Was the relay delivering? Asked before any lane question.

        A window with no events at all is SILENT regardless of thresholds --
        there is no last event to measure age from, and treating "never
        delivered" as "delivered long ago" would make the answer depend on how
        wide a window somebody chose.
        """
        if request.relay_last_event_at is None or request.relay_event_count == 0:
            return EnumRelayState.SILENT, None
        silent = _elapsed_seconds(request.window_end, request.relay_last_event_at)
        if silent > request.relay_silence_threshold_seconds:
            return EnumRelayState.SILENT, silent
        return EnumRelayState.CARRYING, silent

    # -- rules 2-4 ---------------------------------------------------------

    def _verdict(
        self,
        observation: ModelLaneObservation,
        request: ModelLaneLivenessRequest,
        relay_state: EnumRelayState,
    ) -> ModelLaneVerdict:
        silent_seconds = (
            _elapsed_seconds(request.window_end, observation.last_hook_event_at)
            if observation.last_hook_event_at is not None
            else None
        )

        def build(
            verdict: EnumLaneVerdict, basis: EnumEvidenceBasis, reason: str
        ) -> ModelLaneVerdict:
            return ModelLaneVerdict(
                lane=observation.lane,
                verdict=verdict,
                evidence_basis=basis,
                reason=reason,
                last_hook_event_at=observation.last_hook_event_at,
                hook_event_count=observation.hook_event_count,
                silent_seconds=silent_seconds,
            )

        # Rule 2. A terminal row is ledger evidence and outranks every
        # hook-derived state, including relay silence.
        if observation.terminal_at is not None:
            return build(
                EnumLaneVerdict.TERMINATED,
                EnumEvidenceBasis.LEDGER_ONLY,
                f"terminal row at {observation.terminal_at.isoformat()}",
            )

        # Rule 1, applied. Nothing below may conclude a drop from silence.
        if relay_state is EnumRelayState.SILENT:
            return build(
                EnumLaneVerdict.UNKNOWN_RELAY_SILENT,
                EnumEvidenceBasis.NONE,
                "the capture relay delivered nothing across this window, so no "
                "lane's silence is evidence about that lane",
            )

        # A lane with live events is alive whatever the ledger says; the hook
        # ledger is the primary record and a missing CLAIM row is a ledger gap,
        # not a dead lane.
        if (
            observation.last_hook_event_at is not None
            and silent_seconds is not None
            and silent_seconds <= request.silence_threshold_seconds
        ):
            return build(
                EnumLaneVerdict.ALIVE,
                EnumEvidenceBasis.HOOK_EVENTS,
                f"{observation.hook_event_count} hook event(s), newest "
                f"{silent_seconds}s before the window end",
            )

        # Rule 3. Without attribution on the wire, an absence of events for a
        # named lane is the absence of the concept.
        if not request.lane_attribution_available:
            if observation.claimed_at is None:
                return build(
                    EnumLaneVerdict.UNOBSERVABLE,
                    EnumEvidenceBasis.NONE,
                    "no hook event in this window carries a lane attribution, "
                    "and the lane has no claim row either",
                )
            # Deliberately NOT dropped. A claim with no terminal row is the
            # PRE-EXISTING un-terminated-claim signal, and treating it as a
            # drop is what produced the 3,144 `died_no_terminal` and 969
            # unattributed verdicts the lane-registry reconcile is called noise
            # for. DROPPED is reserved for a conclusion the hook ledger can
            # actually support; reproducing the old signal under a new name
            # would make this reader a second source of the same noise.
            return build(
                EnumLaneVerdict.UNOBSERVABLE,
                EnumEvidenceBasis.LEDGER_ONLY,
                "the hand ledger shows a claim row with no terminal row, which "
                "is the pre-existing un-terminated-claim signal and not a drop; "
                "no hook event in this window carries a lane attribution, so "
                "the hook ledger can neither corroborate nor refute it",
            )

        # Rule 4. Attribution is available and the relay is carrying, so
        # silence is now genuinely about this lane.
        if observation.claimed_at is None:
            return build(
                EnumLaneVerdict.UNOBSERVABLE,
                EnumEvidenceBasis.NONE,
                "no claim row and no recent hook events; nothing asserts this "
                "lane was ever dispatched",
            )
        if observation.last_hook_event_at is None:
            return build(
                EnumLaneVerdict.DROPPED,
                EnumEvidenceBasis.HOOK_EVENTS,
                "claim row with no terminal row, and no hook event at all in "
                "this window while the relay was carrying other traffic",
            )
        return build(
            EnumLaneVerdict.DROPPED,
            EnumEvidenceBasis.HOOK_EVENTS,
            f"claim row with no terminal row; last hook event {silent_seconds}s "
            f"before the window end, past the {request.silence_threshold_seconds}s "
            "threshold, while the relay was carrying other traffic",
        )


__all__ = ["HandlerLaneLiveness"]
