# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane census snapshot's contract, re-exported from ``omnimarket.models.lane_census_event`` (OMN-24425).

The code is written once, in that module; this module keeps the names the alarm verbs import. It was moved unchanged from ``scripts/lane_census_event.py`` (``validate_event``, ``_alert_key``, the drift-event key
set and the supported schema versions) and ``scripts/lane_census_plan.py`` (``FINDING_KIND_SEVERITY``, the one
severity of every finding kind the census may carry). The lane_drift and container_unhealthy verbs read the
census snapshot through these. Parity with the old copies is pinned by
``tests/nodes/node_lab_alarm/test_lab_alarm_lane_census_parity.py``, whose expected values were computed by
running the old functions.
"""

from omnimarket.models.lane_census_event import (
    DRIFT_EVENT_FIELDS,
    FINDING_FIELDS,
    FINDING_KIND_SEVERITY,
    SUPPORTED_EVENT_SCHEMA_VERSIONS,
    alert_key,
    validate_event,
)

__all__ = [
    "DRIFT_EVENT_FIELDS",
    "FINDING_FIELDS",
    "FINDING_KIND_SEVERITY",
    "SUPPORTED_EVENT_SCHEMA_VERSIONS",
    "alert_key",
    "validate_event",
]
