# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The worker-engine choice never returns a stopped or disallowed engine (T-A5, OMN-21958).

The stopped engines are supplied facts (the open stops and the caller's disallowed list) and the engine ids
are the ladder contract's; the node holds no engine name. The injected placement API stands in for the
caller's placement module exactly as in the node's golden-chain tests.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
import yaml

from omnimarket.nodes.node_landing_worker_engine_compute import (
    HandlerLandingWorkerEngine,
)
from omnimarket.nodes.node_landing_worker_engine_compute.models import (
    ModelLandingWorkerEngineRequest,
)
from tests.nodes.node_andon_stop_reducer.stop_gate_facts import (
    SWARM,
    facts,
    stop,
)
from tests.nodes.node_landing_worker_engine_compute.test_golden_chain_landing_worker_engine_compute import (
    CODEX_HOSTS,
    READINGS,
    placement,
)

pytestmark = pytest.mark.unit

CLAUDE = "claude_opus"
CODEX = "codex_high"


def request(api: Mock, **over: Any) -> ModelLandingWorkerEngineRequest:
    return ModelLandingWorkerEngineRequest(
        **{
            "readings": READINGS,
            "requested_engine": CLAUDE,
            "codex_first": False,
            "cause": False,
            "codex_allowed": CODEX_HOSTS,
            "env": {},
            "placement": api,
            **over,
        }
    )


def picks() -> tuple[Any, Any]:
    return (
        SimpleNamespace(engine=CLAUDE, host=SimpleNamespace(name="host-a")),
        SimpleNamespace(engine=CODEX, host=SimpleNamespace(name="host-b")),
    )


def test_stop_gate_with_nothing_supplied_the_choice_is_today_s() -> None:
    claude, _ = picks()
    api = placement(claude=claude)
    result = HandlerLandingWorkerEngine().handle(request(api))
    assert result.pick is claude
    assert "engine_refused" not in result.record


@pytest.mark.parametrize("route", ["claude", "codex_first", "codex_fallback"])
def test_stop_gate_a_stopped_engine_is_never_returned_on_any_route(
    tmp_path: Path, route: str
) -> None:
    claude, codex = picks()
    api = placement(claude=claude, codex=codex)
    stopped = {"claude": CLAUDE, "codex_first": CODEX, "codex_fallback": CODEX}[route]
    over: dict[str, Any] = {"stop_gate": facts(tmp_path, "enforce", stop(key=stopped))}
    if route == "codex_first":
        over["codex_first"] = True
    if route == "codex_fallback":
        # the Claude route is stopped too, so the fallback is the only way to a pick
        over["stop_gate"] = facts(
            tmp_path, "enforce", stop(key=CLAUDE), stop(key=CODEX)
        )
    result = HandlerLandingWorkerEngine().handle(request(api, **over))
    assert result.pick is None or result.pick.engine != stopped
    if route == "claude":
        assert result.pick is codex
        assert CLAUDE in result.record["engine_refused"]
        assert stop(key=CLAUDE).id in result.record["engine_refused"][CLAUDE]
        assert f"swarm={SWARM}" in result.record["engine_refused"][CLAUDE]
    if route == "codex_first":
        assert result.pick is not codex
        assert CODEX in result.record["engine_refused"]
    if route == "codex_fallback":
        assert result.pick is None
        assert set(result.record["engine_refused"]) == {CLAUDE, CODEX}


@pytest.mark.parametrize("route", ["claude", "codex_first", "codex_fallback"])
def test_stop_gate_a_disallowed_engine_is_never_returned_on_any_route(
    route: str,
) -> None:
    claude, codex = picks()
    api = placement(claude=claude, codex=codex)
    over: dict[str, Any] = {"disallowed_engines": (CLAUDE,)}
    if route == "codex_first":
        over = {"disallowed_engines": (CODEX,), "codex_first": True}
    if route == "codex_fallback":
        over = {"disallowed_engines": (CLAUDE, CODEX)}
    result = HandlerLandingWorkerEngine().handle(request(api, **over))
    banned = set(over["disallowed_engines"])
    assert result.pick is None or result.pick.engine not in banned
    assert set(result.record["engine_refused"]) <= banned


def test_stop_gate_observe_mode_refuses_nothing_and_records_the_stop(
    tmp_path: Path,
) -> None:
    claude, _ = picks()
    api = placement(claude=claude)
    result = HandlerLandingWorkerEngine().handle(
        request(api, stop_gate=facts(tmp_path, "observe", stop(key=CLAUDE)))
    )
    assert result.pick is claude
    assert stop(key=CLAUDE).id in result.record["stop_observed"][CLAUDE]


def test_stop_gate_config_the_same_stop_refuses_under_enforce_and_admits_under_observe(
    tmp_path: Path,
) -> None:
    claude, _ = picks()
    api = placement(claude=claude)
    enforced = HandlerLandingWorkerEngine().handle(
        request(api, stop_gate=facts(tmp_path / "e", "enforce", stop(key=CLAUDE)))
    )
    observed = HandlerLandingWorkerEngine().handle(
        request(api, stop_gate=facts(tmp_path / "o", "observe", stop(key=CLAUDE)))
    )
    assert enforced.pick is None
    assert observed.pick is claude


def test_stop_gate_exemption_the_swarm_lane_as_recorded_may_take_the_engine(
    tmp_path: Path,
) -> None:
    claude, _ = picks()
    api = placement(claude=claude)
    admitted = HandlerLandingWorkerEngine().handle(
        request(
            api,
            stop_gate=facts(tmp_path / "a", "enforce", stop(key=CLAUDE), lane_id=SWARM),
        )
    )
    forged = HandlerLandingWorkerEngine().handle(
        request(
            api,
            stop_gate=facts(
                tmp_path / "f", "enforce", stop(key=CLAUDE), lane_id="lane-forged"
            ),
        )
    )
    assert admitted.pick is claude
    assert forged.pick is None


def test_stop_gate_the_request_serializes_with_the_stop_facts(tmp_path: Path) -> None:
    gate = facts(tmp_path, "enforce", stop(key=CLAUDE))
    restored = type(gate).model_validate_json(gate.model_dump_json())
    assert restored == gate


def test_stop_gate_the_node_carries_no_engine_name_literal() -> None:
    root = Path(__file__).resolve().parents[3] / (
        "src/omnimarket/nodes/node_landing_worker_engine_compute"
    )
    ladder = yaml.safe_load(
        (root.parent / "node_remote_lane_compute" / "contract.yaml").read_text(
            encoding="utf-8"
        )
    )["config"]["engine_ladder"]
    names = {e["rung"] for e in ladder} | {e["host_engine"] for e in ladder}
    handler = (root / "handlers/handler_landing_worker_engine.py").read_text(
        encoding="utf-8"
    )
    code = "\n".join(
        line for line in handler.splitlines() if not line.lstrip().startswith("#")
    )
    code = re.sub(r'""".*?"""', "", code, flags=re.S)
    code = re.sub(r"[A-Z_]+ = \"\"\".*?\"\"\"", "", code, flags=re.S)
    literals = set(re.findall(r"[\"']([A-Za-z0-9_\-]+)[\"']", code))
    assert not (literals & names), literals & names
