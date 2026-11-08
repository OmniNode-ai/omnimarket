# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tessl review findings on the J2 join (OMN-22907), each pinned by a test that failed before the fix.

* the join's settings accept a commit sha and a timeout that the work-unit request then refuses;
* a send that raises after the seam chain started left that chain with a start and no end;
* the join-j2 command on the in-memory bus has no advertiser and read every host as
  ``no_advertisement`` instead of saying so.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.models.model_host_capacity_advertisement import (
    ModelHostCapacityAdvertisement,
)
from omnimarket.models.model_join_j2 import (
    EnumJoinJ2Outcome,
    ModelJoinJ2Settings,
)
from omnimarket.nodes.node_host_reading_effect import runtime_host_reading
from omnimarket.nodes.node_host_reading_effect.handlers.join_j2 import JoinJ2

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
_FIELDS: dict[str, Any] = {
    "canary_piece": 2,
    "seam_piece": 3,
    "canary_node": "node_host_reading_effect",
    "seam_node": "node_lab_placement_compute",
    "repo": "OmniNode-ai/omnimarket",
    "commit_sha": "4c3985a451847571e2cebd06a6edf1940069946e",
    "argv": ("python3", "-c", "print('J2-CANARY {nonce}')"),
    "result_marker": "J2-CANARY",
    "kind": "other",
    "lane": "host-canary",
    "command_timeout_seconds": 1,
    "result_wait_slack_seconds": 1,
    "advert_max_age_seconds": 60,
    "need_tools": ("git",),
}


def _settings(**overrides: Any) -> ModelJoinJ2Settings:
    return ModelJoinJ2Settings(**{**_FIELDS, **overrides})


def test_settings_refuse_a_commit_sha_the_work_unit_request_would_refuse() -> None:
    with pytest.raises(ValidationError, match="commit_sha"):
        _settings(commit_sha="abcdef1")


def test_settings_refuse_a_timeout_above_the_work_unit_limit() -> None:
    with pytest.raises(ValidationError, match="command_timeout_seconds"):
        _settings(command_timeout_seconds=7201)


def test_settings_keep_a_full_sha_and_the_limit_itself() -> None:
    assert _settings(command_timeout_seconds=7200).command_timeout_seconds == 7200


class _Placement:
    placed = True
    host_name = "h-a"

    def describe(self) -> str:
        return "placed"


class _SendRaises:
    """A caller that advertises and places the host, then fails to send."""

    def advertisements(self) -> list[ModelHostCapacityAdvertisement]:
        return [
            ModelHostCapacityAdvertisement(
                host_name="h-a",
                advertised_at=NOW,
                cores=8,
                load1=0.5,
                mem_available_bytes=32 * 1024**3,
                cadence_seconds=60,
                tools=("git",),
            )
        ]

    def place(self, **_: Any) -> _Placement:
        return _Placement()

    async def dispatch(self, request: Any) -> Any:
        raise RuntimeError("bus publish refused")


def test_a_send_that_raises_ends_the_seam_chain_with_a_typed_failure() -> None:
    sink = InMemoryEmissionSink()
    join = JoinJ2(
        settings=_settings(),
        caller=_SendRaises(),
        sink=sink,
        backend="in_memory",
        clock=lambda: NOW,
    )
    results = asyncio.run(join.run_hosts(("h-a",)))
    assert results[0].outcome is EnumJoinJ2Outcome.NO_RESULT
    assert "bus publish refused" in results[0].detail
    seam = [
        (e.phase, e.failure_code)
        for e in sink.events
        if e.correlation_id == results[0].correlation_id and e.piece == 3
    ]
    assert seam == [("start", None), ("failure", "seam_no_result")]


def test_join_j2_on_the_in_memory_bus_says_it_has_no_advertiser(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Any,
) -> None:
    overlay = SimpleNamespace(
        join_j2=SimpleNamespace(
            settings=_settings(),
            emission_home_env="J2_TEST_EMISSION_HOME",
            emission_journal_relpath="j2.jsonl",
            collect_window_seconds=0,
        )
    )
    monkeypatch.setattr(runtime_host_reading, "load_overlay", lambda: overlay)
    monkeypatch.setenv("J2_TEST_EMISSION_HOME", str(tmp_path))
    code = runtime_host_reading.main(["join-j2", "--bus", "inmemory", "--host", "h-a"])
    captured = capsys.readouterr()
    assert code == 1
    assert "J2-BUS-FAILED" in captured.err
    assert "advertiser" in captured.err
    assert "J2 host=" not in captured.out
