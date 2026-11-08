# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of node_job_log_filler_effect over the bus (OMN-23032).

The node's contract is wired into the runtime's dispatch engine on the bus. A fill command
published on its command topic is answered on its terminal topic: the golden chain fills
the cache from a planted watcher state and reports ``completed``; the error chain plants a
404 for every log and reports ``failed`` with the reason on each run. The in-memory profile
runs everywhere. The Redpanda profile runs the same chains against the plaintext lab broker
named in ``JOB_LOG_FILLER_REDPANDA_BOOTSTRAP`` (never the dev lane's):

    JOB_LOG_FILLER_REDPANDA_BOOTSTRAP=<host:port> uv run pytest -m kafka tests/nodes/node_job_log_filler_effect -q
"""

from __future__ import annotations

import os
import uuid
from importlib import resources
from pathlib import Path

import pytest
import yaml
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import (
    subscribe_wired_contract_topics,
    wire_from_manifest,
)
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

from omnimarket.delegated_test_loop.lane_bus import open_lab_run_bus
from omnimarket.handlers.bus_binding.select import positional_opener
from omnimarket.handlers.scm.handler_scm_dispatch import ScmBusCaller
from omnimarket.nodes.node_job_log_filler_effect.handlers import (
    handler_job_log_filler as hjf,
)
from omnimarket.nodes.node_job_log_filler_effect.handlers.adapter_job_log_filler_cli import (
    load_topics,
    result_from_terminal,
)
from omnimarket.nodes.node_job_log_filler_effect.handlers.adapter_job_log_readers import (
    InMemoryJobLogReader,
)
from omnimarket.nodes.node_job_log_filler_effect.models.model_job_log_filler import (
    EnumJobLogFillOutcome,
    EnumJobLogFillStatus,
    ModelJobLogFillRequest,
    ModelJobLogFillResult,
)
from tests.nodes.node_job_log_filler_effect.planted import (
    RED_JOB,
    SUMMARY_JOB,
    transport_for,
    watcher_state,
)

pytestmark = pytest.mark.unit
BOOTSTRAP_ENV = "JOB_LOG_FILLER_REDPANDA_BOOTSTRAP"
NODE = "node_job_log_filler_effect"


def test_the_contract_names_the_handler_topics_and_overlay() -> None:
    node = resources.files(f"omnimarket.nodes.{NODE}")
    contract = yaml.safe_load(node.joinpath("contract.yaml").read_text())
    topics = load_topics()
    assert topics.command == "onex.cmd.omnimarket.job-log-filler-requested.v1"
    assert topics.success == "onex.evt.omnimarket.job-log-filler-completed.v1"
    assert contract["handler"]["class"] == "HandlerJobLogFiller"
    example = yaml.safe_load(node.joinpath("overlay.example.yaml").read_text())
    parsed = hjf.ModelJobLogFillerOverlay.model_validate(example)
    assert set(parsed.profiles) == {"inmemory", "redpanda"}
    assert parsed.profile(None).kind == "inmemory"
    assert parsed.profile("redpanda").kind == "kafka"


def test_an_overlay_missing_a_key_refuses_and_names_it() -> None:
    with pytest.raises(Exception, match="max_reads_per_pass"):
        hjf.ModelJobLogFillerOverlay.model_validate(
            {"github_owner": "o", "watcher_state_env": "E"}
        )


def _plant_deployment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transport: object
) -> Path:
    """The deployed handler reads everything from its overlay and environment; plant those."""
    import json

    root = tmp_path / "job-logs"
    (tmp_path / "github_job_logs").mkdir()
    (tmp_path / "github_job_logs" / "overlay.yaml").write_text(f"root: {root}\n")
    state = tmp_path / "pr-watcher-state.json"
    state.write_text(json.dumps(watcher_state()))
    overlay_file = tmp_path / "filler-overlay.yaml"
    overlay_file.write_text(
        yaml.safe_dump(
            {
                "github_owner": "OmniNode-ai",
                "watcher_state_env": "PLANTED_PR_WATCHER_STATE",
                "pr_classes": ["red"],
                "always_job_names": ["CI Summary"],
                "max_reads_per_pass": 10,
                "reader_concurrency": 1,
                "reply_tail_bytes": 4096,
                "reply_timeout_seconds": 5,
                "default_profile": "inmemory",
                "profiles": {"inmemory": {"kind": "inmemory"}},
            }
        )
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    monkeypatch.setenv(hjf.OVERLAY_FILE_ENV, str(overlay_file))
    monkeypatch.setenv("PLANTED_PR_WATCHER_STATE", str(state))
    # The one seam: a live download uses the recorded GitHub instead of the network.
    monkeypatch.setattr(
        InMemoryJobLogReader, "_real_transport", staticmethod(lambda: transport)
    )
    return root


async def _chain(
    bus_args: tuple[str, str | None, str | None, None],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    plant_404: bool,
) -> ModelJobLogFillResult:
    bodies = {SUMMARY_JOB: (404, b"{}"), RED_JOB: (404, b"{}")} if plant_404 else {}
    transport = transport_for(SUMMARY_JOB, RED_JOB, bodies=bodies)
    _plant_deployment(tmp_path, monkeypatch, transport)
    node = resources.files(f"omnimarket.nodes.{NODE}")
    with resources.as_file(node.joinpath("contract.yaml")) as path:
        manifest = discover_contracts_from_paths([path])
        assert not manifest.errors, manifest.errors
        assert len(manifest.contracts) == 1
        async with positional_opener(open_lab_run_bus)(*bus_args) as bus:
            engine = MessageDispatchEngine()
            report = await wire_from_manifest(
                manifest,
                engine,
                event_bus=bus,
                environment="local",
                subscribe_immediately=False,
            )
            engine.freeze()
            attached = await subscribe_wired_contract_topics(
                manifest, report, engine, bus, environment="local"
            )
            assert attached.get(NODE), "no consumer attached for the filler"
            request = ModelJobLogFillRequest(correlation_id=uuid.uuid4())
            terminal = await ScmBusCaller(bus).call_topics(
                topics=load_topics(),
                request=request,
                correlation_id=request.correlation_id,
                timeout_s=20,
            )
    assert terminal.receipt.terminal_envelope_id is not None
    return result_from_terminal(terminal.payload, request, "inmemory")


@pytest.mark.asyncio
async def test_golden_chain_on_the_in_memory_bus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = await _chain(
        ("inmemory", None, None, None), tmp_path, monkeypatch, plant_404=False
    )
    assert result.status is EnumJobLogFillStatus.COMPLETED, result
    assert (result.considered, result.filled, result.failed) == (2, 2, 0)
    assert {i.outcome for i in result.items} == {EnumJobLogFillOutcome.FILLED}
    assert result.cache_path == str(tmp_path / "job-logs")


@pytest.mark.asyncio
async def test_error_chain_on_the_in_memory_bus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = await _chain(
        ("inmemory", None, None, None), tmp_path, monkeypatch, plant_404=True
    )
    assert result.status is EnumJobLogFillStatus.FAILED, result
    assert (result.filled, result.failed) == (0, 2)
    assert all("404" in (i.error or "") for i in result.items)


@pytest.mark.asyncio
async def test_error_chain_a_pass_that_cannot_start_is_a_failed_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plant_deployment(tmp_path, monkeypatch, transport_for())
    monkeypatch.delenv(
        "PLANTED_PR_WATCHER_STATE"
    )  # the overlay names it; the host lacks it
    node = resources.files(f"omnimarket.nodes.{NODE}")
    with resources.as_file(node.joinpath("contract.yaml")) as path:
        manifest = discover_contracts_from_paths([path])
        async with positional_opener(open_lab_run_bus)(
            "inmemory", None, None, None
        ) as bus:
            engine = MessageDispatchEngine()
            report = await wire_from_manifest(
                manifest,
                engine,
                event_bus=bus,
                environment="local",
                subscribe_immediately=False,
            )
            engine.freeze()
            await subscribe_wired_contract_topics(
                manifest, report, engine, bus, environment="local"
            )
            request = ModelJobLogFillRequest(correlation_id=uuid.uuid4())
            terminal = await ScmBusCaller(bus).call_topics(
                topics=load_topics(),
                request=request,
                correlation_id=request.correlation_id,
                timeout_s=20,
            )
    result = result_from_terminal(terminal.payload, request, "inmemory")
    assert result.status is EnumJobLogFillStatus.FAILED
    assert "PLANTED_PR_WATCHER_STATE" in (result.error or "")


@pytest.mark.kafka
@pytest.mark.integration
@pytest.mark.asyncio
async def test_golden_and_error_chains_on_a_redpanda_bus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bootstrap = os.environ.get(BOOTSTRAP_ENV)
    if not bootstrap:
        pytest.skip(f"Kafka not reachable: {BOOTSTRAP_ENV} names no lab broker")
    golden = await _chain(
        ("kafka", None, bootstrap, None), tmp_path / "g", monkeypatch, plant_404=False
    )
    error = await _chain(
        ("kafka", None, bootstrap, None), tmp_path / "e", monkeypatch, plant_404=True
    )
    assert golden.status is EnumJobLogFillStatus.COMPLETED
    assert error.status is EnumJobLogFillStatus.FAILED
