# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22351: ``onex scm`` reads its broker from the node's contract overlay and never hosts a
source-control node on a host whose lane binding is degraded.

Operator rulings 2026-11-03: "The node should be in the runtime." and "The default broker should be
in the contract overlay." Before this, every host exited 69 "names no broker" without ``--bus-lane``,
and a host with no lane login (h106) fell back to hosting node_github_repo_write_effect in process,
which then failed credential_unavailable looking up a GitHub token the host must never hold.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from click.testing import CliRunner, Result

from omnimarket.delegated_test_loop.lab_run_bus import ProtocolLabRunBus
from omnimarket.delegated_test_loop.lane_bus import LabRunBusError
from omnimarket.handlers.bus_binding import select
from omnimarket.handlers.bus_binding.node_event_bus_overlay import (
    read_node_event_bus,
)
from omnimarket.handlers.bus_binding.select import BoundBus
from omnimarket.handlers.scm import scm_cli
from omnimarket.models.bus_binding import (
    EnumBusBinding,
    EnumBusBindingState,
    ModelBusBindingDecision,
)
from omnimarket.models.scm import EnumScmNode

pytestmark = pytest.mark.unit

WRITE = EnumScmNode.WRITE.value


def _workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overlays: dict[str, str]
) -> Path:
    """A workspace root whose config root holds ``overlays`` (node name -> overlay text)."""
    config_root = tmp_path / "config-owner"
    for node, text in overlays.items():
        path = config_root / "config" / "onex" / "overlays" / node / "overlay.yaml"
        path.parent.mkdir(parents=True)
        path.write_text(text, encoding="utf-8")
    config_root.mkdir(exist_ok=True)
    monkeypatch.setenv("ONEX_WORKSPACE_CONFIG_ROOT", str(config_root))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


REDPANDA_OVERLAY = (
    "comment_author_logins:\n  - a-login\nevent_bus:\n  type: kafka\n  lane: dev\n"
)
INMEMORY_OVERLAY = "event_bus:\n  type: inmemory\n"


def _decision(state: EnumBusBindingState, reason: str = "") -> ModelBusBindingDecision:
    return ModelBusBindingDecision(
        lane="dev",
        binding=EnumBusBinding.DURABLE
        if state is EnumBusBindingState.HEALTHY
        else EnumBusBinding.INMEMORY,
        state=state,
        prior_state=EnumBusBindingState.HEALTHY,
        reason=reason,
        observed_at=datetime.now(UTC),
    )


def _bind(
    monkeypatch: pytest.MonkeyPatch,
    state: EnumBusBindingState,
    seen: dict[str, object],
    reason: str = "",
) -> object:
    """Replace the binding selector; record the lane it was asked for and yield a sentinel bus."""
    sentinel = object()

    @contextlib.asynccontextmanager
    async def bound(**kwargs: object) -> AsyncIterator[BoundBus]:
        seen.update(kwargs)
        yield BoundBus(cast(ProtocolLabRunBus, sentinel), _decision(state, reason))

    monkeypatch.setattr(select, "open_bound_bus", bound)
    return sentinel


def _opts(workspace: Path, **override: object) -> dict[str, object]:
    return {
        "bus": "kafka",
        "bus_lane": None,
        "kafka_bootstrap": None,
        "omnibase_path": workspace,
        "scm_node": EnumScmNode.WRITE,
        **override,
    }


async def _enter(opts: dict[str, object]) -> object:
    async with scm_cli._open_bus(opts) as bus:
        return bus


# ------------------------------------------------------- the overlay selects


def test_redpanda_overlay_lane_is_dialed_with_no_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path, monkeypatch, {WRITE: REDPANDA_OVERLAY})
    seen: dict[str, object] = {}
    sentinel = _bind(monkeypatch, EnumBusBindingState.HEALTHY, seen)

    assert asyncio.run(_enter(_opts(workspace))) is sentinel
    assert seen["bus"] == "kafka"
    assert seen["lane"] == "dev"
    assert seen["kafka_bootstrap"] is None


def test_the_overlay_keeps_its_other_readers_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _workspace(tmp_path, monkeypatch, {WRITE: REDPANDA_OVERLAY})
    path = tmp_path / "config-owner/config/onex/overlays" / WRITE / "overlay.yaml"
    declared = read_node_event_bus(path)
    assert declared is not None
    assert (declared.type, declared.lane) == ("kafka", "dev")


def test_an_explicit_bus_lane_wins_over_the_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(
        tmp_path,
        monkeypatch,
        {WRITE: "event_bus:\n  type: kafka\n  lane: other-lane\n"},
    )
    seen: dict[str, object] = {}
    _bind(monkeypatch, EnumBusBindingState.HEALTHY, seen)

    asyncio.run(_enter(_opts(workspace, bus_lane="dev")))
    assert seen["lane"] == "dev"


def test_gateway_reads_use_the_gateway_nodes_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(
        tmp_path,
        monkeypatch,
        {EnumScmNode.GATEWAY.value: "event_bus:\n  type: kafka\n  lane: dev\n"},
    )
    seen: dict[str, object] = {}
    _bind(monkeypatch, EnumBusBindingState.HEALTHY, seen)

    asyncio.run(_enter(_opts(workspace, scm_node=EnumScmNode.GATEWAY)))
    assert seen["lane"] == "dev"


def test_inmemory_overlay_hosts_the_node_and_answers_a_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The in-memory overlay: no flag, no broker, a real terminal from the canonical node."""
    workspace = _workspace(tmp_path, monkeypatch, {WRITE: INMEMORY_OVERLAY})
    monkeypatch.delenv("BIFROST_BASE_CONTRACT_PATH", raising=False)
    result = _pr_create_dry_run(workspace)
    assert result.exit_code == 0, result.output
    answer = json.loads(result.stdout.strip().splitlines()[-1])
    assert answer["outcome"] == "completed"
    assert answer["result"]["mode"] == "dry_run"
    assert answer["bus"]["terminal_topic"].endswith("github-repo-write-completed.v1")


# --------------------------------------------------------- refusals, exit 69


def _pr_create_dry_run(workspace: Path, *extra: str) -> Result:
    return CliRunner().invoke(
        scm_cli.scm_group,
        [
            "pr-create",
            "OmniNode-ai/omnimarket",
            "--head",
            "omn-21655-overlay-probe",
            "--base",
            "dev",
            "--title",
            "Overlay probe",
            "--body",
            "Dry run",
            "--lane",
            "scm-overlay-test",
            "--omnibase-path",
            str(workspace),
            "--timeout-s",
            "0.1",
            "--dry-run",
            *extra,
        ],
        catch_exceptions=False,
    )


def test_no_overlay_declaration_refuses_naming_the_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(
        tmp_path, monkeypatch, {WRITE: "comment_author_logins: []\n"}
    )
    seen: dict[str, object] = {}
    _bind(monkeypatch, EnumBusBindingState.HEALTHY, seen)

    result = _pr_create_dry_run(workspace)
    assert result.exit_code == scm_cli.EXIT_BUS_UNAVAILABLE
    assert "declares no event_bus" in result.stderr
    assert f"overlays/{WRITE}/overlay.yaml" in result.stderr
    assert seen == {}


def test_a_malformed_overlay_is_refused_not_read_as_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(
        tmp_path, monkeypatch, {WRITE: "event_bus:\n  type: kafka\n"}
    )
    with pytest.raises(LabRunBusError, match="invalid event_bus"):
        asyncio.run(_enter(_opts(workspace)))


def test_a_degraded_binding_refuses_and_hosts_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """h106's case: no lane login, the binding degrades, and the write node is not hosted here."""
    workspace = _workspace(tmp_path, monkeypatch, {WRITE: REDPANDA_OVERLAY})
    seen: dict[str, object] = {}
    _bind(
        monkeypatch,
        EnumBusBindingState.DEGRADED,
        seen,
        reason="LabRunBusError: this machine holds no identity for it",
    )

    def no_host(*_: object, **__: object) -> None:
        raise AssertionError("a source-control node was hosted in process")

    monkeypatch.setattr(
        "omnimarket.handlers.scm.scm_inprocess.open_scm_inprocess", no_host
    )
    result = _pr_create_dry_run(workspace)
    assert result.exit_code == scm_cli.EXIT_BUS_UNAVAILABLE
    assert seen["lane"] == "dev"
    assert f"never hosts {WRITE} on this host" in result.stderr
    assert "needs its own bus login on lane 'dev'" in result.stderr
    assert "holds no identity" in result.stderr


def test_a_degraded_read_is_refused_once_without_retry_pauses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(
        tmp_path,
        monkeypatch,
        {EnumScmNode.GATEWAY.value: "event_bus:\n  type: kafka\n  lane: dev\n"},
    )
    seen: dict[str, object] = {}
    _bind(monkeypatch, EnumBusBindingState.DEGRADED, seen, reason="no login")

    def no_sleep(_: float) -> None:
        raise AssertionError("a refusal no retry can change was retried")

    monkeypatch.setattr(scm_cli.time, "sleep", no_sleep)
    result = CliRunner().invoke(
        scm_cli.scm_group,
        [
            "pr-review",
            "OmniNode-ai/omnimarket#1",
            "--source",
            "bus",
            "--omnibase-path",
            str(workspace),
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == scm_cli.EXIT_BUS_UNAVAILABLE
    assert "never hosts node_github_repo_gateway_effect" in result.stderr
