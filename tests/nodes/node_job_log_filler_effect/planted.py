# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Planted PR watcher state and overlay for the job-log filler tests (OMN-23032)."""

from __future__ import annotations

from pathlib import Path

from omnimarket.nodes.node_job_log_filler_effect.models.model_job_log_filler import (
    ModelJobLogFillerBusProfile,
    ModelJobLogFillerOverlay,
)
from tests.nodes.node_github_repo_gateway_effect.recorded_transport import (
    BytesExchange,
    RecordedGatewayTransport,
    get,
)

OWNER = "OmniNode-ai"
REPO = f"{OWNER}/omnimarket"
HEAD = "a" * 40
RED_JOB = 128000000001
OTHER_RED_JOB = 128000000002
SUMMARY_JOB = 128000000003
GREEN_JOB = 128000000004
STALE_JOB = 128000000005
RED_LOG = b"setup\n##[error]Process completed with exit code 1.\n"


def log_path(job: int, repo: str = REPO) -> str:
    return f"/repos/{repo}/actions/jobs/{job}/logs"


def overlay(**changes: object) -> ModelJobLogFillerOverlay:
    base: dict[str, object] = {
        "github_owner": OWNER,
        "watcher_state_env": "PLANTED_PR_WATCHER_STATE",
        "pr_classes": ("red", "pending"),
        "always_job_names": ("CI Summary",),
        "max_reads_per_pass": 10,
        "reader_concurrency": 1,
        "reply_tail_bytes": 4096,
        "reply_timeout_seconds": 5.0,
        "default_profile": "inmemory",
        "profiles": {
            "inmemory": ModelJobLogFillerBusProfile(kind="inmemory"),
            "redpanda": ModelJobLogFillerBusProfile(kind="kafka", lane="example-lane"),
        },
    }
    return ModelJobLogFillerOverlay.model_validate({**base, **changes})


def _record(
    number: int,
    cls: str,
    *,
    red: list[str],
    detail: list[list[object]],
    sha: str = HEAD,
    draft: bool = False,
    state: str = "OPEN",
    ci_sha: str | None = None,
) -> dict[str, object]:
    return {
        "cls": cls,
        "facts": {
            "repo": "omnimarket",
            "number": number,
            "state": state,
            "draft": draft,
            "head_sha": sha,
        },
        "ci": {"sha": ci_sha or sha, "red": red, "detail": detail},
    }


def watcher_state() -> dict[str, object]:
    """One red PR with a red job, a named job and a green job; plus runs that must not be picked."""
    return {
        "last_tick": "2026-11-07T22:49:59Z",
        "prs": {
            "omnimarket#1": _record(
                1,
                "red",
                red=["Tests Gate"],
                detail=[
                    ["Tests Gate", str(RED_JOB), "2026-11-07T22:00:00Z"],
                    [
                        "Tests Gate",
                        "127999999999",
                        "2026-11-07T21:00:00Z",
                    ],  # an earlier attempt
                    ["CI Summary", str(SUMMARY_JOB), "2026-11-07T22:01:00Z"],
                    ["lint", str(GREEN_JOB), "2026-11-07T22:02:00Z"],
                ],
            ),
            # a draft, a merged PR, a class outside the overlay and a stale CI record
            "omnimarket#2": _record(
                2, "red", red=["x"], detail=[["x", "9", "t"]], draft=True
            ),
            "omnimarket#3": _record(
                3, "merged", red=["x"], detail=[["x", "9", "t"]], state="MERGED"
            ),
            "omnimarket#4": _record(
                4, "held-excluded", red=["x"], detail=[["x", "9", "t"]]
            ),
            "omnimarket#5": _record(
                5,
                "red",
                red=["x"],
                detail=[["x", str(STALE_JOB), "t"]],
                ci_sha="b" * 40,
            ),
        },
    }


def transport_for(
    *jobs: int, bodies: dict[int, tuple[int, bytes]] | None = None
) -> RecordedGatewayTransport:
    """A recorded GitHub: each listed job's log is served once, in order."""
    bodies = bodies or {}
    exchanges = []
    for job in jobs:
        status, body = bodies.get(job, (200, RED_LOG))
        exchanges.append(
            BytesExchange(get(log_path(job)), status, body, 64 * 1024 * 1024, "window")
        )
    return RecordedGatewayTransport(exchanges)


def write_state(tmp_path: Path) -> Path:
    import json

    path = tmp_path / "pr-watcher-state.json"
    path.write_text(json.dumps(watcher_state()), encoding="utf-8")
    return path
