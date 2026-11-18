# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of node_ci_checkout_effect over the bus."""

from __future__ import annotations

import os
import subprocess
import uuid
from importlib import resources
from pathlib import Path

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import (
    subscribe_wired_contract_topics,
    wire_from_manifest,
)
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

from omnimarket.delegated_test_loop.lane_bus import open_lab_run_bus
from omnimarket.handlers.bus_binding.select import positional_opener
from omnimarket.handlers.scm.handler_scm_dispatch import ScmBusCaller
from omnimarket.models.scm import ModelScmTerminal
from omnimarket.nodes.node_ci_checkout_effect.handlers import handler_ci_checkout as hch
from omnimarket.nodes.node_ci_checkout_effect.handlers.adapter_ci_checkout_cli import (
    load_topics,
    result_from_terminal,
)
from omnimarket.nodes.node_ci_checkout_effect.models.model_ci_checkout import (
    EnumCiCheckoutStatus,
    ModelCiCheckoutRequest,
    ModelCiCheckoutResult,
)
from tests.nodes.node_ci_checkout_effect.planted import HOST, deployment_document

pytestmark = pytest.mark.unit
BOOTSTRAP_ENV = "CI_CHECKOUT_REDPANDA_BOOTSTRAP"
NODE = "node_ci_checkout_effect"


def test_the_contract_names_the_handler_topics_and_overlay() -> None:
    node = resources.files(f"omnimarket.nodes.{NODE}")
    contract = yaml.safe_load(node.joinpath("contract.yaml").read_text())
    topics = load_topics()
    assert topics.command == "onex.cmd.omnimarket.ci-checkout-requested.v1"
    assert topics.success == "onex.evt.omnimarket.ci-checkout-completed.v1"
    assert topics.failure == "onex.evt.omnimarket.ci-checkout-failed.v1"
    assert contract["handler"]["class"] == "HandlerCiCheckout"
    example = yaml.safe_load(node.joinpath("overlay.example.yaml").read_text())
    assert "hosts" in example
    assert "sources" in example
    assert "unit" in example


def _git(*argv: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *argv],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env=scrub_git_location_env(os.environ),
    ).stdout.strip()


def _plant_deployment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, with_overlay: bool = True
) -> str:
    """A bare mirror holding one real commit, and the overlay naming it. Returns that sha."""
    work = tmp_path / "work"
    work.mkdir(parents=True)
    _git("init", "-q", cwd=work)
    (work / "hello.txt").write_text("hello\n")
    _git("add", "hello.txt", cwd=work)
    _git(
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.invalid",
        "commit",
        "-q",
        "-m",
        "one",
        cwd=work,
    )
    sha = _git("rev-parse", "HEAD", cwd=work)
    mirrors = tmp_path / "mirrors"
    mirrors.mkdir()
    _git(
        "clone",
        "-q",
        "--bare",
        str(work),
        str(mirrors / "omnimarket.git"),
        cwd=tmp_path,
    )
    if with_overlay:
        overlay_file = tmp_path / "ci-checkout-overlay.yaml"
        overlay_file.write_text(yaml.safe_dump(deployment_document(mirrors)))
        monkeypatch.setenv(hch.OVERLAY_FILE_ENV, str(overlay_file))
        monkeypatch.setenv(hch.HOST_NAME_ENV, HOST)
    else:
        monkeypatch.delenv(hch.OVERLAY_FILE_ENV, raising=False)
        monkeypatch.setenv(hch.HOST_NAME_ENV, HOST)
        monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "no-overlays"))
    return sha


async def _chain(
    bus_args: tuple[str, str | None, str | None, None],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    sha_held: bool,
    with_overlay: bool = True,
) -> tuple[ModelCiCheckoutResult, ModelScmTerminal]:
    held_sha = _plant_deployment(tmp_path, monkeypatch, with_overlay=with_overlay)
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
            assert attached.get(NODE), "no consumer attached for ci checkout"
            request = ModelCiCheckoutRequest(
                correlation_id=uuid.uuid4(),
                repo="OmniNode-ai/omnimarket",
                sha=held_sha if sha_held else "b" * 40,
                ref=None,
                requested_by_lane="test-lane",
                host=HOST,
            )
            terminal = await ScmBusCaller(bus).call_topics(
                topics=load_topics(),
                request=request,
                correlation_id=request.correlation_id,
                timeout_s=20,
            )
    assert terminal.receipt.terminal_envelope_id is not None
    return result_from_terminal(terminal.payload, request), terminal


@pytest.mark.asyncio
async def test_golden_chain_on_the_in_memory_bus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, terminal = await _chain(
        ("inmemory", None, None, None), tmp_path, monkeypatch, sha_held=True
    )
    assert result.status is EnumCiCheckoutStatus.SERVED, result
    assert result.upstream_fetches == 0
    assert result.serve_url is not None
    assert result.serve_url.startswith("file://")
    assert not terminal.failed
    assert terminal.receipt.terminal_topic == load_topics().success


@pytest.mark.asyncio
async def test_error_chain_a_sha_no_source_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, _terminal = await _chain(
        ("inmemory", None, None, None), tmp_path, monkeypatch, sha_held=False
    )
    assert result.status is EnumCiCheckoutStatus.NOT_FOUND, result
    assert result.serve_url is None
    # Failing control: the golden chain's assertion on the same node reads SERVED, so this
    # answer differs from it only by the sha, which is what the node reads.
    assert result.status is not EnumCiCheckoutStatus.SERVED


@pytest.mark.asyncio
async def test_error_chain_no_overlay_ends_on_the_failure_topic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, terminal = await _chain(
        ("inmemory", None, None, None),
        tmp_path,
        monkeypatch,
        sha_held=True,
        with_overlay=False,
    )
    assert terminal.failed
    assert terminal.receipt.terminal_topic == load_topics().failure
    assert result.status is EnumCiCheckoutStatus.FAILED
    assert result.detail


@pytest.mark.kafka
@pytest.mark.integration
@pytest.mark.asyncio
async def test_golden_and_error_chains_on_a_redpanda_bus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bootstrap = os.environ.get(BOOTSTRAP_ENV)
    if not bootstrap:
        pytest.skip(f"Kafka not reachable: {BOOTSTRAP_ENV} names no lab broker")
    golden, _ = await _chain(
        ("kafka", None, bootstrap, None), tmp_path / "g", monkeypatch, sha_held=True
    )
    error, _ = await _chain(
        ("kafka", None, bootstrap, None), tmp_path / "e", monkeypatch, sha_held=False
    )
    assert golden.status is EnumCiCheckoutStatus.SERVED
    assert error.status is EnumCiCheckoutStatus.NOT_FOUND
