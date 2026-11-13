# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24124: join J2 does not probe a host the host table parks, and a failed run says which host failed and why.

Measured on the served board of 2026-11-13: the publish cycle's join_j2 step failed every cycle with ``exit 1`` and an empty
diagnostic. The overlay lists h106; the host table parks it (operator, 2026-11-12: the laptop moves to the basement), so
placement refused it typed every run and ``all_hosts_golden`` read the run as failed. The run's failure lines went to stdout and
the publisher keeps only the stderr tail, so the cycle's log named nothing.

Golden: a parked host is skipped with a line naming the table's reason and the other hosts are probed. Error chains: an
unreadable table probes every host (nothing is hidden); a failed run writes one stderr line naming each failed host and its typed
failure, and the publisher's diagnostic carries exactly that line (the seam between the two). Control: a host the table does not
park is still probed, and a run whose only hosts all failed still exits 1.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from omnimarket.models.model_host_reading import ModelCanaryHost
from omnimarket.models.model_join_j2 import EnumJoinJ2Outcome, ModelJoinJ2HostResult
from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_factory_floor_publish import (
    HandlerFactoryFloorPublish,
)
from omnimarket.nodes.node_host_reading_effect import runtime_host_reading
from omnimarket.nodes.node_host_reading_effect.adapters.canary_route import (
    InMemoryHostInventory,
)
from tests.nodes.node_factory_floor_publish_effect.test_factory_floor_publish import (
    PLUGIN,
    Scripted,
    _refresh_request,
)
from tests.nodes.node_factory_floor_publish_effect.test_join_j2_step import _FakeCaller

pytestmark = pytest.mark.unit

TABLE = (
    ModelCanaryHost(name="h051", pooled=True),
    ModelCanaryHost(
        name="h106", pooled=False, reason="moving to the basement, operator 2026-11-12"
    ),
)


def _split(
    hosts: tuple[str, ...], inventory: InMemoryHostInventory
) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...], str]:
    from omnimarket.nodes.node_host_reading_effect.handlers.join_j2_hosts import (
        split_probed_and_parked,
    )

    return split_probed_and_parked(hosts, inventory)


def test_golden_a_parked_host_is_skipped_with_the_tables_reason() -> None:
    probed, parked, warning = _split(
        ("h051", "h106", "h201"), InMemoryHostInventory(TABLE)
    )
    assert probed == (
        "h051",
        "h201",
    )  # h201 is not in the table: probed, placement refuses it typed
    assert parked == (("h106", "moving to the basement, operator 2026-11-12"),)
    assert warning == ""


def test_error_an_unreadable_table_probes_every_host() -> None:
    probed, parked, warning = _split(
        ("h051", "h106"), InMemoryHostInventory(TABLE, error="no host table")
    )
    assert probed == ("h051", "h106")
    assert parked == ()
    assert "host table unread" in warning
    assert "ValueError" in warning


def _failed(host: str, code: str, detail: str) -> ModelJoinJ2HostResult:
    return ModelJoinJ2HostResult(
        host=host,
        correlation_id="c" * 32,
        outcome=EnumJoinJ2Outcome.NO_RESULT,
        failure_code=code,
        detail=detail,
    )


def test_golden_the_failure_report_names_each_failed_host_and_its_code() -> None:
    from omnimarket.nodes.node_host_reading_effect.handlers.join_j2_hosts import (
        failure_report,
    )

    golden = ModelJoinJ2HostResult(
        host="h051",
        correlation_id="a" * 32,
        outcome=EnumJoinJ2Outcome.GOLDEN,
    )
    report = failure_report(
        (golden, _failed("h201", "seam_no_result", "EventTopicAuthorizationError"))
    )
    assert report is not None
    assert "\n" not in report
    assert report.startswith("J2-FAILED ")
    assert "h201" in report
    assert "seam_no_result" in report
    assert "EventTopicAuthorizationError" in report
    assert "h051" not in report
    assert failure_report((golden,)) is None
    # A placement refusal's detail is several lines; the publisher keeps the last three stderr lines, so the report is one.
    multi = failure_report(
        (
            _failed(
                "h202",
                "seam_placement_refused",
                "placement: refused (over_capacity)\n  h051: not the requested host\n  h053: not the requested host",
            ),
        )
    )
    assert multi is not None
    assert "\n" not in multi
    assert "over_capacity) h051: not the requested host" in multi


def test_the_publisher_diagnostic_carries_the_runners_failure_line(
    tmp_path: Path,
) -> None:
    from omnimarket.nodes.node_host_reading_effect.handlers.join_j2_hosts import (
        failure_report,
    )

    report = failure_report(
        (_failed("h201", "seam_no_result", "no grant on the command topic"),)
    )
    assert report is not None
    runner = Scripted({"--json": (0, b"[]", b""), "join-j2": (1, b"", report.encode())})
    result = HandlerFactoryFloorPublish(runner).handle(
        _refresh_request(tmp_path), environ=PLUGIN
    )
    step = next(s for s in result.steps if s.name == "join_j2")
    assert (step.status, step.detail) == ("failed", "exit 1")
    assert step.diagnostic == report
    assert "seam_no_result" in step.diagnostic


def test_control_a_runner_that_says_nothing_leaves_the_diagnostic_empty(
    tmp_path: Path,
) -> None:
    runner = Scripted({"--json": (0, b"[]", b""), "join-j2": (1, b"", b"")})
    result = HandlerFactoryFloorPublish(runner).handle(
        _refresh_request(tmp_path), environ=PLUGIN
    )
    step = next(s for s in result.steps if s.name == "join_j2")
    assert step.diagnostic == ""


def _run_main(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    *,
    results: tuple[ModelJoinJ2HostResult, ...],
) -> tuple[int, list[tuple[str, ...]], str, str]:
    seen: list[tuple[str, ...]] = []

    class _FakeJoin:
        def __init__(self, **_: Any) -> None:
            pass

        async def run_hosts(
            self, names: tuple[str, ...], *, deadline_seconds: float | None = None
        ) -> tuple[ModelJoinJ2HostResult, ...]:
            seen.append(tuple(names))
            return results

    @asynccontextmanager
    async def _bus(**_: Any) -> Any:
        yield object()

    overlay = SimpleNamespace(
        join_j2=SimpleNamespace(
            settings=SimpleNamespace(result_wait_slack_seconds=1),
            emission_home_env="J2_PARKED_TEST_HOME",
            emission_journal_relpath="j2.jsonl",
            collect_window_seconds=0,
            hosts=("h051", "h106"),
        )
    )
    monkeypatch.setattr(runtime_host_reading, "load_overlay", lambda: overlay)
    monkeypatch.setattr(runtime_host_reading, "open_lab_run_bus", _bus)
    monkeypatch.setattr(runtime_host_reading, "LabWorkCaller", _FakeCaller)
    monkeypatch.setattr(runtime_host_reading, "JoinJ2", _FakeJoin)
    monkeypatch.setattr(
        runtime_host_reading,
        "HostTableInventory",
        lambda _env: InMemoryHostInventory(TABLE),
    )
    monkeypatch.setenv("J2_PARKED_TEST_HOME", str(tmp_path))
    code = runtime_host_reading.main(["join-j2", "--bus", "kafka"])
    captured = capsys.readouterr()
    return code, seen, captured.out, captured.err


def test_golden_the_run_probes_only_the_unparked_host_and_exits_zero_when_it_is_golden(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    golden = ModelJoinJ2HostResult(
        host="h051", correlation_id="a" * 32, outcome=EnumJoinJ2Outcome.GOLDEN
    )
    code, seen, out, err = _run_main(monkeypatch, tmp_path, capsys, results=(golden,))
    assert seen == [("h051",)]
    assert code == 0
    assert "J2-SKIP host=h106 reason=parked-in-host-table" in out
    assert "basement" in out
    assert err == ""


def test_error_a_failed_host_exits_one_and_names_itself_on_stderr(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    failed = _failed("h051", "seam_no_result", "no grant")
    code, seen, out, err = _run_main(monkeypatch, tmp_path, capsys, results=(failed,))
    assert seen == [("h051",)]
    assert code == 1
    assert err.startswith("J2-FAILED ")
    assert "h051" in err
    assert "seam_no_result" in err
    assert "J2-SKIP host=h106" in out
