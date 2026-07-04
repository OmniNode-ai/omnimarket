# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Coverage for node_env_parity_collect_effect (OMN-13925).

The defect class under enforcement: env_parity emitting a parity verdict from
sample/static lane data while claiming to describe the live runtime lanes.
These tests prove the collect EFFECT:

1. fails fast (typed error, no verdict) when no live collection input exists,
2. fails fast when live collection yields ZERO lane containers (a verdict over
   zero snapshots is vacuous),
3. produces a parity verdict ONLY from freshly collected snapshots, carrying
   verifiable provenance (UTC timestamps, lane ids, container name/id), and
4. never leaks raw env values (secrets) into the typed receipt.

The ssh boundary is faked at ``subprocess.run`` so the docker probe argv and
output parsing are exercised for real; nothing else is mocked.
"""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
import yaml

from omnimarket.nodes.node_env_parity_collect_effect.handlers import (
    handler_env_parity_collect as handler_module,
)
from omnimarket.nodes.node_env_parity_collect_effect.handlers.handler_env_parity_collect import (
    HandlerEnvParityCollect,
)
from omnimarket.nodes.node_env_parity_collect_effect.models.model_env_parity_collect_request import (
    ModelEnvParityCollectRequest,
)

_SECRET_VALUE = "super-secret-postgres-password"  # onex-allow-test-fixture OMN-13925 reason="synthetic lane env value; test proves collected secrets never leak into the receipt"

_CONTRACT = yaml.safe_load(
    (
        handler_module.Path(handler_module.__file__).resolve().parents[1]
        / "contract.yaml"
    ).read_text(encoding="utf-8")
)
_VARIABLES = [rule["name"] for rule in _CONTRACT["env_parity"]["variables"]]
_LANES = {lane["name"]: lane for lane in _CONTRACT["lane_collection"]["lanes"]}


def _lane_env(lane: str) -> list[str]:
    """A complete runtime env for one lane covering every contract variable."""
    values = {name: f"{name.lower()}-{lane}" for name in _VARIABLES}
    values["POSTGRES_PASSWORD"] = _SECRET_VALUE
    values["QDRANT_PORT"] = "6333"
    values["VALKEY_PORT"] = "6379"
    return [f"{key}={value}" for key, value in values.items()]


def _fake_docker_outputs(
    running_lanes: list[str],
) -> tuple[str, str]:
    """Build fake `docker ps` and `docker inspect` stdout for running lanes."""
    ps_lines = []
    inspect_lines = []
    for lane in running_lanes:
        project = _LANES[lane]["compose_project"]
        container = f"runtime-{lane}"
        ps_lines.append(f"{container}\t{lane}cid123\t{project}")
        # docker inspect separator is a space (see handler NOTE): the env JSON
        # is compact (no spaces after separators), matching {{json ...}}.
        inspect_lines.append(
            f"/{container} {json.dumps(_lane_env(lane), separators=(',', ':'))}"
        )
    return "\n".join(ps_lines) + "\n", "\n".join(inspect_lines) + "\n"


def _install_fake_ssh(
    monkeypatch: pytest.MonkeyPatch,
    ps_stdout: str,
    inspect_stdout: str,
    *,
    returncode: int = 0,
) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        calls.append(argv)
        assert argv[0] == "ssh"
        assert "BatchMode=yes" in argv
        remote_command = argv[-1]
        if returncode != 0:
            return SimpleNamespace(
                returncode=returncode, stdout="", stderr="probe refused"
            )
        if remote_command.startswith("docker ps"):
            return SimpleNamespace(returncode=0, stdout=ps_stdout, stderr="")
        if remote_command.startswith("docker inspect"):
            return SimpleNamespace(returncode=0, stdout=inspect_stdout, stderr="")
        raise AssertionError(f"unexpected remote command: {remote_command}")

    monkeypatch.setattr(handler_module.subprocess, "run", fake_run)
    return calls


@pytest.mark.integration
def test_contract_lane_topology_is_self_consistent() -> None:
    """env_parity lanes and lane_collection lanes must be the same set."""
    parity_lanes = set(_CONTRACT["env_parity"]["lanes"])
    collect_lanes = set(_LANES)
    assert parity_lanes == collect_lanes
    # The four census lanes of the runtime host must all be collectable.
    assert collect_lanes == {"dev", "stability-test", "prod", "judge"}
    # dev is the ephemeral developer lane — optional per the lane census.
    assert _LANES["dev"].get("optional") is True
    assert not _LANES["prod"].get("optional", False)


@pytest.mark.integration
def test_fails_fast_without_live_collection_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ssh target anywhere → typed error, no verdict, no sample fallback."""
    monkeypatch.delenv("ONEX_LANE_SSH_TARGET", raising=False)
    ran: list[list[str]] = []

    def forbidden_run(*args: Any, **kwargs: Any) -> SimpleNamespace:
        ran.append(list(args))
        raise AssertionError("subprocess must not run without a live target")

    monkeypatch.setattr(handler_module.subprocess, "run", forbidden_run)

    result = HandlerEnvParityCollect().handle(ModelEnvParityCollectRequest())

    assert result.status == "error"
    assert result.parity_ok is False
    assert result.parity is None, "must not fabricate a parity verdict"
    assert result.lane_collections == []
    assert result.error is not None
    assert "no live collection input was provided" in result.error
    assert ran == [], "must not probe anything without a target"


@pytest.mark.integration
def test_fails_fast_on_zero_collected_lanes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero running lane containers → typed error, never a vacuous verdict."""
    _install_fake_ssh(monkeypatch, ps_stdout="\n", inspect_stdout="")

    result = HandlerEnvParityCollect().handle(
        ModelEnvParityCollectRequest(ssh_target="ops@lane-host.test")
    )

    assert result.status == "error"
    assert result.parity is None
    assert result.error is not None
    assert "zero running lane runtime containers" in result.error


@pytest.mark.integration
def test_fails_fast_on_ssh_probe_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_ssh(monkeypatch, "", "", returncode=255)

    result = HandlerEnvParityCollect().handle(
        ModelEnvParityCollectRequest(ssh_target="ops@lane-host.test")
    )

    assert result.status == "error"
    assert result.error is not None
    assert "live container listing failed" in result.error


@pytest.mark.integration
def test_live_collection_produces_provenance_backed_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: verdict derives from live snapshots with full provenance."""
    running = ["stability-test", "prod", "judge"]  # dev (optional) is down
    ps_stdout, inspect_stdout = _fake_docker_outputs(running)
    calls = _install_fake_ssh(monkeypatch, ps_stdout, inspect_stdout)
    correlation_id = uuid4()

    result = HandlerEnvParityCollect().handle(
        ModelEnvParityCollectRequest(
            correlation_id=correlation_id, ssh_target="ops@lane-host.test"
        )
    )

    # Two read-only probes: docker ps then docker inspect.
    assert len(calls) == 2
    assert "docker ps --filter" in calls[0][-1]
    assert "docker inspect --format" in calls[1][-1]

    assert result.status == "passed"
    assert result.parity_ok is True
    assert result.collection_source == "live-ssh-docker-inspect"
    assert result.ssh_target == "ops@lane-host.test"
    assert result.collected_at is not None
    assert result.correlation_id == correlation_id

    by_lane = {entry.lane: entry for entry in result.lane_collections}
    assert set(by_lane) == {"dev", "stability-test", "prod", "judge"}
    for lane in running:
        entry = by_lane[lane]
        assert entry.collected is True
        assert entry.container_name == f"runtime-{lane}"
        assert entry.container_id == f"{lane}cid123"
        assert entry.env_var_count == len(_VARIABLES)
        assert entry.collected_at is not None
    # dev was down: provenance recorded, optional, and NOT a parity gap.
    assert by_lane["dev"].collected is False
    assert by_lane["dev"].optional is True

    assert result.parity is not None
    assert sorted(result.parity.lanes_checked) == sorted(running)
    assert result.parity.gaps == []
    assert result.parity.variables_checked == sorted(_VARIABLES)

    # Raw env values (secrets) must never appear in the typed receipt.
    assert _SECRET_VALUE not in result.model_dump_json()


@pytest.mark.integration
def test_required_lane_down_is_a_real_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A required lane with no runtime container surfaces lane_missing gaps."""
    running = ["stability-test", "judge"]  # prod (required) is down
    ps_stdout, inspect_stdout = _fake_docker_outputs(running)
    _install_fake_ssh(monkeypatch, ps_stdout, inspect_stdout)

    result = HandlerEnvParityCollect().handle(
        ModelEnvParityCollectRequest(ssh_target="ops@lane-host.test")
    )

    assert result.status == "gaps_detected"
    assert result.parity_ok is False
    assert result.parity is not None
    prod_reasons = {gap.reason for gap in result.parity.gaps if gap.lane == "prod"}
    assert prod_reasons == {"lane_missing"}
    by_lane = {entry.lane: entry for entry in result.lane_collections}
    assert by_lane["prod"].collected is False


@pytest.mark.integration
def test_timeout_produces_typed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_timeout(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        raise subprocess.TimeoutExpired(cmd=argv, timeout=1)

    monkeypatch.setattr(handler_module.subprocess, "run", raise_timeout)

    result = HandlerEnvParityCollect().handle(
        ModelEnvParityCollectRequest(ssh_target="ops@lane-host.test")
    )

    assert result.status == "error"
    assert result.error is not None
    assert "timed out" in result.error
