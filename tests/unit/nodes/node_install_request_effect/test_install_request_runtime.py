# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The install request over the in-memory bus: publish, serve, answer (OMN-19998).

The lane side publishes one draft; the host on the served host decides with the
same handler the handler tests exercise, runs it on a worker thread and
publishes the typed result on the success terminal, keyed by the request id. A
command that is no request is answered on the failure terminal. A held request
(a blocker file on the host) is decided again after the overlay's retry
interval and answers once the blocker is gone. The ``owed`` command is the
offline reading: it reads a published-requests file and the result journal and
reports the installs still owed, exit 1 when one expired.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from omnimarket.delegated_test_loop.lane_bus import LabRunBusError
from omnimarket.models.install_request import (
    EXPIRED_MARK,
    NOT_RUNNING_MARK,
    EnumInstallRequestStatus,
    EnumInstallTarget,
    ModelInstallRequest,
    ModelInstallRequestDraft,
    ModelInstallRequestResult,
)
from omnimarket.nodes.node_install_request_effect.handlers.handler_install_request import (
    HandlerInstallRequest,
    InMemoryResultJournal,
)
from omnimarket.nodes.node_install_request_effect.overlay import (
    ModelInstallHostRow,
    ModelInstallRequestEffectOverlay,
    ModelInstallUnitRow,
)
from omnimarket.nodes.node_install_request_effect.runtime_install_request import (
    InstallRequestHost,
    load_install_request_topics,
    main,
    open_install_request_bus,
    publish_request,
)
from omnimarket.nodes.node_unit_tree_install_effect.models.model_unit_tree_install import (
    ModelUnitTreeInstallRequest,
    ModelUnitTreeInstallResult,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
UNIT_TREE_COMMIT = "4c3985a451847571e2cebd06a6edf1940069946e"


def _overlay(blocker: str) -> ModelInstallRequestEffectOverlay:
    """The example overlay's values, one unit-tree unit, a blocker path of the test's."""
    return ModelInstallRequestEffectOverlay(
        host_name_env="TEST_INSTALL_HOST_NAME",
        default_expiry_minutes=120,
        command_timeout_seconds=600,
        hold_retry_seconds=60,
        max_command_age_seconds=21600,
        output_max_chars=400,
        journal_dir_env="TEST_INSTALL_JOURNAL_DIR",
        journal_file="install-request-results.jsonl",
        hosts=(
            ModelInstallHostRow(
                name="example-host",
                blocker_files=(blocker,),
                units=(
                    ModelInstallUnitRow(
                        name="example-unit",
                        kind=EnumInstallTarget.UNIT_TREE,
                        repositories=("Example-Org/example-repo",),
                        verify_prefixes=(("/usr/bin/env", "example-verify"),),
                        branch_ref_prefix="origin/",
                    ),
                ),
            ),
        ),
    )


class _GoldenRunner:
    """Every command exits 0 and prints the line the request expects."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def __call__(
        self, argv: Sequence[str], timeout: int
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(tuple(argv))
        return subprocess.CompletedProcess(
            args=list(argv), returncode=0, stdout="example-unit ok\n", stderr=""
        )


class _GoldenInstaller:
    """The unit tree installer: one pass, the golden commit, every request kept."""

    def __init__(self) -> None:
        self.requests: list[ModelUnitTreeInstallRequest] = []

    def handle(
        self, request: ModelUnitTreeInstallRequest
    ) -> ModelUnitTreeInstallResult:
        self.requests.append(request)
        return ModelUnitTreeInstallResult(
            operation=request.operation,
            unit=request.unit,
            verdict="pass",
            commit=UNIT_TREE_COMMIT,
        )


def _draft() -> ModelInstallRequestDraft:
    return ModelInstallRequestDraft(
        lane="example-lane",
        host="example-host",
        kind=EnumInstallTarget.UNIT_TREE,
        repository="Example-Org/example-repo",
        ref="omn-19998-example",
        unit="example-unit",
        verify_command=("/usr/bin/env", "example-verify"),
        expected_line="example-unit ok",
    )


def _handler(
    overlay: ModelInstallRequestEffectOverlay, journal: InMemoryResultJournal
) -> HandlerInstallRequest:
    return HandlerInstallRequest(
        overlay=overlay,
        executor_host="example-host",
        unit_tree_installer=_GoldenInstaller(),
        runner=_GoldenRunner(),
        journal=journal,
        now=lambda: NOW,
    )


async def _wait_for(messages: list[dict[str, object]]) -> None:
    """Give the in-memory bus's own delivery the short while it needs."""
    for _ in range(200):
        if messages:
            return
        await asyncio.sleep(0.05)


async def test_a_published_request_is_served_and_its_result_comes_back(
    tmp_path: Path,
) -> None:
    async with open_install_request_bus(
        bus="inmemory", lane=None, kafka_bootstrap=None
    ) as bus:
        overlay = _overlay(blocker=str(tmp_path / "no-such.lock"))
        journal = InMemoryResultJournal()
        host = InstallRequestHost(
            bus, _handler(overlay, journal), overlay, "example-host", now=lambda: NOW
        )
        topics = load_install_request_topics()
        results: list[dict[str, object]] = []

        async def capture(message: Any) -> None:
            results.append(json.loads(message.value))

        await bus.subscribe(topics.success, on_message=capture, group_id="capture")
        await host.start()
        request = await publish_request(bus, _draft(), overlay, now=lambda: NOW)
        await host.drain()

        assert len(results) == 1
        envelope = results[0]
        assert envelope["correlation_id"] == str(request.request_id)
        result = ModelInstallRequestResult.model_validate(envelope["payload"])
        assert result.request_id == request.request_id
        assert result.status is EnumInstallRequestStatus.VERIFIED
        assert result.verify_line == "example-unit ok"
        assert result.revision == UNIT_TREE_COMMIT
        assert journal.get(request.request_id) == result
        await host.stop()


async def test_a_command_that_is_no_request_lands_on_the_failure_topic(
    tmp_path: Path,
) -> None:
    async with open_install_request_bus(
        bus="inmemory", lane=None, kafka_bootstrap=None
    ) as bus:
        overlay = _overlay(blocker=str(tmp_path / "no-such.lock"))
        host = InstallRequestHost(
            bus, _handler(overlay, InMemoryResultJournal()), overlay, "example-host"
        )
        topics = load_install_request_topics()
        failures: list[dict[str, object]] = []

        async def capture(message: Any) -> None:
            failures.append(json.loads(message.value))

        await bus.subscribe(topics.failure, on_message=capture, group_id="capture")
        await host.start()
        await bus.publish(
            topics.command,
            None,
            json.dumps({"payload": {"host": "example-host"}}).encode("utf-8"),
        )
        await host.drain()

        assert len(failures) == 1
        payload = failures[0]["payload"]
        assert isinstance(payload, dict)
        assert payload["status"] == "failed"
        assert "not an install request" in str(payload["error_message"])
        await host.stop()


async def test_a_held_request_answers_once_the_blocker_is_gone(
    tmp_path: Path,
) -> None:
    blocker = tmp_path / "install.lock"
    blocker.write_text("held\n", encoding="utf-8")
    async with open_install_request_bus(
        bus="inmemory", lane=None, kafka_bootstrap=None
    ) as bus:
        overlay = _overlay(blocker=str(blocker)).model_copy(
            update={"hold_retry_seconds": 1}
        )
        host = InstallRequestHost(
            bus, _handler(overlay, InMemoryResultJournal()), overlay, "example-host"
        )
        topics = load_install_request_topics()
        results: list[dict[str, object]] = []

        async def capture(message: Any) -> None:
            results.append(json.loads(message.value))

        await bus.subscribe(topics.success, on_message=capture, group_id="capture")
        await host.start()
        request = await publish_request(bus, _draft(), overlay, now=lambda: NOW)
        await host.drain()
        assert results == []

        blocker.unlink()
        await _wait_for(results)

        assert len(results) == 1
        result = ModelInstallRequestResult.model_validate(results[0]["payload"])
        assert result.request_id == request.request_id
        assert result.status is EnumInstallRequestStatus.VERIFIED
        await host.stop()


async def test_a_declared_kafka_lane_with_no_workspace_root_is_refused_by_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first run of the served host on the dev lane failed on exactly this: a lane declaration is read from a workspace root."""
    monkeypatch.delenv("OMNIBASE_PATH", raising=False)
    with pytest.raises(LabRunBusError, match="workspace root"):
        async with open_install_request_bus(
            bus="kafka", lane="dev", kafka_bootstrap=None
        ):
            pytest.fail("the lane must not open without its declaration")


def _request(expires_in: timedelta) -> ModelInstallRequest:
    """The example draft as a published request, expiring ``expires_in`` from now."""
    return ModelInstallRequest(
        **_draft().model_dump(),
        request_id=uuid.uuid4(),
        requested_at=NOW,
        expires_at=NOW + expires_in,
    )


def _z_stamp(moment: datetime) -> str:
    """The ISO-8601-with-Z stamp the ``--now`` argument takes."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_owed(
    tmp_path: Path,
    request: ModelInstallRequest,
    results_text: str | None,
    now: str,
) -> int:
    """Write the one request (and the journal when given), then read the owed CLI."""
    requests_path = tmp_path / "requests.jsonl"
    requests_path.write_text(request.model_dump_json() + "\n", encoding="utf-8")
    results_path = tmp_path / "results.jsonl"
    if results_text is not None:
        results_path.write_text(results_text, encoding="utf-8")
    return main(
        [
            "owed",
            "--requests",
            str(requests_path),
            "--results",
            str(results_path),
            "--now",
            now,
        ]
    )


def test_owed_names_the_unverified_request_inside_its_expiry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    request = _request(expires_in=timedelta(minutes=120))
    code = _run_owed(tmp_path, request, None, _z_stamp(NOW))

    assert code == 0
    out = capsys.readouterr().out
    assert str(request.request_id) in out
    assert NOT_RUNNING_MARK in out
    assert out.endswith("owed=1 expired=0\n")


def test_owed_exits_1_when_an_owed_request_is_past_its_expiry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    request = _request(expires_in=timedelta(minutes=120))
    code = _run_owed(
        tmp_path,
        request,
        None,
        _z_stamp(request.expires_at + timedelta(minutes=1)),
    )

    assert code == 1
    out = capsys.readouterr().out
    assert out.startswith(EXPIRED_MARK)
    assert out.endswith("owed=1 expired=1\n")


def test_a_verified_result_clears_the_request_even_past_its_expiry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    request = _request(expires_in=timedelta(minutes=120))
    result = ModelInstallRequestResult(
        request_id=request.request_id,
        host=request.host,
        unit=request.unit,
        status=EnumInstallRequestStatus.VERIFIED,
        at=NOW + timedelta(minutes=5),
        revision=UNIT_TREE_COMMIT,
        verify_line=request.expected_line,
    )
    code = _run_owed(
        tmp_path,
        request,
        result.model_dump_json() + "\n",
        _z_stamp(request.expires_at + timedelta(minutes=1)),
    )

    assert code == 0
    assert capsys.readouterr().out.endswith("owed=0 expired=0\n")


def test_a_results_line_that_is_no_json_is_indeterminate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    request = _request(expires_in=timedelta(minutes=120))
    code = _run_owed(tmp_path, request, "not json\n", _z_stamp(NOW))

    captured = capsys.readouterr()
    assert code == 3
    assert captured.err.startswith("INDETERMINATE:")
    assert captured.out == ""
