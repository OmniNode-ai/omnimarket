# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain coverage for node_env_parity_collect_effect (OMN-13925).

Drives the live-collection EFFECT end-to-end with the ssh boundary faked at
``subprocess.run`` (docker ps + docker inspect argv + output parsing are
exercised for real) and asserts the terminal output state the contract
declares. Also serves as the dep-health / state-coverage anchor for
``handler_env_parity_collect`` and the terminal event
``onex.evt.omnimarket.env-parity-collect-completed.v1``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_env_parity_collect_effect.handlers import (
    handler_env_parity_collect,
)
from omnimarket.nodes.node_env_parity_collect_effect.handlers.handler_env_parity_collect import (
    HandlerEnvParityCollect,
)
from omnimarket.nodes.node_env_parity_collect_effect.models.model_env_parity_collect_request import (
    ModelEnvParityCollectRequest,
)

# The terminal output state the contract declares (event_bus.publish_topics /
# terminal_event). Asserted below so the state-coverage gate maps this node's
# declared output to a covering test.
_TERMINAL_EVENT = "onex.evt.omnimarket.env-parity-collect-completed.v1"

_CONTRACT = yaml.safe_load(
    (
        handler_env_parity_collect.Path(handler_env_parity_collect.__file__)
        .resolve()
        .parents[1]
        / "contract.yaml"
    ).read_text(encoding="utf-8")
)
_VARIABLES = [rule["name"] for rule in _CONTRACT["env_parity"]["variables"]]
_LANES = {lane["name"]: lane for lane in _CONTRACT["lane_collection"]["lanes"]}


def _lane_env(lane: str) -> list[str]:
    values = {name: f"{name.lower()}-{lane}" for name in _VARIABLES}
    values["QDRANT_PORT"] = "6333"
    values["VALKEY_PORT"] = "6379"
    return [f"{key}={value}" for key, value in values.items()]


def _fake_docker(running: list[str]) -> tuple[str, str]:
    ps, inspect = [], []
    for lane in running:
        project = _LANES[lane]["compose_project"]
        ps.append(f"runtime-{lane}\t{lane}cid\t{project}")
        inspect.append(
            f"/runtime-{lane} {json.dumps(_lane_env(lane), separators=(',', ':'))}"
        )
    return "\n".join(ps) + "\n", "\n".join(inspect) + "\n"


@pytest.mark.unit
def test_golden_chain_collects_live_lanes_and_declares_terminal_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    running = ["stability-test", "prod", "judge"]  # dev (optional) is down
    ps_stdout, inspect_stdout = _fake_docker(running)

    def fake_run(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        remote = argv[-1]
        if remote.startswith("docker ps"):
            return SimpleNamespace(returncode=0, stdout=ps_stdout, stderr="")
        if remote.startswith("docker inspect"):
            return SimpleNamespace(returncode=0, stdout=inspect_stdout, stderr="")
        raise AssertionError(remote)

    monkeypatch.setattr(handler_env_parity_collect.subprocess, "run", fake_run)

    result = HandlerEnvParityCollect().handle(
        ModelEnvParityCollectRequest(ssh_target="ops@lane-host.test")
    )

    assert result.status == "passed"
    assert result.parity_ok is True
    assert result.collection_source == "live-ssh-docker-inspect"
    assert {c.lane for c in result.lane_collections} == set(_LANES)

    # Contract declares the terminal event as the node's output state; the
    # collect receipt is the runtime payload wrapped into that terminal event.
    assert _CONTRACT["terminal_event"] == _TERMINAL_EVENT
    assert _TERMINAL_EVENT in _CONTRACT["event_bus"]["publish_topics"]
