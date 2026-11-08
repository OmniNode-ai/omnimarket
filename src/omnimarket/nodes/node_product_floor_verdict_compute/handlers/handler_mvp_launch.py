# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The MVP launch block (OMN-23205, MVP launch plan task M1).

Pure: the clock, the run histories, the candidate manifest and the overlay are handed in; nothing is read here.

The rule for a scheduled reading: the newest scheduled run decides, and grace governs only absence.
  * the latest due slot is the newest cron instant at or before now minus grace;
  * no eligible run created at or after that slot is ``missed-slot``;
  * otherwise the newest eligible run by creation time decides: still executing is ``latest-running`` (never green on an
    older run); a conclusion other than the green one is NOT WORKING with the conclusion as the reason;
  * a newer slot with no run yet, inside its grace, leaves the newest existing run deciding;
  * a green run whose graded versions differ from the candidate manifest is ``not-the-candidate``.
A customer-machine reading is also composite: the newest walk must be recent, graded the candidate and read its Mac leg
WORKING. A derived reading is NOT WORKING when any input is unreadable or itself NOT WORKING. An unread or unreadable
source is NOT WORKING, never WORKING; the block is WORKING only when all 17 readings are.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from omnimarket.models.floor_17a.model_mvp_launch import (
    CANDIDATE_INPUT,
    MVP_DERIVED_IDS,
    MVP_READING_IDS,
    ModelCron,
    ModelMvpCandidate,
    ModelMvpLaunchBlock,
    ModelMvpLaunchRequest,
    ModelMvpReading,
    ModelMvpReadingSpec,
    ModelMvpRun,
    ModelMvpSourceHistory,
    MvpState,
)

WORKING: MvpState = "WORKING"
NOT_WORKING: MvpState = "NOT WORKING"

REASON_UNREADABLE = "unreadable"
REASON_MISSED_SLOT = "missed-slot"
REASON_LATEST_RUNNING = "latest-running"
REASON_NOT_THE_CANDIDATE = "not-the-candidate"
REASON_CANDIDATE_UNREADABLE = "candidate-unreadable"
REASON_MAC_MISSING = "mac-leg-missing"
REASON_MAC_FAILED = "mac-leg-failed"
REASON_MAC_STALE = "mac-leg-stale"
REASON_UNREADABLE_CONCLUSION = "unreadable-conclusion"


class HandlerMvpLaunch:
    """The ``mvp_launch`` block, from recorded run histories."""

    def handle(self, request: ModelMvpLaunchRequest) -> ModelMvpLaunchBlock:
        ov = request.overlay
        scenarios = tuple(
            self._scheduled(spec, request, ov.walk_reading_id) for spec in ov.scenarios
        )
        by_scenario = {s.id: s for s in scenarios}
        pending = [spec for spec in ov.readings if spec.kind == "scheduled"]
        done: dict[str, ModelMvpReading] = {
            spec.id: self._scheduled(spec, request, ov.walk_reading_id)
            for spec in pending
        }
        candidate = self._candidate_reading(request.candidate)
        readings: list[ModelMvpReading] = []
        for spec in ov.readings:
            if spec.kind == "derived":
                readings.append(
                    self._derived(
                        spec, {**done, **by_scenario}, candidate, request.candidate
                    )
                )
            else:
                readings.append(done[spec.id])
        readings.sort(key=lambda r: r.id)
        named = tuple(f"{r.id}: {r.reason}" for r in readings if r.state != WORKING)
        state: MvpState = WORKING if not named else NOT_WORKING
        title = ov.title
        line = (
            f"{title}: WORKING, {len(readings)} readings"
            if state == WORKING
            else f"{title}: NOT WORKING, " + "; ".join(named)
        )
        return ModelMvpLaunchBlock(
            state=state,
            line=line,
            generated_at=request.now,
            max_age_s=ov.max_age_s,
            readings=tuple(readings),
            scenarios=scenarios,
            named=named,
            candidate_versions=dict(request.candidate.versions),
            candidate_error=request.candidate.error,
        )

    @staticmethod
    def refused(
        *, now: datetime, max_age_s: int, reason: str, detail: str
    ) -> ModelMvpLaunchBlock:
        """The block when its overlay could not be read: all 17 ids, each NOT WORKING with the reason, none absent."""
        readings = tuple(
            ModelMvpReading(
                id=rid,
                kind="derived" if rid in MVP_DERIVED_IDS else "scheduled",
                state=NOT_WORKING,
                reason=reason,
                detail=detail,
            )
            for rid in sorted(MVP_READING_IDS)
        )
        return ModelMvpLaunchBlock(
            state=NOT_WORKING,
            line=f"MVP launch: NOT WORKING, {reason}",
            generated_at=now,
            max_age_s=max_age_s,
            readings=readings,
            scenarios=(),
            named=tuple(f"{r.id}: {reason}" for r in readings),
        )

    # ----------------------------------------------------------------------------------------------------------------

    @staticmethod
    def _candidate_reading(candidate: ModelMvpCandidate) -> tuple[MvpState, str]:
        if candidate.read:
            return WORKING, "ok"
        return NOT_WORKING, REASON_UNREADABLE

    @staticmethod
    def _eligible(
        spec: ModelMvpReadingSpec, history: ModelMvpSourceHistory
    ) -> list[ModelMvpRun]:
        """The runs that count as scheduled for this reading, oldest first."""
        admitted = {"schedule", *spec.admitted_triggers}
        runs = [r for r in history.runs if r.event in admitted]
        runs.sort(key=lambda r: (r.created_at, r.run_id))
        return runs

    @staticmethod
    def _differs(
        graded: tuple[str, ...],
        run_versions: dict[str, str],
        candidate: ModelMvpCandidate,
    ) -> bool:
        return any(
            run_versions.get(p) is None
            or candidate.versions.get(p) is None
            or run_versions[p] != candidate.versions[p]
            for p in graded
        )

    def _scheduled(
        self, spec: ModelMvpReadingSpec, request: ModelMvpLaunchRequest, walk_id: str
    ) -> ModelMvpReading:
        history = request.histories.get(spec.id)
        base = {"id": spec.id, "kind": "scheduled", "mac_leg": spec.mac_leg}
        if history is None or history.error is not None:
            return ModelMvpReading(
                **base,
                state=NOT_WORKING,
                reason=REASON_UNREADABLE,
                detail=(history.error if history else "no history supplied") or "",
            )
        # the overlay loader guarantees both
        assert spec.cron is not None
        assert spec.grace_s is not None
        now = request.now
        due = ModelCron.parse(spec.cron).latest_at_or_before(
            now - timedelta(seconds=spec.grace_s)
        )
        runs = self._eligible(spec, history)
        newest = runs[-1] if runs else None
        success = [
            r
            for r in runs
            if r.status == "completed" and r.conclusion == spec.green_conclusion
        ]
        facts = {
            "latest_due_slot": due,
            "newest_run_id": newest.run_id if newest else None,
            "newest_run_created_at": newest.created_at if newest else None,
            "newest_run_conclusion": newest.conclusion if newest else None,
            "last_success": success[-1].created_at if success else None,
            "age_s": int((now - newest.created_at).total_seconds()) if newest else None,
            "graded_versions": dict(newest.versions) if newest else {},
        }

        def verdict(
            state: MvpState, reason: str, **more: str | None
        ) -> ModelMvpReading:
            return ModelMvpReading(
                **base,
                **facts,
                state=state,
                reason=reason,
                **more,
            )

        if newest is None or (due is not None and newest.created_at < due):
            return verdict(NOT_WORKING, REASON_MISSED_SLOT)
        if newest.status != "completed":
            return verdict(NOT_WORKING, REASON_LATEST_RUNNING)
        if newest.conclusion != spec.green_conclusion:
            return verdict(
                NOT_WORKING, newest.conclusion or REASON_UNREADABLE_CONCLUSION
            )
        if spec.graded_packages:
            if not request.candidate.read:
                return verdict(NOT_WORKING, REASON_CANDIDATE_UNREADABLE)
            if self._differs(spec.graded_packages, newest.versions, request.candidate):
                return verdict(NOT_WORKING, REASON_NOT_THE_CANDIDATE)
        if spec.mac_leg is not None:
            mac_state, mac_reason = self._mac_leg(spec, request, walk_id)
            if mac_reason is not None:
                return verdict(NOT_WORKING, mac_reason, mac_leg_state=mac_state)
            return verdict(WORKING, "ok", mac_leg_state=mac_state)
        return verdict(WORKING, "ok")

    def _mac_leg(
        self, spec: ModelMvpReadingSpec, request: ModelMvpLaunchRequest, walk_id: str
    ) -> tuple[str | None, str | None]:
        """The Mac leg's state in the newest walk and the reason it does not vouch, or ``None`` when it does."""
        assert spec.mac_leg is not None
        walk_spec = next(r for r in request.overlay.readings if r.id == walk_id)
        history = request.histories.get(walk_id)
        if history is None or history.error is not None:
            return None, REASON_MAC_MISSING
        runs = self._eligible(walk_spec, history)
        if not runs:
            return None, REASON_MAC_MISSING
        walk = runs[-1]
        leg = walk.legs.get(spec.mac_leg) if walk.status == "completed" else None
        if leg is None:
            return None, REASON_MAC_MISSING
        if (
            request.now - walk.created_at
        ).total_seconds() > request.overlay.mac_leg_max_age_s:
            return leg, REASON_MAC_STALE
        if walk_spec.graded_packages:
            if not request.candidate.read:
                return leg, REASON_CANDIDATE_UNREADABLE
            if self._differs(
                walk_spec.graded_packages, walk.versions, request.candidate
            ):
                return leg, REASON_NOT_THE_CANDIDATE
        if leg != WORKING:
            return leg, REASON_MAC_FAILED
        return leg, None

    def _derived(
        self,
        spec: ModelMvpReadingSpec,
        known: dict[str, ModelMvpReading],
        candidate: tuple[MvpState, str],
        manifest: ModelMvpCandidate,
    ) -> ModelMvpReading:
        """NOT WORKING when any input is unreadable or itself NOT WORKING; the first such input is named."""
        reason = "ok"
        detail = ""
        for name in spec.inputs:
            if name == CANDIDATE_INPUT:
                state, _ = candidate
                if state != WORKING:
                    reason, detail = f"input-unreadable:{name}", manifest.error or ""
                    break
                continue
            item = known.get(name)
            if item is None or item.reason == REASON_UNREADABLE:
                reason = f"input-unreadable:{name}"
                detail = item.detail if item else "no such input"
                break
            if item.state != WORKING:
                reason = f"input-not-working:{name}"
                detail = item.reason
                break
        return ModelMvpReading(
            id=spec.id,
            kind="derived",
            state=WORKING if reason == "ok" else NOT_WORKING,
            reason=reason,
            detail=detail,
        )
