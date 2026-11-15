# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam: lane end -> install request -> installer result -> integration line (OMN-24546, root 5).

One request crosses these seams, and each is read here against the other side's own code, never a copy of it:

* the request and the result cross the bus as JSON inside a command envelope whose producer is the publishing login, and
  are decoded by the shared models, so a cell renamed on either side fails and a message can carry no command at all;
* the effect hands the unit tree installer (``node_unit_tree_install_effect``) a request that installer accepts and reads
  its real verdict and commit: the installs below run the real installer over a throwaway git repository whose own
  mirror refs decide what is merged;
* the result's ``installed=<host>:<revision> observed=<line>`` words are read by the handoff proof compute's own patterns;
* the owed reading's lines carry the two marks the throughput tick matches on, and a verified result clears the request;
* the existing host install orchestrator routes the same request and result models on the same topics.

The golden and error chains run on the in-memory bus always, and on a real broker where
``INSTALL_REQUEST_PARITY_BOOTSTRAP`` names a plaintext lab broker (never the dev lane's):

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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
    ModelInstallRequestReceived,
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
    ModelInstallRepositoryRow,
    ModelInstallRequestEffectOverlay,
    ModelInstallUnitRow,
    ModelInstallVerifyRow,
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
from omnimarket.nodes.node_throughput_tick_decision_compute.models.model_throughput_tick_decision import (
    ModelThroughputTickRequest,
)
from omnimarket.nodes.node_unit_tree_install_effect.models.model_unit_tree_install import (
    ModelUnitTreeInstallRequest,
    ModelUnitTreeInstallResult,
)
from omnimarket.nodes.node_unit_tree_install_effect.node import (
    NodeUnitTreeInstallEffect,
)
from tests.nodes.node_unit_tree_install_effect.unit_tree_support import (
    commit_all,
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
REPOSITORY = "Example-Org/example-repo"
PUBLISHER = "example-lane-bot"
FORGER = "example-other-login"
VERIFY_A = "tree-holds-a"
VERIFY_NEVER = "tree-holds-nothing-true"


@dataclass(frozen=True)
class _Scene:
    """A throwaway installer root: ``first`` and ``second`` are merged into the mirror's landing branch, ``unmerged`` is not."""

    root: Path
    first: str
    second: str
    unmerged: str


def _scene(tmp_path: Path) -> _Scene:
    """Three real commits: main holds ``first`` then ``second`` (origin/main names both), ``side`` holds one more."""
    root = make_root(tmp_path)
    clone = root / "clone"
    first = git(clone, "rev-parse", "HEAD")
    (clone / "b.txt").write_text("b2\n", encoding="utf-8")
    second = commit_all(clone, "two")
    git(clone, "update-ref", "refs/remotes/origin/main", second)
    git(clone, "checkout", "-q", "-b", "side", first)
    (clone / "c.txt").write_text("c1\n", encoding="utf-8")
    unmerged = commit_all(clone, "side")
    git(clone, "checkout", "-q", "main")
    return _Scene(root=root, first=first, second=second, unmerged=unmerged)


def _overlay(root: Path) -> ModelInstallRequestEffectOverlay:
    tree = root / "units" / "u1" / "tree" / "a.txt"
    return ModelInstallRequestEffectOverlay(
        host_name_env="TEST_INSTALL_HOST_NAME",
        default_expiry_minutes=120,
        command_timeout_seconds=60,
        hold_retry_seconds=1,
        max_command_age_seconds=21600,
        output_max_chars=400,
        journal_dir_env="TEST_INSTALL_JOURNAL_DIR",
        journal_file="results.jsonl",
        publisher_login_env="TEST_INSTALL_PUBLISHER_LOGIN",
        owed_quiet_seconds=1,
        git_command=("/usr/bin/git",),
        hosts=(
            ModelInstallHostRow(
                name=HOST,
                allowed_publishers=(PUBLISHER,),
                units=(
                    ModelInstallUnitRow(
                        name="u1",
                        kind=EnumInstallTarget.UNIT_TREE,
                        repositories=(
                            ModelInstallRepositoryRow(
                                name=REPOSITORY,
                                mirror_path=str(root / "clone"),
                                remote_ref_prefix="origin/",
                                landing_branch="main",
                            ),
                        ),
                        verifies=(
                            ModelInstallVerifyRow(
                                name=VERIFY_A,
                                argv=(CAT, "cat", str(tree)),
                                expected_line="a1",
                            ),
                            ModelInstallVerifyRow(
                                name=VERIFY_NEVER,
                                argv=(CAT, "cat", str(tree)),
                                expected_line="a line the tree never holds",
                            ),
                        ),
                    ),
                ),
            ),
        ),
    )


def _draft(ref: str, verify: str = VERIFY_A) -> ModelInstallRequestDraft:
    return ModelInstallRequestDraft(
        lane="example-lane",
        host=HOST,
        kind=EnumInstallTarget.UNIT_TREE,
        repository=REPOSITORY,
        ref=ref,
        unit="u1",
        verify=verify,
    )


def _request(
    draft: ModelInstallRequestDraft, *, expires_in: timedelta
) -> ModelInstallRequest:
    stamp = datetime(2026, 11, 15, 1, 0, tzinfo=UTC)
    return ModelInstallRequest(
        **draft.model_dump(),
        request_id=uuid.uuid4(),
        requested_at=stamp,
        expires_at=stamp + expires_in,
    )


def _handler(root: Path, journal: InMemoryResultJournal) -> HandlerInstallRequest:
    return HandlerInstallRequest(
        overlay=_overlay(root),
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


class _Host:
    """One served host on one bus: ask it for installs and read what it answers on the two terminals."""

    def __init__(self, bus: ProtocolLabRunBus, scene: _Scene) -> None:
        self.bus = bus
        self.scene = scene
        self.topics = load_install_request_topics()
        self.journal = InMemoryResultJournal()
        self.host = InstallRequestHost(
            bus,
            _handler(scene.root, self.journal),
            _overlay(scene.root),
            HOST,
            topics=self.topics,
            group_id=f"seam-host-{uuid.uuid4().hex}",
        )
        self.results: list[dict[str, Any]] = []
        self.failures: list[dict[str, Any]] = []

    async def start(self) -> None:
        self.results = await _capture(self.bus, self.topics.success)
        self.failures = await _capture(self.bus, self.topics.failure)
        await self.host.start()
        await asyncio.sleep(1.0 if "kafka" in repr(type(self.bus)).lower() else 0.0)

    async def ask(
        self, draft: ModelInstallRequestDraft, publisher: str = PUBLISHER
    ) -> tuple[ModelInstallRequest, ModelInstallRequestResult]:
        """Publish one request under ``publisher``'s login and read its result off the success terminal."""
        request = await publish_request(
            self.bus,
            draft,
            _overlay(self.scene.root),
            lambda: datetime.now(UTC),
            publisher,
        )
        await _until(
            lambda: any(
                r.get("correlation_id") == str(request.request_id) for r in self.results
            )
        )
        envelope = next(
            r for r in self.results if r["correlation_id"] == str(request.request_id)
        )
        return request, ModelInstallRequestResult.model_validate(envelope["payload"])

    async def bare(self, request: ModelInstallRequest) -> dict[str, Any]:
        """Publish a request with no envelope around it and read the failure terminal's answer."""
        await self.bus.publish(
            self.topics.command,
            str(request.request_id).encode("utf-8"),
            request.model_dump_json().encode("utf-8"),
        )
        await _until(
            lambda: any(
                f["payload"].get("request_id") == str(request.request_id)
                for f in self.failures
            )
        )
        return next(
            f
            for f in self.failures
            if f["payload"].get("request_id") == str(request.request_id)
        )["payload"]


@asynccontextmanager
async def _served(open_bus: Callable[[], Any], scene: _Scene) -> AsyncIterator[_Host]:
    async with open_bus() as bus:
        host = _Host(bus, scene)
        await host.start()
        try:
            yield host
        finally:
            await host.host.stop()


def _tree(scene: _Scene) -> Path:
    return scene.root / "units" / "u1" / "tree"


async def _golden(open_bus: Callable[[], Any], tmp_path: Path) -> None:
    scene = _scene(tmp_path)
    async with _served(open_bus, scene) as served:
        request, result = await served.ask(_draft(scene.first))
    # The real installer ran: its commit is the revision, its tree holds the file the declared verify read.
    assert result.status is EnumInstallRequestStatus.VERIFIED
    assert result.revision == scene.first
    assert result.verify_line == "a1"
    assert result.rolled_back is False
    assert (_tree(scene) / "a.txt").read_text() == "a1\n"
    # The words a handoff reads match the handoff proof compute's own patterns.
    line = result.integration_line()
    installed = INSTALLED_RE.search(line)
    assert installed is not None
    assert installed.groups() == (HOST, scene.first)
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


async def _errors(open_bus: Callable[[], Any], tmp_path: Path) -> None:
    scene = _scene(tmp_path)
    async with _served(open_bus, scene) as served:
        # 1. A login the host row does not name is refused, answered and never journalled.
        forged, forged_result = await served.ask(_draft(scene.first), publisher=FORGER)
        assert forged_result.status is EnumInstallRequestStatus.REFUSED
        assert forged_result.reason == "publisher_not_allowed"
        assert served.journal.get(forged.request_id) is None
        assert not _tree(scene).exists()
        assert forged_result.integration_line() == ""
        # 2. A commit no landing branch holds is refused and the installer never ran.
        unmerged, unmerged_result = await served.ask(_draft(scene.unmerged))
        assert unmerged_result.status is EnumInstallRequestStatus.REFUSED
        assert unmerged_result.reason == "ref_not_merged"
        assert not _tree(scene).exists()
        owed = HandlerInstallRequestOwed().handle(
            ModelInstallOwedInput(
                requests=(unmerged,),
                results=(unmerged_result,),
                now=datetime.now(UTC),
            )
        )
        assert owed.owed == 1
        assert "last result refused: ref_not_merged" in owed.lines[0].line
        # 3. A request with no envelope never reaches the handler and is answered on the failure terminal.
        bare_request = _request(_draft(scene.first), expires_in=timedelta(hours=2))
        failure = await served.bare(bare_request)
        assert "no envelope" in failure["error_message"]
        assert served.journal.get(bare_request.request_id) is None
        # 4. A failed verify rolls the host back to the revision it stood at, and says so.
        _, first_result = await served.ask(_draft(scene.first))
        assert first_result.status is EnumInstallRequestStatus.VERIFIED
        assert (_tree(scene) / "b.txt").read_text() == "b1\n"
        _, failed = await served.ask(_draft(scene.second, verify=VERIFY_NEVER))
        assert failed.status is EnumInstallRequestStatus.VERIFY_FAILED
        assert failed.rolled_back is True
        assert failed.integration_line() == ""
        assert (_tree(scene) / "b.txt").read_text() == "b1\n"


def test_the_golden_chain_on_the_in_memory_bus(tmp_path: Path) -> None:
    asyncio.run(_golden(_in_memory, tmp_path))


def test_the_error_chain_on_the_in_memory_bus(tmp_path: Path) -> None:
    asyncio.run(_errors(_in_memory, tmp_path))


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
    asyncio.run(_errors(_broker, tmp_path))


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
    request = _request(_draft("a" * 40), expires_in=timedelta(hours=2))
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


def test_the_owed_reading_rides_the_throughput_tick_request_as_one_typed_field() -> (
    None
):
    """The tick prints the owed reading the way the owed compute shapes it: a shape change on either side fails here."""
    reading = HandlerInstallRequestOwed().handle(
        ModelInstallOwedInput(
            requests=(_request(_draft("a" * 40), expires_in=timedelta(hours=2)),),
            now=datetime(2026, 11, 15, 3, 0, tzinfo=UTC),
        )
    )
    tick = ModelThroughputTickRequest.model_validate(
        {
            "now": "2026-11-15T03:00:00Z",
            "launchctl": "not_loaded",
            "install_owed": reading.model_dump(mode="json"),
        }
    )
    assert tick.install_owed == reading


def test_the_unit_tree_installer_models_still_carry_the_cells_the_effect_reads() -> (
    None
):
    """The effect sends operation, unit, host and ref and reads verdict, commit and detail: a rename on either side fails."""
    assert {"operation", "unit", "host", "ref"} <= set(
        ModelUnitTreeInstallRequest.model_fields
    )
    assert {"verdict", "commit", "detail"} <= set(
        ModelUnitTreeInstallResult.model_fields
    )


def test_the_host_install_orchestrator_routes_the_same_request_and_result_on_the_effect_topics() -> (
    None
):
    """The request is a typed input of the existing orchestrator: same models, same topics as the effect."""
    orchestrator = yaml.safe_load(
        resources.files(f"{NODES}.node_host_install_orchestrator")
        .joinpath("contract.yaml")
        .read_text()
    )
    routes = {
        route["topic"]: route["event_model"]["name"]
        for route in orchestrator["handler_routing"]["handlers"]
    }
    topics = load_install_request_topics()
    assert routes[topics.command] == ModelInstallRequest.__name__
    assert routes[topics.success] == ModelInstallRequestResult.__name__


def test_a_message_can_carry_no_command_and_no_expected_line() -> None:
    """Failing control: the draft names a declared verify and nothing else; an old-shape cell is refused by the model."""
    good = _draft("a" * 40).model_dump()
    for cell in ("verify_command", "expected_line"):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            ModelInstallRequestDraft.model_validate({**good, cell: "x"})


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
    assert contract["runtime_dispatch"]["terminal_events"]["success"] == topics.success
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


def test_without_the_decision_expiry_rule_an_expired_request_would_have_been_installed(
    tmp_path: Path,
) -> None:
    """Failing control of the whole chain: the effect installs nothing for an expired request, and does when the rule is removed."""
    scene = _scene(tmp_path)

    class NoExpiry(HandlerInstallRequestDecision):
        def _expired(self, request: Any) -> bool:
            return False

    root_calls: list[str] = []

    class Installer:
        def handle(self, request: Any) -> Any:
            root_calls.append(request.ref)
            raise RuntimeError("installer reached")

    expired = ModelInstallRequestReceived(
        request=_request(_draft(scene.first), expires_in=timedelta(hours=1)),
        publisher=PUBLISHER,
    )

    def build(decision: HandlerInstallRequestDecision) -> HandlerInstallRequest:
        return HandlerInstallRequest(
            overlay=_overlay(scene.root),
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
    assert root_calls == [scene.first]
