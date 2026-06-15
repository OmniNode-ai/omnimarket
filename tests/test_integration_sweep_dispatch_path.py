# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# onex-allow-file OMN-13145 reason="test fixtures use .201 lab endpoints as probe inputs; not runtime defaults or shipping connection strings"
"""Real-dispatch-path tests for node_integration_sweep_orchestrator (OMN-13145).

These drive the node through the canonical RuntimeLocal event-driven runtime
(contract -> handler_routing -> typed event_model -> handler), NOT a direct
handler call. Both OMN-13145 bugs hid precisely because the only coverage was
direct handler invocation:

  (a) the bogus ``surface_probes`` handler_routing entry crashed handler
      resolution before any command ran;
  (b) the missing ``event_model`` wiring meant the runtime forwarded a raw dict
      to ``handle()`` (or, after adding the field, an envelope correlation_id
      that the request model rejected with ``extra_forbidden``).

A direct ``HandlerIntegrationSweepOrchestrator().handle(...)`` call exercises
neither path. These tests assert the node LOADS and COMPLETES through the
runtime, which is the acceptance bar.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult

from tests.runtime_local_compat import RuntimeLocal

CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_integration_sweep_orchestrator/contract.yaml"
)

_PROBES_SUBPROCESS = (
    "omnimarket.nodes.node_integration_sweep_orchestrator."
    "handlers.surface_probes.subprocess.run"
)


def _write_input(tmp_path: Path, payload: dict[str, object]) -> Path:
    input_path = tmp_path / "sweep_input.json"
    input_path.write_text(json.dumps(payload), encoding="utf-8")
    return input_path


@pytest.mark.unit
def test_dispatch_path_loads_and_completes(tmp_path: Path) -> None:
    """The node resolves its handler and completes via the runtime (covers bug a).

    No bogus handler entry means handler resolution succeeds; the event_model
    wiring means the typed request validates against the envelope payload.
    Surface probes are off so no network is touched.
    """
    artifact_root = tmp_path / "cc"
    (artifact_root / "contracts").mkdir(parents=True)
    input_path = _write_input(
        tmp_path,
        {
            "scope": "explicit",
            "tickets": ["OMN-13145"],
            "artifact_root": str(artifact_root),
            "artifact_date": "2026-06-14",
            "run_surface_probes": False,
        },
    )

    runtime = RuntimeLocal(
        workflow_path=CONTRACT_PATH,
        state_root=tmp_path / "state",
        input_path=input_path,
        timeout=30,
    )
    result = runtime.run()

    assert result == EnumWorkflowResult.COMPLETED, (
        f"dispatch path did not complete: {result}"
    )
    assert runtime.exit_code == 0
    state = json.loads((tmp_path / "state" / "workflow_result.json").read_text())
    assert state["result"] == "completed"
    artifact = yaml.safe_load(
        (artifact_root / "drift" / "integration" / "2026-06-14.yaml").read_text()
    )
    assert artifact["artifact_type"] == "ModelIntegrationRecord"
    assert artifact["tickets"] == ["OMN-13145"]


@pytest.mark.unit
def test_dispatch_path_input_fields_reach_handler(tmp_path: Path) -> None:
    """The caller's --input fields are plumbed through the runtime (covers bug b).

    Regression guard for the top-level ``input_model`` fix: without it the
    runtime published only ``{"correlation_id": ...}`` and the handler ignored
    every caller field, silently falling back to ONEX_CC_REPO_PATH. Here the
    artifact lands at the caller-supplied artifact_root/date, proving the
    payload survived the dispatch boundary.
    """
    artifact_root = tmp_path / "explicit_root"
    (artifact_root / "contracts").mkdir(parents=True)
    input_path = _write_input(
        tmp_path,
        {
            "artifact_root": str(artifact_root),
            "artifact_date": "2026-06-13",
            "run_surface_probes": False,
        },
    )

    runtime = RuntimeLocal(
        workflow_path=CONTRACT_PATH,
        state_root=tmp_path / "state",
        input_path=input_path,
        timeout=30,
    )
    # No ONEX_CC_REPO_PATH in env — proves the field came from --input, not env.
    with patch.dict("os.environ", {}, clear=False) as _env:
        import os

        os.environ.pop("ONEX_CC_REPO_PATH", None)
        result = runtime.run()

    assert result == EnumWorkflowResult.COMPLETED
    expected = artifact_root / "drift" / "integration" / "2026-06-13.yaml"
    assert expected.is_file(), (
        f"artifact not written to caller-supplied root: {expected}"
    )


@pytest.mark.unit
def test_dispatch_path_runs_infra_probes_when_configured(tmp_path: Path) -> None:
    """KAFKA/DB/PROJECTION/GOLDEN_CHAIN probes flow through dispatch into the artifact.

    subprocess.run is mocked so the probes never reach the network; the test
    asserts the configured surfaces appear in the written ModelIntegrationRecord.
    """
    artifact_root = tmp_path / "cc"
    (artifact_root / "contracts").mkdir(parents=True)
    input_path = _write_input(
        tmp_path,
        {
            "artifact_root": str(artifact_root),
            "artifact_date": "2026-06-14",
            "run_surface_probes": True,
            "kafka_topics": ["onex.cmd.omnimarket.integration-sweep.v1"],
            "kafka_consumer_groups": ["omnimarket-integration-sweep"],
            "db_database": "omnidash_analytics",
            "db_tables": ["llm_routing_decisions"],
            "projection_topics": ["onex.evt.omnimarket.integration-sweep-completed.v1"],
            "golden_chains": [
                {
                    "chain_name": "routing",
                    "command_topic": "onex.cmd.omnimarket.integration-sweep.v1",
                    "consumer_group": "omnimarket-integration-sweep",
                    "tail_database": "omnidash_analytics",
                    "tail_table": "llm_routing_decisions",
                }
            ],
        },
    )

    # Make every shelled probe succeed deterministically.
    def _fake_run(argv: list[str], **_kwargs: object) -> MagicMock:
        joined = " ".join(argv)
        out = MagicMock()
        out.returncode = 0
        out.stderr = ""
        if "rpk topic list" in joined or "rpk\ttopic" in joined:
            out.stdout = "NAME\nonex.cmd.omnimarket.integration-sweep.v1\n"
        elif "rpk group list" in joined or "group" in joined:
            out.stdout = "BROKER GROUP\n0 omnimarket-integration-sweep\n"
        elif "to_regclass" in joined:
            out.stdout = "public.llm_routing_decisions\n"
        elif "count(*)" in joined:
            out.stdout = "5\n"
        elif "curl" in argv[0] if argv else False:
            out.stdout = "200"
        else:
            out.stdout = "200" if argv and argv[0] == "curl" else ""
        return out

    with patch(_PROBES_SUBPROCESS, side_effect=_fake_run):
        runtime = RuntimeLocal(
            workflow_path=CONTRACT_PATH,
            state_root=tmp_path / "state",
            input_path=input_path,
            timeout=30,
        )
        result = runtime.run()

    assert result == EnumWorkflowResult.COMPLETED
    artifact = yaml.safe_load(
        (artifact_root / "drift" / "integration" / "2026-06-14.yaml").read_text()
    )
    surfaces = {s["surface"] for s in artifact["surfaces"]}
    assert {"RUNTIME_HEALTH", "CONTAINER_HEALTH", "GITHUB_CI"} <= surfaces
    assert {"KAFKA", "DB", "PROJECTION", "GOLDEN_CHAIN"} <= surfaces
