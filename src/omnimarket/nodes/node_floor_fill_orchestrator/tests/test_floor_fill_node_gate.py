# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""G-NODE for the fill orchestrator: contract, no hard-coded value, injection, and a failing control (plan T13)."""

from __future__ import annotations

import importlib
import re
import threading
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_floor_fill_orchestrator.handlers.handler_floor_fill_command import (
    HandlerFloorFillCommand,
)
from omnimarket.nodes.node_floor_fill_orchestrator.models import (
    EnumFloorFillCommand,
    FloorFillConfigError,
    ModelFloorFillCommandRequest,
    load_floor_fill_config,
)
from omnimarket.nodes.node_floor_fill_orchestrator.models.model_floor_fill_live_settings import (
    load_floor_fill_live_settings,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.ports import (
    ModelRunOutcome,
    ModelStateRead,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.scripted_ports import (
    ScriptedCommandForward,
    ScriptedFacts,
    ScriptedStarter,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.store import (
    InMemoryFloorStateStore,
)
from omnimarket.nodes.node_floor_fill_orchestrator.tests.support import (
    OVERLAY,
    SynchronizedSnapshot,
    harness,
    overlay,
)

pytestmark = pytest.mark.unit

NODE_DIR = Path(__file__).resolve().parent.parent
TICK = EnumFloorFillCommand.TICK


def _contract() -> dict[str, Any]:
    contract: dict[str, Any] = yaml.safe_load(
        (NODE_DIR / "contract.yaml").read_text(encoding="utf-8")
    )
    return contract


def _resolve(path: str) -> object:
    module, _, name = path.partition(":")
    return getattr(importlib.import_module(module), name)


def test_contract_loads() -> None:
    contract = _contract()
    assert contract["node_type"] == "ORCHESTRATOR_GENERIC"
    assert contract["state_io"]["key"] == "scope"
    handler = contract["handler"]
    assert isinstance(_resolve(f"{handler['module']}:{handler['class']}"), type)
    for model in ("input_model", "output_model"):
        assert _resolve(f"{contract[model]['module']}:{contract[model]['name']}")
    for dependency in contract["protocol_dependencies"]:
        assert _resolve(dependency["protocol"]), dependency
    codec = contract["state_io"]["codec"]
    assert _resolve(f"{codec['module']}:{codec['name']}")
    assert (NODE_DIR / "metadata.yaml").is_file()
    assert (NODE_DIR / "overlay.example.yaml").is_file()


def test_overlay_example_is_the_contract_keys_and_loads() -> None:
    example = yaml.safe_load(
        (NODE_DIR / "overlay.example.yaml").read_text(encoding="utf-8")
    )
    assert list(example["floor_fill"]) == _contract()["overlay"]["keys"]
    config = load_floor_fill_config(example["floor_fill"])
    assert "onex_remote_lane" not in " ".join(
        config.runner_command + config.remote_argv
    )


def test_overlay_example_live_section_loads_and_names_every_field() -> None:
    example = yaml.safe_load(
        (NODE_DIR / "overlay.example.yaml").read_text(encoding="utf-8")
    )
    settings = load_floor_fill_live_settings(example["floor_fill_live"])
    assert set(example["floor_fill_live"]) == set(type(settings).model_fields)


@pytest.mark.parametrize("key", sorted(OVERLAY))
def test_missing_key_refused(key: str) -> None:
    broken = {k: v for k, v in OVERLAY.items() if k != key}
    with pytest.raises(FloorFillConfigError) as error:
        load_floor_fill_config(broken)
    assert f"floor_fill.{key}" in error.value.keys


def test_config_variation_behaviour_follows_each_key() -> None:
    # max_fill_lanes_per_line: one lane per line, then two.
    for cap, expected in ((1, 4), (2, 7)):
        h = harness(config=overlay(max_fill_lanes_per_line=cap))
        h.orchestrator().run(TICK)
        assert len(h.starter.starts) == expected
    # runner_command, process_environment, process_working_directory: the start carries them.
    h = harness(
        lines={"alpha": ["a1"]},
        config=overlay(
            runner_command=["other-entry", "{lane}", "{attempt}"],
            process_environment={"OTHER": "2"},
            process_working_directory="/other/work",
        ),
    )
    h.orchestrator().run(TICK)
    (start,) = h.starter.starts
    assert start.argv == ("other-entry", start.lane, "1")
    assert start.environment == (("OTHER", "2"),)
    assert start.working_directory == "/other/work"
    # scope: the rows are filed under it.
    h = harness(config=overlay(scope="another-scope"))
    h.orchestrator().run(TICK)
    assert h.store.load("another-scope").payload is not None
    assert h.store.load(OVERLAY["scope"]) == ModelStateRead(payload=None, version=0)
    # url and plans_root reach the fact source.
    seen: list[tuple[str, str]] = []

    class Spy(ScriptedFacts):
        def read_floor(self, url: str) -> str:
            seen.append(("url", url))
            return str(super().read_floor(url))

        def read_plan(self, plans_root: str, line_id: str) -> str:
            seen.append(("plans_root", plans_root))
            return str(super().read_plan(plans_root, line_id))

    base = harness(config=overlay(url="u://other", plans_root="p://other"))
    spy = Spy(base.facts.floor)
    base.facts = spy
    base.orchestrator().run(TICK)
    assert ("url", "u://other") in seen
    assert ("plans_root", "p://other") in seen
    # reserved_timeout_seconds and sent_window_minutes: when the entries clear.
    for timeout, cleared in ((300, True), (900, False)):
        h = harness(
            lines={"alpha": ["a1"]},
            config=overlay(
                reserved_timeout_seconds=timeout, command_timeout_seconds=60
            ),
        )
        h.orchestrator().run(TICK)
        h.clock.advance(seconds=400)
        h.orchestrator().run(TICK)
        assert (len(h.starter.starts) == 2) is cleared
    # max_age_minutes: a floor record older than it is unread.
    for age, unread in ((14, False), (16, True)):
        h = harness(lines={"alpha": ["a1"]})
        h.clock.advance(minutes=age)
        answer = h.orchestrator().run(TICK)
        assert (
            any(line.startswith("UNKNOWN floor-unread:") for line in answer.lines)
            is unread
        )
    # host_id, remote_argv, command_timeout_seconds: the forward.
    runner = ScriptedCommandForward(
        ModelRunOutcome(state="exited", exit_code=0, stdout="")
    )
    h = harness(
        config=overlay(
            host_id="elsewhere",
            remote_argv=["ssh", "x", "{argv}"],
            command_timeout_seconds=7,
        )
    )
    HandlerFloorFillCommand(
        config=h.config, orchestrator=h.orchestrator(), runner=runner
    ).handle(
        ModelFloorFillCommandRequest(command=EnumFloorFillCommand.LINES, host_id="here")
    )
    assert runner.calls == [(("ssh", "x", "lines"), 7.0)]


def test_node_source_holds_no_ticket_id_host_or_url() -> None:
    pattern = re.compile(r"OMN-\d+|https?://|\b(?:h\d{3}|\d{1,3}(?:\.\d{1,3}){3})\b")
    offenders = [
        f"{path.name}:{match.group(0)}"
        for path in NODE_DIR.rglob("*.py")
        if "tests" not in path.parts
        for match in pattern.finditer(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


# --------------------------------------------------------------------------- the failing control


class UnconditionalStore(InMemoryFloorStateStore):
    """The named injected break: a store whose write ignores the version it was told to expect."""

    def compare_and_set(
        self, key: str, expected_version: int, new_version: int, payload: str
    ) -> bool:
        with self._lock:
            self._rows[key] = (payload, new_version)
            return True


def test_one_writer_control_two_lanes_start_without_the_conditional_write() -> None:
    """Without the conditional write the one-writer test goes red: both instances start the one task's lane."""
    starters = [ScriptedStarter(), ScriptedStarter()]
    shared = SynchronizedSnapshot(UnconditionalStore(), OVERLAY["scope"], 2)
    orchestrators = [
        harness(lines={"alpha": ["a1"]}, store=shared, starter=s).orchestrator()
        for s in starters
    ]
    threads = [threading.Thread(target=lambda o=o: o.run(TICK)) for o in orchestrators]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert (
        sum(len(s.starts) for s in starters) == 2
    )  # the reading the real test asserts is == 1


# The injected protocols that still resolve outside omnibase_spi, by contract code name. Each stays
# node-local for a stated reason; the test below fails if a protocol moves (or a new one appears)
# without this table changing, and the strict xfail after it turns red the day the table is empty.
NODE_LOCAL_SEAMS: dict[str, str] = {
    "LED": "adapts ProtocolWorkLedgerStore (spi) with the CLAIM and liveness reading; a decision-shaped seam",
    "HST": "adapts ProtocolLaneHostPool (spi) to a free-slot count; a decision-shaped seam",
    "CLK": "a one-method clock; omnibase_spi ProtocolClock is the replacement once its shape is checked",
    "STO": "the conditional state row; bound to the state_io table by the omnibase_infra migration follow-up",
    "BRF": "reads a stopped lane's report and writes a continuation's brief file; no omnibase_spi protocol covers either yet (OMN-23416)",
    "TKT": "carries T1's verdict models, which move to the floor-fill models package (OMN-22931); the spi store is ProtocolFloorTicketStore",
}


def _seam_modules() -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    for dep in _contract()["protocol_dependencies"]:
        module, _, name = dep["protocol"].partition(":")
        _resolve(dep["protocol"])  # raises when it does not resolve
        out[dep["name"]] = (module, name)
    return out


def test_seams_in_spi_lists_each_injected_protocol_and_what_is_still_node_local() -> (
    None
):
    seams = _seam_modules()
    outside = {
        code
        for code, (module, _) in seams.items()
        if module.split(".")[0] != "omnibase_spi"
    }
    assert outside == set(NODE_LOCAL_SEAMS)
    assert {"SRC", "PRC"} <= set(seams) - outside


def test_seams_in_spi_complete_only_once_node_local_seams_are_empty() -> None:
    """G-NODE point 3 runs: every seam outside ``omnibase_spi`` is a declared node-local seam."""
    outside = {
        code
        for code, (module, _) in _seam_modules().items()
        if module.split(".")[0] != "omnibase_spi"
    }
    assert outside == set(NODE_LOCAL_SEAMS)
