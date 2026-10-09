# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: a committed judged run is replayed into events and delivered to the emit daemon.

The chain is the two nodes end to end in process: HandlerDelegationAcceptanceJudgedReplay builds
the events of a committed judgments.jsonl and summary.json, and
HandlerDelegationAcceptanceJudgedPublish sends each one over a Unix socket to a daemon that
answers the emit protocol. Every counted item arrives once, under the event type, with the id the
projection will dedupe on; a second replay delivers the same ids.
"""

from __future__ import annotations

import json
import socket
import threading
from pathlib import Path
from typing import Any

import pytest

from omnimarket.models.delegation_acceptance_judge.enum_acceptance_publish_status import (
    EnumAcceptancePublishStatus,
)
from omnimarket.models.delegation_acceptance_judge.model_acceptance_publish_request import (
    ModelAcceptancePublishRequest,
)
from omnimarket.models.delegation_acceptance_judge.model_acceptance_replay_request import (
    ModelAcceptanceReplayRequest,
)
from omnimarket.nodes.node_delegation_acceptance_judged_publish_effect.handlers.handler_delegation_acceptance_judged_publish import (
    EVENT_TYPE,
    HandlerDelegationAcceptanceJudgedPublish,
)
from omnimarket.nodes.node_delegation_acceptance_judged_replay_compute.handlers.handler_delegation_acceptance_judged_replay import (
    HandlerDelegationAcceptanceJudgedReplay,
)

pytestmark = pytest.mark.unit

FIXTURES = (
    Path(__file__).resolve().parents[1]
    / "node_delegation_acceptance_judged_replay_compute"
    / "fixtures"
)


def _replay_request() -> ModelAcceptanceReplayRequest:
    return ModelAcceptanceReplayRequest.model_validate(
        {
            "rows": [
                json.loads(line)
                for line in (FIXTURES / "judgments_sample.jsonl")
                .read_text()
                .splitlines()
            ],
            "summary": json.loads((FIXTURES / "summary_sample.json").read_text()),
            "tenant_id": "820272f9-4aaf-5add-a2df-0af942852ab2",
            "tier_by_model": {"Qwen3.8-27B": "local", "glm-5.3": "cheap_cloud"},
            "judge_run_id": "golden-chain-run",
            "judge_model": "synthetic-judge",
            "judge_model_version": "high-effort",
            "rubric_id": "delegation-acceptance-judge",
            "rubric_version": "v1",
            "rubric_hash": "sha256:" + "c" * 64,
            "calibration_run_id": "golden-chain-calibration",
            "judged_at": "2026-10-03T18:45:33+00:00",
        }
    )


def _serve(path: str, count: int, seen: list[dict[str, Any]]) -> threading.Thread:
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(1)

    def run() -> None:
        conn, _ = server.accept()
        with conn, conn.makefile("rwb") as stream:
            for _ in range(count):
                seen.append(json.loads(stream.readline()))
                stream.write(b'{"status": "queued", "event_id": "d"}\n')
                stream.flush()
        server.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def _replay_to_daemon(path: str) -> list[dict[str, Any]]:
    built = HandlerDelegationAcceptanceJudgedReplay().handle(_replay_request())
    assert built.status is EnumAcceptancePublishStatus.COMPLETED
    seen: list[dict[str, Any]] = []
    thread = _serve(path, len(built.events), seen)
    published = HandlerDelegationAcceptanceJudgedPublish().handle(
        ModelAcceptancePublishRequest(events=built.events, socket_path=path)
    )
    thread.join(timeout=5)
    assert published.status is EnumAcceptancePublishStatus.COMPLETED
    assert published.published_count == len(built.events)
    return seen


def test_golden_chain_every_counted_item_reaches_the_daemon_once(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    path = str(tmp_path_factory.mktemp("g", numbered=True) / "e.sock")
    seen = _replay_to_daemon(path)
    assert len(seen) == 4
    assert {r["event_type"] for r in seen} == {EVENT_TYPE}
    ids = [r["payload"]["event_id"] for r in seen]
    assert len(set(ids)) == 4


def test_golden_chain_a_second_replay_delivers_the_same_event_ids(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    first = _replay_to_daemon(
        str(tmp_path_factory.mktemp("g1", numbered=True) / "e.sock")
    )
    second = _replay_to_daemon(
        str(tmp_path_factory.mktemp("g2", numbered=True) / "e.sock")
    )
    assert first == second
