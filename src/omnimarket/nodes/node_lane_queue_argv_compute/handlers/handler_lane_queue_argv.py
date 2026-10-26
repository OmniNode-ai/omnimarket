# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The remote-lane runner's own command for a lane again, for the lane queue (OMN-19998, moved here from the runner, OMN-17427).

The run's inputs, with the brief the run pinned and without ``--host``, whose pin is the queue entry's (``lane_queue_pin`` ages it
out, so a relaunch after the pin's term asks any admitting host). Flags the run did not set are left out.
"""

from __future__ import annotations

from ..models.model_lane_queue_argv import (
    ModelLaneQueueArgvRequest,
    ModelLaneQueueArgvResult,
)

VALUE_FLAGS = (
    ("--ticket", "ticket"),
    ("--parent", "parent"),
    ("--priority-class", "priority_class"),
    ("--repo", "repo"),
    ("--owner", "owner"),
    ("--effort", "effort"),
    ("--risk-class", "risk_class"),
    ("--timeout-min", "timeout_min"),
    ("--mem-max", "mem_max"),
    ("--cpu-quota", "cpu_quota"),
    ("--max-turns", "max_turns"),
    ("--session-env", "session_env"),
    ("--placement", "placement"),
)
LIST_FLAGS = (
    ("--pr", "pr"),
    ("--cause", "cause"),
    ("--plugin", "plugin"),
    ("--writable", "writable"),
    ("--check", "check"),
    ("--context", "context"),
    ("--require-capability", "require_capability"),
)
SWITCHES = (
    ("--require-kb", "require_kb"),
    ("--no-plugins", "no_plugins"),
    ("--lane-claim", "lane_claim"),
)


class HandlerLaneQueueArgv:
    def handle(self, request: ModelLaneQueueArgvRequest) -> ModelLaneQueueArgvResult:
        given = request.inputs
        argv = [
            "run",
            "--detach",
            "--no-placement-wait",
            "--brief",
            request.brief,
            "--lane",
            str(given["lane"]),
            "--model",
            str(given["model"]),
        ]
        for flag, name in VALUE_FLAGS:
            value = given.get(name)
            if value not in (None, "", []):
                argv += [flag, str(value)]
        if given.get("repo"):
            for flag, name in (("--sha", "sha"), ("--ref", "ref")):
                if given.get(name):
                    argv += [flag, str(given[name])]
                    break
        for flag, name in LIST_FLAGS:
            for value in given.get(name) or []:
                argv += [flag, str(value)]
        argv += [flag for flag, name in SWITCHES if given.get(name)]
        return ModelLaneQueueArgvResult(tuple(argv))
