# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane guard a bus host must have installed (OMN-17427).

On 2026-10-18 to 2026-10-20 one lab host had no ``lane_guard_host.py`` installed, so every bus unit placed there exited 78 LANE-GUARD-MISSING
in 0 s (at least 7 times) while its idle load kept it the freest host. The serve process now advertises a ``lane-guard:<sha256>``
entry in its tools for each installed guard whose bytes hash to its directory name (omnimarket ``local_host_reader``), and a host is
admitted for a bus unit only when it advertises the launcher's own guard.

An advertisement with no ``lane-guard:`` entry at all comes from a serve that predates the entry (omnimarket#4276 not yet deployed),
and it is admitted as not yet upgraded: refusing it refused every pool host on 2026-10-21 once a plugin release was installed
ahead of the serves. Execution stays guarded there, because the unit's stub exits 78 LANE-GUARD-MISSING when the guard file is
absent. Only a host that advertises guards and not this one is refused. TEMPORARY: the follow-up ticket named in the contract removes
this tolerance once every pool host advertises its guards.
"""

from ..models.model_lane_guard_admission import (
    ModelLaneGuardAdmissionRequest,
    ModelLaneGuardAdmissionVerdict,
)

LANE_GUARD_TOOL_PREFIX = "lane-guard:"


class HandlerLaneGuardAdmission:
    def handle(
        self, request: ModelLaneGuardAdmissionRequest
    ) -> ModelLaneGuardAdmissionVerdict:
        installed = tuple(
            sorted(
                t[len(LANE_GUARD_TOOL_PREFIX) :]
                for t in request.tools
                if t.startswith(LANE_GUARD_TOOL_PREFIX)
            )
        )
        missing = (
            request.bus and bool(installed) and request.expected_sha not in installed
        )
        return ModelLaneGuardAdmissionVerdict(
            installed=installed,
            refusal=f"lane guard {request.expected_sha[:8]} not installed"
            if missing
            else None,
        )
