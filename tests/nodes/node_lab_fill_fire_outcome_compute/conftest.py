# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared fixtures of the lab-fill fire outcome tests: the policy the tick runs under today, and fire builders."""

from __future__ import annotations

import json

import pytest

from omnimarket.nodes.node_lab_fill_fire_outcome_compute.models import (
    ModelLabFillFireOutcomePolicy,
    ModelLabFillFireOutcomeRequest,
)

FIRE_ID = "20261113T060000Z-lab-fill"

POLICY_VALUES: dict[str, object] = {
    "workflow": "lab-fill",
    "results_version": 1,
    "truncated_marker": "WORKFLOW_RESULT_TRUNCATED",
    "refused_pattern": "hit your .*limit|usage limit reached|out of extra usage",
    "refused_line_max_chars": 160,
    "alert_text_max_chars": 700,
    "no_result_phases": ("failed", "timeout", "abandoned_background"),
    "degraded_exit": 70,
    "invalid_exit": 65,
    "class_truncated": "workflow-result-truncated",
    "class_refused": "lab-fill-claude-refused",
    "class_no_result": "lab-fill-no-result",
    "class_blind": "lab-fill-blind",
    "class_undelivered": "lab-idle-alert-undelivered",
    "class_friction_missing": "lab-idle-friction-missing",
    "truncated_notice_template": (
        "{workflow} degraded: class={degraded_class}; the Workflow result was cut before the agent read it"
        " and no summary was built (full result file: {result_file})"
    ),
    "refused_alert_template": (
        "LAB FILL REFUSED fire={fire_id} the Claude run was refused for a usage limit and dispatched nothing"
        " (free slots unread): {refused_line}"
    ),
    "no_result_alert_template": (
        "LAB FILL NO RESULT fire={fire_id} phase={phase} exit={exit}: the fire wrote no result, so its free slots"
        " and dispatches are unread; run log {run_log}"
    ),
    "idle_alert_template": (
        "floor row stoppage:lab-idle | {cause}free-slots={free_slots} eligible={eligible} dispatched={dispatched}"
        " | full reasons in {latest} | {alert_text}"
    ),
    "snapshot_cause_template": "cause=linear-snapshot-absent:{snapshot_name} | ",
    "eligible_unread": "unread",
}

LATEST_PATH = "/state/lab-fill-latest.json"
WORKFLOW_RESULT_FILE = "/state/results/20261113T060000Z-lab-fill.workflow-result.txt"


@pytest.fixture
def policy() -> ModelLabFillFireOutcomePolicy:
    return ModelLabFillFireOutcomePolicy.model_validate(POLICY_VALUES)


def valid_result(**overrides: object) -> dict[str, object]:
    """A version-1 result a fill that placed lanes writes (the shape node_lab_fill_plan_compute's status handler builds)."""
    value: dict[str, object] = {
        "version": 1,
        "workflow": "lab-fill",
        "fire_id": FIRE_ID,
        "observed_at": "2026-11-13T06:00:00Z",
        "free_slots": 3,
        "dispatched": 2,
        "idle_alarm": False,
        "friction_recorded": False,
        "alert_text": "",
        "session_line": "lab h202:load=0.1/running=2/free=1 approved-work=0",
    }
    value.update(overrides)
    return value


def make_request(
    policy: ModelLabFillFireOutcomePolicy, **overrides: object
) -> ModelLabFillFireOutcomeRequest:
    """A completed fire whose written result file is valid; each test overrides what it varies."""
    fields: dict[str, object] = {
        "policy": policy,
        "fire_id": FIRE_ID,
        "phase": "completed",
        "exit": 0,
        "selftest": False,
        "result_channel_enabled": True,
        "workflow_result_file": WORKFLOW_RESULT_FILE,
        "truncated_marker_seen": False,
        "file_state": "present",
        "file_nonempty": True,
        "file_text": json.dumps(valid_result()),
        "run_log_refused_line": "",
        "run_log_path": "/state/runs/x.log",
        "latest_path": LATEST_PATH,
        "snapshot_name": "linear-2026-11-13.json",
        "snapshot_present": True,
        "alert_exit": None,
    }
    fields.update(overrides)
    return ModelLabFillFireOutcomeRequest.model_validate(fields)


def with_result(**overrides: object) -> dict[str, object]:
    """Request overrides carrying a written result file built from ``valid_result(**overrides)``."""
    return {
        "file_text": json.dumps(valid_result(**overrides)),
        "file_state": "present",
        "file_nonempty": True,
    }
