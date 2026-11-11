# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared fixtures of the piece 11 reading run tests: an overlay, good facts, a clock."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from omnimarket.models.model_dev_deploy_reading import (
    ModelLabPassReceiptFact,
    ModelRuntimeMerge,
    ModelRuntimeProbeFact,
)
from omnimarket.nodes.node_dev_deploy_reading_effect.adapters import (
    InMemoryDevDeployFacts,
)
from omnimarket.nodes.node_dev_deploy_reading_effect.handlers.handler_dev_deploy_reading_run import (
    HandlerDevDeployReadingRun,
)
from omnimarket.nodes.node_dev_deploy_reading_effect.models.model_dev_deploy_reading_run import (
    ModelDevDeployFacts,
)
from omnimarket.nodes.node_dev_deploy_reading_effect.overlay import (
    ModelDevDeployReadingOverlay,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)

RUN_AT = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
HOST = "test-host"
LANE = "lane-a"
SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_DRILL = "d" * 40


def at(hhmm: str) -> datetime:
    hour, minute = hhmm.split(":")
    return datetime(2026, 11, 8, int(hour), int(minute), tzinfo=UTC)


def document(**changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "ledger_append": {
            "command_env": "TEST_APPEND_COMMAND",
            "host_env": "TEST_HOST",
            "timeout_seconds": 5,
        },
        "publish": {
            "lane": "dev-deploy-reading",
            "actor": "node:node_dev_deploy_reading_effect",
            "piece": 11,
            "publish_max_age_seconds": 7200,
        },
        "thresholds": {
            "required_lane": LANE,
            "receipt_deadline_seconds": 3600,
            "probe_max_age_seconds": 86400,
        },
        "merges": {
            "clone_path_env": "TEST_CLONE",
            "ref": "origin/dev",
            "lookback_seconds": 86400,
            "runtime_path_globs": ["src/**", "docker/*.yml"],
            "git_timeout_seconds": 5,
        },
        "lineage": {
            "receipt_covers_descendant": False,
            "probe_accepts_descendant": False,
            "max_commits_between": 50,
            "receipt_cover_window_seconds": 86400,
        },
        "receipts": {
            "projection_url": "http://projection.test/rows",
            "timeout_seconds": 5,
        },
        "probe": {
            "health_url": "http://runtime.test/health",
            "health_ok_path": ["details", "healthy"],
            "ssh_host_env": "TEST_SSH_HOST",
            "compose_project": "test-project",
            "provenance_service": "test-runtime",
            "provenance_path": "/app/build-provenance.json",
            "sha_path": ["vcs_ref"],
            "timeout_seconds": 5,
        },
        "source_rule": "lab_pass_receipt",
        "deploy_agent": {
            "health_url": "http://agent.test/health",
            "queue_url": "http://agent.test/queue",
            "timeout_seconds": 5,
            "idle_grace_seconds": 7200,
            "provenance_section": "build_context",
            "source_repo": "repo-a",
            "merge_repos": [
                {
                    "source_repo": "repo-a",
                    "clone_path_env": "TEST_CLONE",
                    "ref": "origin/dev",
                    "provenance_section": "build_context",
                },
                {
                    "source_repo": "repo-b",
                    "clone_path_env": "TEST_CLONE",
                    "ref": "origin/dev",
                    "provenance_section": "siblings",
                },
            ],
        },
        "drill_fault": {"synthetic_merge_sha": SHA_DRILL, "age_seconds": 7200},
        "schedule_units": {
            "unit_name": "test-reading",
            "drill_unit_name": "test-reading-drill",
        },
        "injected_fault": "Drop the lane's PASS receipt for a runtime-affecting merge",
    }
    base.update(changes)
    return base


def overlay(**changes: Any) -> ModelDevDeployReadingOverlay:
    return ModelDevDeployReadingOverlay.model_validate(document(**changes))


def good_facts() -> ModelDevDeployFacts:
    """One runtime-affecting merge with its PASS receipt, a non-runtime merge and a fresh probe that saw the merge."""
    return ModelDevDeployFacts(
        merges=(
            ModelRuntimeMerge(sha=SHA_A, merged_at=at("09:00"), runtime_affecting=True),
            ModelRuntimeMerge(
                sha=SHA_B, merged_at=at("10:30"), runtime_affecting=False
            ),
        ),
        receipts=(
            ModelLabPassReceiptFact(
                sha=SHA_A, lane=LANE, result="PASS", finished_at=at("09:30")
            ),
        ),
        probes=(
            ModelRuntimeProbeFact(
                probe_id="probe-1",
                probed_at=at("11:50"),
                expected_sha=SHA_A,
                observed_sha=SHA_A,
                ok=True,
            ),
        ),
    )


def source(facts: ModelDevDeployFacts | None = None) -> InMemoryDevDeployFacts:
    return InMemoryDevDeployFacts(facts or good_facts())


def handler(
    test_source: InMemoryDevDeployFacts,
    ledger: InMemoryLedger | None,
    test_overlay: ModelDevDeployReadingOverlay | None = None,
    drill: bool = False,
) -> HandlerDevDeployReadingRun:
    return HandlerDevDeployReadingRun(
        overlay=test_overlay or overlay(),
        source=test_source,
        appender=ledger,
        clock=lambda: RUN_AT,
        host=HOST,
        drill=drill,
    )
