# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The install request over the in-memory bus: publish, serve, answer, owe (OMN-19998).

The lane side publishes one draft under its login -- the envelope's producer,
the fact the host's overlay row checks -- and the host on the served host
decides with the same handler the handler tests exercise, runs it on a worker
thread and publishes the typed result on the success terminal, keyed by the
request id. A command that carries no envelope (a bare request) is answered on
the failure terminal and never reaches the handler, and a command whose payload
is no request is answered there too; a request from a login the host does not
allow is answered ``refused`` for its own id and never journalled. The
``read_owed`` reading subscribes to the command topic and the success terminal
itself and reports the installs still owed from the bus alone: a request with
no result is listed, a verified result clears it, a request past its expiry
owns the expired line, and a forged producer and a bare request are never
counted. The ``owed`` command takes the same bus arguments as serve and
publish, prints one line per owed install and exits 1 when one expired.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import subprocess
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.delegated_test_loop.lab_run_bus import envelope_bytes_for
from omnimarket.delegated_test_loop.lane_bus import LabRunBusError
from omnimarket.models.install_request import (
    EXPIRED_MARK,
    NOT_RUNNING_MARK,
    EnumInstallRequestStatus,
    EnumInstallTarget,
    ModelInstallOwedLine,
    ModelInstallOwedReading,
    ModelInstallRequest,
    ModelInstallRequestDraft,
    ModelInstallRequestReceived,
    ModelInstallRequestResult,
)
from omnimarket.nodes.node_install_request_effect import runtime_install_request
from omnimarket.nodes.node_install_request_effect.handlers.handler_install_request import (
    HandlerInstallRequest,
    InMemoryResultJournal,
)
from omnimarket.nodes.node_install_request_effect.models.model_install_request_effect import (
    ModelInstallRequestHandled,
)
from omnimarket.nodes.node_install_request_effect.overlay import (
    NODE_NAME,
    ModelInstallHostRow,
    ModelInstallRepositoryRow,
    ModelInstallRequestEffectOverlay,
    ModelInstallUnitRow,
    ModelInstallVerifyRow,
)
from omnimarket.nodes.node_install_request_effect.runtime_install_request import (
    InstallRequestHost,
    load_install_request_topics,
    main,
    open_install_request_bus,
    publish_request,
    read_owed,
)
from omnimarket.nodes.node_unit_tree_install_effect.models.model_unit_tree_install import (
    ModelUnitTreeInstallRequest,
    ModelUnitTreeInstallResult,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
NEW_COMMIT = "4c3985a451847571e2cebd06a6edf1940069946e"
MIRROR = "/srv/example-host/mirrors/example-repo"
REF = "omn-19998-example"
ALLOWED_PUBLISHER = "example-lane-bot"


def _overlay(
    blocker: str = "/tmp/omn-19998-no-such-dir/install.lock",
) -> ModelInstallRequestEffectOverlay:
    """The example overlay's values, with a blocker path no test file sits at."""
    return ModelInstallRequestEffectOverlay(
        host_name_env="TEST_INSTALL_HOST_NAME",
        default_expiry_minutes=120,
        command_timeout_seconds=600,
        hold_retry_seconds=60,
        max_command_age_seconds=21600,
        output_max_chars=400,
        journal_dir_env="TEST_INSTALL_JOURNAL_DIR",
        journal_file="install-request-results.jsonl",
        publisher_login_env="TEST_INSTALL_PUBLISHER_LOGIN",
        owed_quiet_seconds=1,
        git_command=("/usr/bin/git",),
        hosts=(
            ModelInstallHostRow(
                name="example-host",
                blocker_files=(blocker,),
                allowed_publishers=(ALLOWED_PUBLISHER,),
                units=(
                    ModelInstallUnitRow(
                        name="example-unit",
                        kind=EnumInstallTarget.UNIT_TREE,
                        repositories=(
                            ModelInstallRepositoryRow(
                                name="Example-Org/example-repo",
                                mirror_path=MIRROR,
                                remote_ref_prefix="origin/",
                                landing_branch="main",
                                review_branches=("integration",),
                            ),
                        ),
                        prepare_argv=(
                            "/usr/bin/env",
                            "example-refresh",
                            "{repository}",
                            "{ref}",
                        ),
                        verifies=(
                            ModelInstallVerifyRow(
                                name="example-verify",
                                argv=(
                                    "/usr/bin/env",
                                    "example-verify",
                                    "--unit",
                                    "example-unit",
                                ),
                                expected_line="example-unit ok",
                            ),
                        ),
                    ),
                ),
            ),
        ),
    )


class _ScriptedRunner:
    """Answers by the first key whose every word appears in the argv's words.

    A git read, the prepare and the verify can share one command, so the key is
    the words that tell them apart (``("merge-base", "origin/main")``). A call
    no key covers answers 1 with the argv in its stderr, so an unscripted call
    fails the assertions that read it.
    """

    _ANSWERS: dict[tuple[str, ...], tuple[int, str, str]] = {
        ("example-refresh",): (0, "refreshed\n", ""),
        ("rev-parse", REF): (0, f"{NEW_COMMIT}\n", ""),
        ("merge-base", "origin/main"): (0, "", ""),
        ("example-verify",): (0, "unit starting\nexample-unit ok\nunit done\n", ""),
    }

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def __call__(
        self, argv: Sequence[str], timeout: int
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(tuple(argv))
        spoken = " ".join(argv)
        code, out, err = next(
            (
                answer
                for key, answer in self._ANSWERS.items()
                if all(word in spoken for word in key)
            ),
            (1, "", f"unscripted: {' '.join(argv)}"),
        )
        return subprocess.CompletedProcess(
            args=list(argv), returncode=code, stdout=out, stderr=err
        )


class _FakeInstaller:
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
            commit=NEW_COMMIT,
        )


class _RecordingHandler:
    """The handler the host serves, keeping every received request it was handed."""

    def __init__(self, inner: HandlerInstallRequest) -> None:
        self.inner = inner
        self.received: list[ModelInstallRequestReceived] = []

    def handle(
        self, received: ModelInstallRequestReceived
    ) -> ModelInstallRequestHandled:
        self.received.append(received)
        return self.inner.handle(received)


def _draft() -> ModelInstallRequestDraft:
    return ModelInstallRequestDraft(
        lane="example-lane",
        host="example-host",
        kind=EnumInstallTarget.UNIT_TREE,
        repository="Example-Org/example-repo",
        ref=REF,
        unit="example-unit",
        verify="example-verify",
    )


def _handler(
    overlay: ModelInstallRequestEffectOverlay,
    journal: InMemoryResultJournal,
    installer: _FakeInstaller | None = None,
) -> HandlerInstallRequest:
    return HandlerInstallRequest(
        overlay=overlay,
        executor_host="example-host",
        unit_tree_installer=installer if installer is not None else _FakeInstaller(),
        runner=_ScriptedRunner(),
        journal=journal,
        now=lambda: NOW,
    )


def _request(expires_in: timedelta) -> ModelInstallRequest:
    """The example draft as a published request, expiring ``expires_in`` from now."""
    return ModelInstallRequest(
        **_draft().model_dump(),
        request_id=uuid.uuid4(),
        requested_at=NOW,
        expires_at=NOW + expires_in,
    )


def _verified_result(request: ModelInstallRequest) -> ModelInstallRequestResult:
    """One verified result for ``request``, as the serving host would publish it."""
    return ModelInstallRequestResult(
        request_id=request.request_id,
        host=request.host,
        unit=request.unit,
        status=EnumInstallRequestStatus.VERIFIED,
        at=NOW + timedelta(minutes=5),
        revision=NEW_COMMIT,
        verify_line="example-unit ok",
        reason="ok",
    )


async def _wait_for(messages: list[object]) -> None:
    """Give the in-memory bus's own delivery the short while it needs."""
    for _ in range(200):
        if messages:
            return
        await asyncio.sleep(0.05)


def test_the_declared_topics_are_the_contract_topics() -> None:
    topics = load_install_request_topics()
    assert topics.command == "onex.cmd.omnimarket.install-requested.v1"
    assert topics.success == "onex.evt.omnimarket.install-request-completed.v1"
    assert topics.failure == "onex.evt.omnimarket.install-request-failed.v1"
    assert (
        topics.writer_command_topic("example-lane-bot")
        == "onex.cmd.omnimarket.install-requested-from-example-lane-bot.v1"
    )


def test_a_login_outside_the_principal_pattern_has_no_writer_topic() -> None:
    topics = load_install_request_topics()
    for login in ("Upper-Case", "has space", "", "-leading-dash", "a/b"):
        with pytest.raises(ValueError, match="invalid writer principal"):
            topics.writer_command_topic(login)


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
        request = await publish_request(
            bus, _draft(), overlay, now=lambda: NOW, publisher=ALLOWED_PUBLISHER
        )
        await host.drain()

        assert len(results) == 1
        envelope = results[0]
        assert envelope["correlation_id"] == str(request.request_id)
        result = ModelInstallRequestResult.model_validate(envelope["payload"])
        assert result.request_id == request.request_id
        assert result.status is EnumInstallRequestStatus.VERIFIED
        assert result.verify_line == "example-unit ok"
        assert result.revision == NEW_COMMIT
        assert journal.get(request.request_id) == result
        await host.stop()


async def test_a_request_on_the_topic_of_a_login_the_host_does_not_allow_is_never_seen(
    tmp_path: Path,
) -> None:
    """The publisher is the topic's login, which the broker attests: a login outside the host
    row's allowed publishers writes a topic the host never reads, so nothing is answered, nothing
    is journalled and the installer is never called -- and a forged ``producer`` cell naming an
    allowed login, written to that topic, changes nothing."""
    async with open_install_request_bus(
        bus="inmemory", lane=None, kafka_bootstrap=None
    ) as bus:
        overlay = _overlay(blocker=str(tmp_path / "no-such.lock"))
        journal = InMemoryResultJournal()
        installer = _FakeInstaller()
        host = InstallRequestHost(
            bus,
            _handler(overlay, journal, installer),
            overlay,
            "example-host",
            now=lambda: NOW,
        )
        topics = load_install_request_topics()
        answers: list[dict[str, object]] = []

        async def capture(message: Any) -> None:
            answers.append(json.loads(message.value))

        await bus.subscribe(topics.success, on_message=capture, group_id="capture")
        await bus.subscribe(topics.failure, on_message=capture, group_id="capture")
        await host.start()
        request = await publish_request(
            bus, _draft(), overlay, now=lambda: NOW, publisher="somebody-else"
        )
        forged = _request(expires_in=timedelta(minutes=120))
        envelope = json.loads(
            envelope_bytes_for(
                topics.command, forged.model_dump(mode="json"), forged.request_id
            )
        )
        envelope["producer"] = ALLOWED_PUBLISHER
        await bus.publish(
            topics.writer_command_topic("somebody-else"),
            None,
            json.dumps(envelope).encode("utf-8"),
        )
        await host.drain()

        assert answers == []
        assert journal.get(request.request_id) is None
        assert journal.get(forged.request_id) is None
        assert installer.requests == []
        assert host.processed == 0
        await host.stop()


async def test_the_publisher_is_the_topic_login_and_never_the_envelope_producer(
    tmp_path: Path,
) -> None:
    """An allowed login's topic carries a request whose envelope producer names someone else: the
    request is served as the topic's login (the producer cell is never read), and the handler is
    handed that login."""
    async with open_install_request_bus(
        bus="inmemory", lane=None, kafka_bootstrap=None
    ) as bus:
        overlay = _overlay(blocker=str(tmp_path / "no-such.lock"))
        recording = _RecordingHandler(_handler(overlay, InMemoryResultJournal()))
        host = InstallRequestHost(
            bus, recording, overlay, "example-host", now=lambda: NOW
        )
        topics = load_install_request_topics()
        await host.start()
        request = _request(expires_in=timedelta(minutes=120))
        envelope = json.loads(
            envelope_bytes_for(
                topics.command, request.model_dump(mode="json"), request.request_id
            )
        )
        envelope["producer"] = "somebody-else"
        await bus.publish(
            topics.writer_command_topic(ALLOWED_PUBLISHER),
            None,
            json.dumps(envelope).encode("utf-8"),
        )
        await host.drain()

        assert [r.publisher for r in recording.received] == [ALLOWED_PUBLISHER]
        await host.stop()


async def test_a_host_with_no_servable_writer_topic_refuses_to_start(
    tmp_path: Path,
) -> None:
    """A host that could subscribe to none of its logins' topics serves nothing: it says so at start."""
    async with open_install_request_bus(
        bus="inmemory", lane=None, kafka_bootstrap=None
    ) as bus:
        overlay = _overlay(blocker=str(tmp_path / "no-such.lock"))

        async def refuse(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("topic not found")

        bus.subscribe = refuse  # type: ignore[method-assign]
        host = InstallRequestHost(
            bus, _handler(overlay, InMemoryResultJournal()), overlay, "example-host"
        )
        with pytest.raises(ValueError, match="serves no writer topic"):
            await host.start()
        await host.stop()


async def test_a_bare_request_with_no_envelope_is_answered_on_the_failure_terminal(
    tmp_path: Path,
) -> None:
    """A bare request object is never validated as a request: it is answered on the
    failure terminal and the handler is never handed it."""
    async with open_install_request_bus(
        bus="inmemory", lane=None, kafka_bootstrap=None
    ) as bus:
        overlay = _overlay(blocker=str(tmp_path / "no-such.lock"))
        recording = _RecordingHandler(_handler(overlay, InMemoryResultJournal()))
        host = InstallRequestHost(
            bus, recording, overlay, "example-host", now=lambda: NOW
        )
        topics = load_install_request_topics()
        failures: list[dict[str, object]] = []

        async def capture(message: Any) -> None:
            failures.append(json.loads(message.value))

        await bus.subscribe(topics.failure, on_message=capture, group_id="capture")
        await host.start()
        request = _request(expires_in=timedelta(minutes=120))
        await bus.publish(
            topics.writer_command_topic(ALLOWED_PUBLISHER),
            str(request.request_id).encode("utf-8"),
            json.dumps(request.model_dump(mode="json")).encode("utf-8"),
        )
        await host.drain()

        assert len(failures) == 1
        payload = failures[0]["payload"]
        assert isinstance(payload, dict)
        assert payload["status"] == "failed"
        assert payload["request_id"] == str(request.request_id)
        assert "no envelope" in str(payload["error_message"])
        assert recording.received == []
        assert host.processed == 0
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
            topics.writer_command_topic(ALLOWED_PUBLISHER),
            None,
            envelope_bytes_for(topics.command, {"host": "example-host"}, uuid.uuid4()),
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
        request = await publish_request(
            bus, _draft(), overlay, now=lambda: NOW, publisher=ALLOWED_PUBLISHER
        )
        await host.drain()
        assert results == []

        blocker.unlink()
        await _wait_for(results)

        assert len(results) == 1
        result = ModelInstallRequestResult.model_validate(results[0]["payload"])
        assert result.request_id == request.request_id
        assert result.status is EnumInstallRequestStatus.VERIFIED
        await host.stop()


async def test_publish_request_writes_the_publishers_topic_and_the_orchestrators_input() -> (
    None
):
    """The request goes to the writer topic of the publishing login (the host's identity fact) and
    once to the shared command topic (the orchestrator's typed input); the envelope names no sender."""
    async with open_install_request_bus(
        bus="inmemory", lane=None, kafka_bootstrap=None
    ) as bus:
        topics = load_install_request_topics()
        writer: list[dict[str, object]] = []
        shared: list[dict[str, object]] = []

        async def capture_writer(message: Any) -> None:
            writer.append(json.loads(message.value))

        async def capture_shared(message: Any) -> None:
            shared.append(json.loads(message.value))

        await bus.subscribe(
            topics.writer_command_topic(ALLOWED_PUBLISHER),
            on_message=capture_writer,
            group_id="capture-writer",
        )
        await bus.subscribe(
            topics.command, on_message=capture_shared, group_id="capture-shared"
        )
        request = await publish_request(
            bus, _draft(), _overlay(), now=lambda: NOW, publisher=ALLOWED_PUBLISHER
        )

        assert len(writer) == 1
        assert len(shared) == 1
        for envelope in (writer[0], shared[0]):
            assert not envelope.get("producer")
            assert envelope["correlation_id"] == str(request.request_id)
            assert ModelInstallRequest.model_validate(envelope["payload"]) == request


async def _read_owed(
    bus: Any,
    overlay: ModelInstallRequestEffectOverlay,
    now: Callable[[], datetime],
    group_id: str,
    publish: Callable[[], Awaitable[None]],
) -> ModelInstallOwedReading:
    """Start the reading, let it subscribe, publish while it listens, return it."""
    task = asyncio.create_task(read_owed(bus, overlay, now, group_id))
    # The in-memory bus hands a record only to a subscriber already registered,
    # so nothing publishes before the reading's two subscriptions stand.
    for _ in range(200):
        if len(bus.get_consumer_groups()) >= 2:
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("the owed reading never subscribed to the bus")
    await publish()
    return await task


async def _publish_result(bus: Any, result: ModelInstallRequestResult) -> None:
    """Publish one result on the success terminal the way the serving host does."""
    topics = load_install_request_topics()
    await bus.publish(
        topics.success,
        str(result.request_id).encode("utf-8"),
        envelope_bytes_for(
            topics.success, result.model_dump(mode="json"), result.request_id
        ),
    )


async def test_read_owed_lists_a_request_with_no_result() -> None:
    async with open_install_request_bus(
        bus="inmemory", lane=None, kafka_bootstrap=None
    ) as bus:
        overlay = _overlay()
        published: list[ModelInstallRequest] = []

        async def publish() -> None:
            published.append(
                await publish_request(
                    bus, _draft(), overlay, now=lambda: NOW, publisher=ALLOWED_PUBLISHER
                )
            )

        reading = await _read_owed(bus, overlay, lambda: NOW, "owed-lists", publish)

        assert reading.owed == 1
        assert reading.expired == 0
        assert len(reading.lines) == 1
        line = reading.lines[0]
        assert line.kind == "not_running"
        assert str(published[0].request_id) in line.line
        assert NOT_RUNNING_MARK in line.line


async def test_read_owed_clears_a_request_with_a_verified_result() -> None:
    async with open_install_request_bus(
        bus="inmemory", lane=None, kafka_bootstrap=None
    ) as bus:
        overlay = _overlay()

        async def publish() -> None:
            request = await publish_request(
                bus, _draft(), overlay, now=lambda: NOW, publisher=ALLOWED_PUBLISHER
            )
            await _publish_result(bus, _verified_result(request))

        reading = await _read_owed(bus, overlay, lambda: NOW, "owed-clears", publish)

        assert reading.owed == 0
        assert reading.expired == 0
        assert reading.lines == ()


async def test_read_owed_marks_a_request_past_its_expiry() -> None:
    async with open_install_request_bus(
        bus="inmemory", lane=None, kafka_bootstrap=None
    ) as bus:
        overlay = _overlay()

        async def publish() -> None:
            await publish_request(
                bus, _draft(), overlay, now=lambda: NOW, publisher=ALLOWED_PUBLISHER
            )

        reading = await _read_owed(
            bus,
            overlay,
            lambda: NOW + timedelta(hours=3),
            "owed-expired",
            publish,
        )

        assert reading.owed == 1
        assert reading.expired == 1
        assert len(reading.lines) == 1
        assert reading.lines[0].kind == "expired"
        assert reading.lines[0].line.startswith(EXPIRED_MARK)


async def test_read_owed_ignores_a_login_no_host_names_and_a_bare_request() -> None:
    """A login no host row names has no topic the reading subscribes to, and a bare request
    object -- no envelope -- is never validated as one: neither is counted."""
    async with open_install_request_bus(
        bus="inmemory", lane=None, kafka_bootstrap=None
    ) as bus:
        overlay = _overlay()

        async def publish() -> None:
            await publish_request(
                bus, _draft(), overlay, now=lambda: NOW, publisher="forged-lane-bot"
            )
            bare = _request(expires_in=timedelta(minutes=120))
            topics = load_install_request_topics()
            await bus.publish(
                topics.writer_command_topic(ALLOWED_PUBLISHER),
                None,
                json.dumps(bare.model_dump(mode="json")).encode("utf-8"),
            )

        reading = await _read_owed(bus, overlay, lambda: NOW, "owed-forged", publish)

        assert reading.owed == 0
        assert reading.unread == 0
        assert reading.lines == ()
        assert reading.readable is True


async def test_read_owed_lists_a_request_for_a_host_the_reader_does_not_hold_as_unread() -> (
    None
):
    """The reader's overlay holds no ``other-host``: the request cannot be checked, so it is
    listed unread -- never dropped, never an owed zero."""
    async with open_install_request_bus(
        bus="inmemory", lane=None, kafka_bootstrap=None
    ) as bus:
        overlay = _overlay()
        published: list[ModelInstallRequest] = []

        async def publish() -> None:
            published.append(
                await publish_request(
                    bus,
                    _draft().model_copy(update={"host": "other-host"}),
                    overlay,
                    now=lambda: NOW,
                    publisher=ALLOWED_PUBLISHER,
                )
            )

        reading = await _read_owed(bus, overlay, lambda: NOW, "owed-unread", publish)

        assert reading.owed == 0
        assert reading.unread == 1
        assert [line.kind for line in reading.lines] == ["unread"]
        assert str(published[0].request_id) in reading.lines[0].line
        assert "other-host" in reading.lines[0].line


async def test_read_owed_lists_a_request_from_a_login_its_host_does_not_allow_as_unread() -> (
    None
):
    """A login another host row allows writes a request naming this host: listed unread."""
    async with open_install_request_bus(
        bus="inmemory", lane=None, kafka_bootstrap=None
    ) as bus:
        base = _overlay()
        second = base.hosts[0].model_copy(
            update={"name": "second-host", "allowed_publishers": ("second-lane-bot",)}
        )
        overlay = base.model_copy(update={"hosts": (*base.hosts, second)})

        async def publish() -> None:
            await publish_request(
                bus, _draft(), overlay, now=lambda: NOW, publisher="second-lane-bot"
            )

        reading = await _read_owed(bus, overlay, lambda: NOW, "owed-cross", publish)

        assert reading.owed == 0
        assert reading.unread == 1
        assert (
            "second-lane-bot is not allowed for example-host" in reading.lines[0].line
        )


async def test_a_quiet_bus_reads_unread_and_not_zero_owed() -> None:
    """No envelope at all inside the quiet window: ``readable`` is False, never ``owed=0``."""
    async with open_install_request_bus(
        bus="inmemory", lane=None, kafka_bootstrap=None
    ) as bus:
        reading = await read_owed(bus, _overlay(), lambda: NOW, "owed-quiet")

        assert reading.readable is False
        assert reading.owed == 0
        assert reading.lines == ()


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


# --------------------------------------------------------------------- the CLI


def _overlay_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An overlay root that holds this node's overlay, with the test's quiet window."""
    root = tmp_path / "roots"
    folder = root / NODE_NAME
    folder.mkdir(parents=True)
    folder.joinpath("overlay.yaml").write_text(
        yaml.safe_dump(_overlay().model_dump(mode="json")), encoding="utf-8"
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root))


def test_the_owed_command_reads_a_quiet_bus_as_unread_with_exit_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _overlay_root(tmp_path, monkeypatch)

    code = main(["owed", "--bus", "inmemory"])

    # A bus that holds no record at all within the quiet window is unread, never a clean zero.
    assert code == 3
    assert capsys.readouterr().out.startswith("owed=unread")


def test_the_owed_command_takes_no_requests_file_argument(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _overlay_root(tmp_path, monkeypatch)

    with pytest.raises(SystemExit):
        main(["owed", "--bus", "inmemory", "--requests", "requests.jsonl"])


def test_the_owed_command_prints_one_line_per_owed_request_and_exits_1_when_one_expired(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _overlay_root(tmp_path, monkeypatch)
    line = ModelInstallOwedLine(
        request_id=uuid.uuid4(),
        kind="expired",
        line=f"{EXPIRED_MARK} one install stopped being worth running",
    )

    async def fake_read_owed(
        bus: object,
        overlay: ModelInstallRequestEffectOverlay,
        now: Callable[[], datetime],
        group_id: str | None = None,
    ) -> ModelInstallOwedReading:
        return ModelInstallOwedReading(owed=1, expired=1, lines=(line,))

    @contextlib.asynccontextmanager
    async def fake_open_bus(args: object) -> Any:
        yield object()

    monkeypatch.setattr(runtime_install_request, "read_owed", fake_read_owed)
    monkeypatch.setattr(runtime_install_request, "_open_bus", fake_open_bus)

    code = main(["owed", "--bus", "inmemory"])

    assert code == 1
    assert capsys.readouterr().out == f"{line.line}\nowed=1 expired=1 unread=0\n"


def test_the_publisher_is_the_lane_principal_on_a_declared_lane_and_the_overlay_login_otherwise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A declared lane bus publishes under the broker principal it authenticated as;
    the in-memory bus and an explicit broker address hold no declared login, so the
    overlay's login variable names it; a missing variable is refused, never guessed."""
    import argparse

    overlay = _overlay()
    monkeypatch.setattr(
        runtime_install_request,
        "bound_lane_principal",
        lambda: "example-lane-principal",
    )
    monkeypatch.setenv("TEST_INSTALL_PUBLISHER_LOGIN", ALLOWED_PUBLISHER)
    lane = argparse.Namespace(bus="kafka", bus_lane="dev", kafka_bootstrap=None)
    explicit = argparse.Namespace(bus="kafka", bus_lane=None, kafka_bootstrap="h:1")
    memory = argparse.Namespace(bus="inmemory", bus_lane=None, kafka_bootstrap=None)

    assert (
        runtime_install_request._publisher_for(lane, overlay)
        == "example-lane-principal"
    )
    assert (
        runtime_install_request._publisher_for(explicit, overlay) == ALLOWED_PUBLISHER
    )
    assert runtime_install_request._publisher_for(memory, overlay) == ALLOWED_PUBLISHER
    monkeypatch.delenv("TEST_INSTALL_PUBLISHER_LOGIN")
    with pytest.raises(ValueError, match="TEST_INSTALL_PUBLISHER_LOGIN"):
        runtime_install_request._publisher_for(explicit, overlay)
