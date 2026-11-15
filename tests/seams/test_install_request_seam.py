# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam: lane end -> install request -> installer result -> integration line (OMN-24546, root 5).

One request crosses four seams, and each is read here against the other side's own code, never a copy of it:

* the request and the result cross the bus as JSON and are decoded by the shared models, so a cell renamed on either side fails;
* the effect hands the unit tree installer (``node_unit_tree_install_effect``) a request that installer accepts and reads its
  real verdict and commit: the install below runs the real installer over a throwaway git repository;
* the result's ``installed=<host>:<revision> observed=<line>`` words are read by the handoff proof compute's own patterns;
* the owed reading's lines carry the two marks the throughput tick matches on, and a verified result clears the request.

The chain runs on the in-memory bus always, and on a real broker where ``INSTALL_REQUEST_PARITY_BOOTSTRAP`` names a plaintext
lab broker (never the dev lane's):

    INSTALL_REQUEST_PARITY_BOOTSTRAP=<host:port> uv run pytest tests/seams/test_install_request_seam.py -m kafka -q
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import os
import re
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.delegated_test_loop.lab_run_bus import ProtocolLabRunBus
from omnimarket.models.install_request import (
    EXPIRED_MARK,
    NOT_RUNNING_MARK,
    EnumInstallRequestStatus,
    EnumInstallTarget,
    ModelInstallOwedInput,
    ModelInstallRequest,
    ModelInstallRequestDraft,
    ModelInstallRequestResult,
)
from omnimarket.nodes.node_install_request_decision_compute.handlers.handler_install_request_decision import (
    HandlerInstallRequestDecision,
)
from omnimarket.nodes.node_install_request_effect.handlers.handler_install_request import (
    HandlerInstallRequest,
    InMemoryResultJournal,
    default_runner,
)
from omnimarket.nodes.node_install_request_effect.overlay import (
    ModelInstallHostRow,
    ModelInstallRequestEffectOverlay,
    ModelInstallUnitRow,
    load_overlay,
)
from omnimarket.nodes.node_install_request_effect.runtime_install_request import (
    InstallRequestHost,
    load_install_request_topics,
    open_install_request_bus,
    publish_request,
)
from omnimarket.nodes.node_install_request_owed_compute.handlers.handler_install_request_owed import (
    HandlerInstallRequestOwed,
)
from omnimarket.nodes.node_pr_handoff_install_proof_compute.handlers.handler_install_proof import (
    INSTALLED_RE,
    OBSERVED_RE,
)
from omnimarket.nodes.node_unit_tree_install_effect.node import (
    NodeUnitTreeInstallEffect,
)
from tests.nodes.node_unit_tree_install_effect.unit_tree_support import (
    environ,
    git,
    make_overlay,
    make_root,
)

pytestmark = pytest.mark.unit

BOOTSTRAP_ENV = "INSTALL_REQUEST_PARITY_BOOTSTRAP"
TIMEOUT_S = 30.0
HOST = "h1"
NODES = "omnimarket.nodes"
CAT = "/usr/bin/env"


def _overlay() -> ModelInstallRequestEffectOverlay:
    return ModelInstallRequestEffectOverlay(
        host_name_env="TEST_INSTALL_HOST_NAME",
        default_expiry_minutes=120,
        command_timeout_seconds=60,
        hold_retry_seconds=1,
        max_command_age_seconds=21600,
        output_max_chars=400,
        journal_dir_env="TEST_INSTALL_JOURNAL_DIR",
        journal_file="results.jsonl",
        hosts=(
            ModelInstallHostRow(
                name=HOST,
                units=(
                    ModelInstallUnitRow(
                        name="u1",
                        kind=EnumInstallTarget.UNIT_TREE,
                        repositories=("Example-Org/example-repo",),
                        verify_prefixes=((CAT, "cat"),),
                    ),
                ),
            ),
        ),
    )


def _draft(
    root: Path, commit: str, *, verify: tuple[str, ...] | None = None
) -> ModelInstallRequestDraft:
    return ModelInstallRequestDraft(
        lane="example-lane",
        host=HOST,
        kind=EnumInstallTarget.UNIT_TREE,
        repository="Example-Org/example-repo",
        ref=commit,
        unit="u1",
        verify_command=verify
        or (CAT, "cat", str(root / "units" / "u1" / "tree" / "a.txt")),
        expected_line="a1",
    )


def _handler(root: Path, journal: InMemoryResultJournal) -> HandlerInstallRequest:
    return HandlerInstallRequest(
        overlay=_overlay(),
        executor_host=HOST,
        unit_tree_installer=NodeUnitTreeInstallEffect(make_overlay(), environ(root)),
        runner=default_runner,
        journal=journal,
        now=lambda: datetime.now(UTC),
    )


@asynccontextmanager
async def _in_memory() -> AsyncIterator[ProtocolLabRunBus]:
    async with open_install_request_bus(
        bus="inmemory", lane=None, kafka_bootstrap=None
    ) as bus:
        yield bus


@asynccontextmanager
async def _broker() -> AsyncIterator[ProtocolLabRunBus]:
    async with open_install_request_bus(
        bus="kafka", lane=None, kafka_bootstrap=os.environ[BOOTSTRAP_ENV]
    ) as bus:
        yield bus


async def _capture(bus: ProtocolLabRunBus, topic: str) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    async def on_message(message: Any) -> None:
        seen.append(json.loads(message.value))

    extra = (
        {"auto_offset_reset": "earliest"}
        if "auto_offset_reset" in inspect.signature(bus.subscribe).parameters
        else {}
    )
    await bus.subscribe(
        topic, on_message=on_message, group_id=f"seam-{uuid.uuid4().hex}", **extra
    )
    return seen


async def _until(done: Callable[[], bool]) -> None:
    async def poll() -> None:
        while not done():
            await asyncio.sleep(0.1)

    await asyncio.wait_for(poll(), TIMEOUT_S)


async def _chain(
    open_bus: Callable[[], Any], root: Path, commit: str, verify: tuple[str, ...] | None
) -> tuple[ModelInstallRequest, ModelInstallRequestResult]:
    """Publish one request, serve it on the host, read its result off the success topic."""
    topics = load_install_request_topics()
    journal = InMemoryResultJournal()
    async with open_bus() as bus:
        host = InstallRequestHost(
            bus,
            _handler(root, journal),
            _overlay(),
            HOST,
            topics=topics,
            group_id=f"seam-host-{uuid.uuid4().hex}",
        )
        results = await _capture(bus, topics.success)
        await host.start()
        try:
            await asyncio.sleep(1.0 if "kafka" in repr(type(bus)).lower() else 0.0)
            request = await publish_request(
                bus,
                _draft(root, commit, verify=verify),
                _overlay(),
                lambda: datetime.now(UTC),
            )
            await _until(
                lambda: any(
                    r.get("correlation_id") == str(request.request_id) for r in results
                )
            )
        finally:
            await host.stop()
    envelope = next(
        r for r in results if r["correlation_id"] == str(request.request_id)
    )
    return request, ModelInstallRequestResult.model_validate(envelope["payload"])


def _fresh_root(tmp_path: Path) -> tuple[Path, str]:
    root = make_root(tmp_path)
    return root, git(root / "clone", "rev-parse", "HEAD")


async def _golden(open_bus: Callable[[], Any], tmp_path: Path) -> None:
    root, commit = _fresh_root(tmp_path)
    request, result = await _chain(open_bus, root, commit, None)
    # The real installer ran: its commit is the revision, its tree holds the file the verify command read.
    assert result.status is EnumInstallRequestStatus.VERIFIED
    assert result.revision == commit
    assert result.verify_line == "a1"
    assert (root / "units" / "u1" / "tree" / "a.txt").read_text() == "a1\n"
    # The words a handoff reads match the handoff proof compute's own patterns.
    line = result.integration_line()
    installed = INSTALLED_RE.search(line)
    assert installed is not None
    assert installed.groups() == (HOST, commit)
    observed = OBSERVED_RE.search(line)
    assert observed is not None
    assert observed.group(1) == "a1"
    # The owed reading clears the request once the verified result exists, and owes it before.
    reading = HandlerInstallRequestOwed().handle(
        ModelInstallOwedInput(
            requests=(request,), results=(result,), now=datetime.now(UTC)
        )
    )
    assert reading.owed == 0
    owed = HandlerInstallRequestOwed().handle(
        ModelInstallOwedInput(requests=(request,), results=(), now=datetime.now(UTC))
    )
    assert owed.owed == 1
    assert re.match(rf"^\S+ {NOT_RUNNING_MARK}: ", owed.lines[0].line)
    assert "\n" not in owed.lines[0].line


async def _refused(open_bus: Callable[[], Any], tmp_path: Path) -> None:
    root, commit = _fresh_root(tmp_path)
    request, result = await _chain(
        open_bus, root, commit, ("/bin/rm", "-rf", "/nonexistent")
    )
    # A verify command the host row does not declare is refused before anything is written.
    assert result.status is EnumInstallRequestStatus.REFUSED
    assert result.reason == "verify_command_not_declared"
    assert not (root / "units" / "u1" / "tree").exists()
    assert result.integration_line() == ""
    owed = HandlerInstallRequestOwed().handle(
        ModelInstallOwedInput(
            requests=(request,), results=(result,), now=datetime.now(UTC)
        )
    )
    assert owed.owed == 1
    assert "last result refused: verify_command_not_declared" in owed.lines[0].line


def test_the_golden_chain_on_the_in_memory_bus(tmp_path: Path) -> None:
    asyncio.run(_golden(_in_memory, tmp_path))


def test_the_error_chain_on_the_in_memory_bus(tmp_path: Path) -> None:
    asyncio.run(_refused(_in_memory, tmp_path))


@pytest.mark.kafka
@pytest.mark.integration
def test_the_golden_chain_on_the_real_broker_ends_as_the_in_memory_bus_does(
    tmp_path: Path,
) -> None:
    if not os.environ.get(BOOTSTRAP_ENV):
        pytest.skip(f"Kafka not reachable: {BOOTSTRAP_ENV} names no lab broker")
    asyncio.run(_golden(_broker, tmp_path))


@pytest.mark.kafka
@pytest.mark.integration
def test_the_error_chain_on_the_real_broker_ends_as_the_in_memory_bus_does(
    tmp_path: Path,
) -> None:
    if not os.environ.get(BOOTSTRAP_ENV):
        pytest.skip(f"Kafka not reachable: {BOOTSTRAP_ENV} names no lab broker")
    asyncio.run(_refused(_broker, tmp_path))


# ------------------------------------------------------------------ the shape seams


def test_a_cell_renamed_on_the_result_is_refused_by_the_model() -> None:
    """Failing control: the bus carries JSON, so a renamed cell must fail the decode rather than be dropped."""
    good = ModelInstallRequestResult(
        request_id=uuid.uuid4(),
        host=HOST,
        unit="u1",
        status=EnumInstallRequestStatus.VERIFIED,
        at=datetime.now(UTC),
        revision="a" * 40,
        verify_line="a1",
        reason="ok",
    )
    renamed = good.model_dump_json().replace('"revision"', '"commit"')
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ModelInstallRequestResult.model_validate_json(renamed)
    assert ModelInstallRequestResult.model_validate_json(good.model_dump_json()) == good


def test_a_result_without_a_revision_gives_no_integration_line() -> None:
    """The handoff reads ``installed=<host>:<sha>``; a result that names none must not print the words at all."""
    result = ModelInstallRequestResult(
        request_id=uuid.uuid4(),
        host=HOST,
        unit="u1",
        status=EnumInstallRequestStatus.VERIFIED,
        at=datetime.now(UTC),
        verify_line="a1",
        reason="ok",
    )
    assert result.integration_line() == ""


def test_the_expired_line_carries_the_mark_the_tick_matches_and_a_not_running_one_does_not() -> (
    None
):
    request = ModelInstallRequest(
        **_draft(Path("/unused"), "a" * 40).model_dump(),
        request_id=uuid.uuid4(),
        requested_at=datetime(2026, 11, 15, 1, 0, tzinfo=UTC),
        expires_at=datetime(2026, 11, 15, 3, 0, tzinfo=UTC),
    )
    before = HandlerInstallRequestOwed().handle(
        ModelInstallOwedInput(
            requests=(request,), now=datetime(2026, 11, 15, 2, 0, tzinfo=UTC)
        )
    )
    after = HandlerInstallRequestOwed().handle(
        ModelInstallOwedInput(
            requests=(request,), now=datetime(2026, 11, 15, 3, 0, tzinfo=UTC)
        )
    )
    assert (before.owed, before.expired) == (1, 0)
    assert (after.owed, after.expired) == (1, 1)
    assert NOT_RUNNING_MARK in before.lines[0].line
    assert EXPIRED_MARK not in before.lines[0].line
    assert after.lines[0].line.startswith(EXPIRED_MARK)


@pytest.mark.parametrize(
    "node",
    [
        "node_install_request_decision_compute",
        "node_install_request_owed_compute",
        "node_install_request_effect",
    ],
)
def test_each_contract_names_models_that_import_and_a_handler_that_takes_them(
    node: str,
) -> None:
    contract = yaml.safe_load(
        resources.files(f"{NODES}.{node}").joinpath("contract.yaml").read_text()
    )
    for side in ("input_model", "output_model"):
        declared = contract[side]
        cls = getattr(importlib.import_module(declared["module"]), declared["name"])
        assert cls.__name__ == declared["name"]
    handler = contract["handler"]
    module = importlib.import_module(handler["module"])
    assert hasattr(getattr(module, handler["class"]), "handle")
    assert contract["handler"]["input_model"].endswith(contract["input_model"]["name"])


def test_the_effect_topics_are_the_contract_topics_and_the_host_reads_them_from_it() -> (
    None
):
    contract = yaml.safe_load(
        resources.files(f"{NODES}.node_install_request_effect")
        .joinpath("contract.yaml")
        .read_text()
    )
    topics = load_install_request_topics()
    assert topics.command == contract["topics"]["install_requested"]
    assert topics.success == contract["topics"]["install_request_completed"]
    assert topics.failure == contract["topics"]["install_request_failed"]
    assert contract["terminal_event"] == topics.success
    granted = {g["name"] for g in contract["broker_acl_grants"][0]["grants"]}
    assert {topics.command, topics.success, topics.failure} <= granted


def test_the_example_overlay_loads_through_the_overlay_model(tmp_path: Path) -> None:
    text = (
        resources.files(f"{NODES}.node_install_request_effect")
        .joinpath("overlay.example.yaml")
        .read_text()
    )
    node_dir = tmp_path / "node_install_request_effect"
    node_dir.mkdir()
    (node_dir / "overlay.yaml").write_text(text)
    overlay = load_overlay({"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)})
    assert {unit.kind for unit in overlay.hosts[0].units} == set(EnumInstallTarget)


def test_without_the_decision_expiry_rule_an_expired_request_would_have_been_installed() -> (
    None
):
    """Failing control of the whole chain: the effect installs nothing for an expired request, and does when the rule is removed."""

    class NoExpiry(HandlerInstallRequestDecision):
        def _expired(self, request: Any) -> bool:
            return False

    root_calls: list[str] = []

    class Installer:
        def handle(self, request: Any) -> Any:
            root_calls.append(request.ref)
            raise RuntimeError("installer reached")

    expired = ModelInstallRequest(
        **_draft(Path("/unused"), "a" * 40).model_dump(),
        request_id=uuid.uuid4(),
        requested_at=datetime(2026, 11, 15, 1, 0, tzinfo=UTC),
        expires_at=datetime(2026, 11, 15, 2, 0, tzinfo=UTC),
    )

    def build(decision: HandlerInstallRequestDecision) -> HandlerInstallRequest:
        return HandlerInstallRequest(
            overlay=_overlay(),
            executor_host=HOST,
            unit_tree_installer=Installer(),
            runner=default_runner,
            journal=InMemoryResultJournal(),
            now=lambda: datetime(2026, 11, 15, 3, 0, tzinfo=UTC),
            decision=decision,
        )

    handled = build(HandlerInstallRequestDecision()).handle(expired)
    assert handled.result is not None
    assert handled.result.status is EnumInstallRequestStatus.EXPIRED
    assert root_calls == []
    with pytest.raises(RuntimeError, match="installer reached"):
        build(NoExpiry()).handle(expired)
    assert root_calls == ["a" * 40]
