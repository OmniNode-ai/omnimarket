# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Claude pool refill read is a declared registry module tick (OMN-24943).

The handler is replaced by a fake that records the request it was given: the tick always runs the
dry run, a pass writes the unit heartbeat, the probe touches nothing, and both platform units are
declared and render with the overlay roots in their environment.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_claude_pool_refill_effect import (
    runtime_claude_pool_refill_tick as tick,
)
from omnimarket.nodes.node_claude_pool_refill_effect.models.model_claude_pool_refill import (
    ModelClaudePoolRefillRequest,
    ModelClaudePoolRefillResult,
)
from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from tests.nodes.node_host_installer_registry_units.test_registry_units import (
    make_checkout,
    real_run,
    request,
)

pytestmark = pytest.mark.unit

ENTRY = "src/omnimarket/nodes/node_claude_pool_refill_effect/runtime_claude_pool_refill_tick.py"
MODULE = (
    "omnimarket.nodes.node_claude_pool_refill_effect.runtime_claude_pool_refill_tick"
)


class _FakeHandler:
    seen: list[ModelClaudePoolRefillRequest] = []
    answer = ModelClaudePoolRefillResult(exit_code=0, lines=("host=h201 verdict=ok",))

    def handle(self, req: ModelClaudePoolRefillRequest) -> ModelClaudePoolRefillResult:
        self.seen.append(req)
        return self.answer


@pytest.fixture(autouse=True)
def fake_handler(monkeypatch: pytest.MonkeyPatch) -> type[_FakeHandler]:
    _FakeHandler.seen = []
    _FakeHandler.answer = ModelClaudePoolRefillResult(
        exit_code=0, lines=("host=h201 verdict=ok",)
    )
    monkeypatch.setattr(tick, "HandlerClaudePoolRefill", _FakeHandler)
    return _FakeHandler


def _heartbeat(state_dir: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(
        (state_dir / "last-run.json").read_text(encoding="utf-8")
    )
    return loaded


def test_golden_a_pass_runs_the_dry_run_and_writes_the_heartbeat(
    tmp_path: Path, fake_handler: type[_FakeHandler]
) -> None:
    code = tick.main(["--state-dir", str(tmp_path)])
    assert code == 0
    assert [r.dry_run for r in fake_handler.seen] == [True]
    beat = _heartbeat(tmp_path)
    assert beat["exit_code"] == 0
    assert beat["result"]["status"] == "ok"
    assert beat["result"]["dry_run"] is True
    assert beat["result"]["lines"] == ["host=h201 verdict=ok"]


def test_live_is_the_only_way_to_leave_the_dry_run(
    tmp_path: Path, fake_handler: type[_FakeHandler]
) -> None:
    tick.main(["--live", "--state-dir", str(tmp_path)])
    assert [r.dry_run for r in fake_handler.seen] == [False]


def test_probe_writes_nothing_and_never_calls_the_handler(
    tmp_path: Path, fake_handler: type[_FakeHandler]
) -> None:
    assert tick.main(["--probe", "--state-dir", str(tmp_path)]) == 0
    assert fake_handler.seen == []
    assert not (tmp_path / "last-run.json").exists()


def test_a_host_error_is_recorded_with_its_exit_code(
    tmp_path: Path, fake_handler: type[_FakeHandler]
) -> None:
    fake_handler.answer = ModelClaudePoolRefillResult(
        exit_code=1, lines=("host=h106 verdict=error",)
    )
    assert tick.main(["--state-dir", str(tmp_path)]) == 1
    beat = _heartbeat(tmp_path)
    assert beat["exit_code"] == 1
    assert beat["result"]["status"] == "host_error"


def test_a_refusal_is_recorded_and_names_its_cause(
    tmp_path: Path, fake_handler: type[_FakeHandler]
) -> None:
    fake_handler.answer = ModelClaudePoolRefillResult(
        exit_code=2, refusal="the pool overlay cannot be found under its roots"
    )
    assert tick.main(["--state-dir", str(tmp_path)]) == 2
    beat = _heartbeat(tmp_path)
    assert beat["result"]["status"] == "refused"
    assert "pool overlay" in beat["result"]["refusal"]


@pytest.mark.parametrize(
    "unit", ["claude-pool-refill-tick-systemd", "claude-pool-refill-tick"]
)
def test_both_platform_units_are_declared_for_the_refill_tick(unit: str) -> None:
    declared = load_units()[unit]
    assert declared["module"] == MODULE
    assert declared["entry_file"] == ENTRY
    assert declared["probe_argv"] == ["--probe"]
    assert declared["schedule"]["start_interval"] == 3600
    assert "ONEX_SKILL_OVERLAY_ROOTS" in declared["environment"]
    assert "--live" not in json.dumps(declared)


def test_the_systemd_unit_renders_the_module_with_the_overlay_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # hosted CI has none of the declared host interpreters; the render does not depend on which host runs it
    monkeypatch.setattr(
        "omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units.interpreter_present",
        lambda path: path == "/usr/bin/python3.12",
    )
    branch = tmp_path / "branch"
    revision = make_checkout(branch, entry=ENTRY)
    result = HandlerRegistryUnits(real_run).handle(
        request(
            tmp_path,
            unit="claude-pool-refill-tick-systemd",
            project_path=str(branch),
            expected_revision=revision,
            path_overrides={"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "overlays")},
        )
    )
    assert result.status == "success", result.stderr
    assert f'"{MODULE}"' in result.rendered
    assert "ONEX_SKILL_OVERLAY_ROOTS=" in result.rendered
    assert "--live" not in result.rendered
