# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Full declared-state COMPUTE coverage for node_contract_drift_compute, driven
over the canonical in-memory bus.

OMN-13674 (cluster wave-sweep-audit-compute). The COMPUTE handler
``NodeContractDriftCompute`` is dispatched through ``LocalRuntimeBusAdapter`` over
``EventBusInmemory`` (via the ``integration_event_bus`` fixture): a
``ModelContractDriftComputeRequest`` lands on the contract-declared command topic
``onex.cmd.omnimarket.contract-drift-compute-start.v1`` and the runtime auto-emits
the ``ModelContractDriftComputeResult`` onto the contract-declared terminal topic
``onex.evt.omnimarket.contract-drift-compute-completed.v1``.

The node classifies contract-vs-handler topic drift. It resolves the scan root
from ``OMNI_HOME``; each test points ``OMNI_HOME`` at a pytest ``tmp_path`` holding
a synthetic single-node repo, so the scan is deterministic and reads no real
repository. No monkeypatching of IO primitives -- only the documented
``OMNI_HOME`` env indirection is redirected.

COMPUTE DoD:
  * every declared ``overall_status`` verdict reached -- clean / breaking /
    drifted;
  * every mode/flag branch exercised: sensitivity STANDARD / STRICT / LAX, the
    ``severity_threshold`` filter (BREAKING hides NON_BREAKING drift), the
    ``repos`` allow-list, ``dry_run`` echo, and ``check_boundaries``;
  * a negative control: a synthetic node whose handler hard-codes an
    undeclared ``onex.*`` topic literal MUST surface as a BREAKING drift finding.

NOTE (honest coverage gap): ``check_boundaries`` is accepted and echoed through,
but ``boundary_findings`` is currently always ``[]`` in the handler
(Kafka-boundary parsing is unimplemented). The flag is exercised for input
coverage; ``boundary_findings == []`` is asserted rather than pretending a
boundary verdict exists.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_contract_drift_compute.handlers.handler_contract_drift_compute import (
    ModelContractDriftComputeRequest,
    ModelContractDriftComputeResult,
    NodeContractDriftCompute,
)
from tests.runtime_local_compat import LocalRuntimeBusAdapter

# Contract-declared topics (node_contract_drift_compute/contract.yaml).
_START_TOPIC = "onex.cmd.omnimarket.contract-drift-compute-start.v1"
_COMPLETED_TOPIC = "onex.evt.omnimarket.contract-drift-compute-completed.v1"


def _write_node(
    omni_home: Path,
    *,
    repo: str = "synthrepo",
    node: str = "node_synth",
    subscribe: list[str],
    publish: list[str],
    terminal: str,
    handler_topics: list[str],
) -> None:
    """Materialize a synthetic single-node repo under ``omni_home``.

    ``handler_topics`` are embedded as string-literal constants in the handler
    source; the classifier compares them against the contract-declared topics.
    """
    node_dir = omni_home / repo / "src" / "omnimarket" / "nodes" / node
    (node_dir / "handlers").mkdir(parents=True, exist_ok=True)

    subscribe_yaml = "\n".join(f"    - {t}" for t in subscribe)
    publish_yaml = "\n".join(f"    - {t}" for t in publish)
    contract = (
        f"name: {node}\n"
        f"node_type: compute\n"
        f"terminal_event: {terminal}\n"
        f"event_bus:\n"
        f"  subscribe_topics:\n{subscribe_yaml}\n"
        f"  publish_topics:\n{publish_yaml}\n"
    )
    (node_dir / "contract.yaml").write_text(contract, encoding="utf-8")

    literals = "\n".join(f'    "{t}",' for t in handler_topics)
    handler_src = (
        '"""Synthetic handler embedding topic literals for drift classification."""\n'
        "\n"
        "TOPICS = [\n"
        f"{literals}\n"
        "]\n"
    )
    (node_dir / "handlers" / "handler_synth.py").write_text(
        handler_src, encoding="utf-8"
    )


async def _run_over_bus(
    bus: Any, request: ModelContractDriftComputeRequest
) -> ModelContractDriftComputeResult:
    """Publish a drift-compute request onto the declared command topic and return
    the terminal ``ModelContractDriftComputeResult`` off the declared terminal
    topic."""
    adapter = LocalRuntimeBusAdapter(
        handler=NodeContractDriftCompute(),
        handler_name="contract-drift-compute",
        input_model_cls=ModelContractDriftComputeRequest,
        output_topic=_COMPLETED_TOPIC,
        bus=bus,
    )
    await bus.subscribe(
        _START_TOPIC,
        on_message=adapter.on_message,
        group_id="omnimarket-contract-drift-test",
    )
    await bus.publish(
        _START_TOPIC,
        key=None,
        value=request.model_dump_json().encode("utf-8"),
    )
    history = await bus.get_event_history(topic=_COMPLETED_TOPIC)
    assert len(history) == 1, f"expected 1 terminal event on {_COMPLETED_TOPIC}"
    return ModelContractDriftComputeResult.model_validate(json.loads(history[-1].value))


@pytest.mark.integration
async def test_contract_drift_clean_over_bus(
    integration_event_bus: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A node whose handler literals are all contract-declared reports clean."""
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    _write_node(
        tmp_path,
        subscribe=["onex.cmd.omnimarket.synth-start.v1"],
        publish=["onex.evt.omnimarket.synth-done.v1"],
        terminal="onex.evt.omnimarket.synth-done.v1",
        handler_topics=["onex.evt.omnimarket.synth-done.v1"],
    )
    bus = integration_event_bus
    await bus.start()
    try:
        result = await _run_over_bus(bus, ModelContractDriftComputeRequest())
        assert result.overall_status == "clean"
        assert result.drifted_contracts == []
        assert result.violations == []
        assert result.staleness_scores == {"synthrepo": 0.0}
        assert result.total_contracts_checked == 1
        assert result.boundary_findings == []
    finally:
        await bus.close()


@pytest.mark.integration
async def test_contract_drift_breaking_negative_control_over_bus(
    integration_event_bus: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative control: a handler hard-coding an undeclared topic MUST produce a
    BREAKING drift finding and a breaking overall status."""
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    _write_node(
        tmp_path,
        subscribe=["onex.cmd.omnimarket.synth-start.v1"],
        publish=["onex.evt.omnimarket.synth-done.v1"],
        terminal="onex.evt.omnimarket.synth-done.v1",
        handler_topics=[
            "onex.evt.omnimarket.synth-done.v1",
            "onex.evt.omnimarket.undeclared-rogue.v1",  # not in the contract
        ],
    )
    bus = integration_event_bus
    await bus.start()
    try:
        result = await _run_over_bus(bus, ModelContractDriftComputeRequest())
        assert result.overall_status == "breaking"
        assert len(result.drifted_contracts) == 1
        finding = result.drifted_contracts[0]
        assert finding.severity == "BREAKING"
        assert finding.repo == "synthrepo"
        assert any(
            "undeclared-rogue" in change.path for change in finding.field_changes
        )
        assert result.staleness_scores == {"synthrepo": 1.0}
        assert result.violations
        assert "BREAKING" in result.violations[0]
    finally:
        await bus.close()


@pytest.mark.integration
async def test_contract_drift_strict_surfaces_non_breaking_drift_over_bus(
    integration_event_bus: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """STRICT sensitivity + a NON_BREAKING threshold surfaces contract topics that
    have no handler literal, yielding a ``drifted`` overall status."""
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    _write_node(
        tmp_path,
        subscribe=["onex.cmd.omnimarket.synth-start.v1"],
        publish=[
            "onex.evt.omnimarket.synth-done.v1",
            "onex.evt.omnimarket.synth-extra.v1",  # declared, absent from handler
        ],
        terminal="onex.evt.omnimarket.synth-done.v1",
        handler_topics=["onex.evt.omnimarket.synth-done.v1"],
    )
    bus = integration_event_bus
    await bus.start()
    try:
        result = await _run_over_bus(
            bus,
            ModelContractDriftComputeRequest(
                sensitivity="STRICT", severity_threshold="NON_BREAKING"
            ),
        )
        assert result.overall_status == "drifted"
        assert len(result.drifted_contracts) == 1
        finding = result.drifted_contracts[0]
        assert finding.severity == "NON_BREAKING"
        assert all(
            change.severity == "NON_BREAKING" for change in finding.field_changes
        )
        assert any("synth-extra" in change.path for change in finding.field_changes)
    finally:
        await bus.close()


@pytest.mark.integration
async def test_contract_drift_severity_threshold_hides_non_breaking_over_bus(
    integration_event_bus: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default BREAKING threshold filters out NON_BREAKING-only drift, so the
    same STRICT scan reports clean."""
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    _write_node(
        tmp_path,
        subscribe=["onex.cmd.omnimarket.synth-start.v1"],
        publish=[
            "onex.evt.omnimarket.synth-done.v1",
            "onex.evt.omnimarket.synth-extra.v1",
        ],
        terminal="onex.evt.omnimarket.synth-done.v1",
        handler_topics=["onex.evt.omnimarket.synth-done.v1"],
    )
    bus = integration_event_bus
    await bus.start()
    try:
        result = await _run_over_bus(
            bus,
            ModelContractDriftComputeRequest(
                sensitivity="STRICT", severity_threshold="BREAKING"
            ),
        )
        assert result.overall_status == "clean"
        assert result.drifted_contracts == []
    finally:
        await bus.close()


@pytest.mark.integration
async def test_contract_drift_lax_matches_standard_for_handler_only_over_bus(
    integration_event_bus: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LAX sensitivity still classifies an undeclared handler literal as BREAKING
    (LAX only suppresses the STRICT contract-only pass)."""
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    _write_node(
        tmp_path,
        subscribe=["onex.cmd.omnimarket.synth-start.v1"],
        publish=["onex.evt.omnimarket.synth-done.v1"],
        terminal="onex.evt.omnimarket.synth-done.v1",
        handler_topics=[
            "onex.evt.omnimarket.synth-done.v1",
            "onex.evt.omnimarket.undeclared-rogue.v1",
        ],
    )
    bus = integration_event_bus
    await bus.start()
    try:
        result = await _run_over_bus(
            bus, ModelContractDriftComputeRequest(sensitivity="LAX")
        )
        assert result.overall_status == "breaking"
        assert result.drifted_contracts[0].severity == "BREAKING"
    finally:
        await bus.close()


@pytest.mark.integration
async def test_contract_drift_repos_filter_and_dry_run_over_bus(
    integration_event_bus: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``repos`` allow-list scopes the scan and ``dry_run`` is echoed through.

    Two synthetic repos exist; only the named one is scanned. ``check_boundaries``
    is exercised for input coverage (boundary_findings stays empty -- see the
    module docstring note)."""
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    _write_node(
        tmp_path,
        repo="repo_scanned",
        subscribe=["onex.cmd.omnimarket.synth-start.v1"],
        publish=["onex.evt.omnimarket.synth-done.v1"],
        terminal="onex.evt.omnimarket.synth-done.v1",
        handler_topics=["onex.evt.omnimarket.rogue-in-scanned.v1"],
    )
    _write_node(
        tmp_path,
        repo="repo_ignored",
        subscribe=["onex.cmd.omnimarket.synth-start.v1"],
        publish=["onex.evt.omnimarket.synth-done.v1"],
        terminal="onex.evt.omnimarket.synth-done.v1",
        handler_topics=["onex.evt.omnimarket.rogue-in-ignored.v1"],
    )
    bus = integration_event_bus
    await bus.start()
    try:
        result = await _run_over_bus(
            bus,
            ModelContractDriftComputeRequest(
                repos=["repo_scanned"],
                dry_run=True,
                check_boundaries=True,
            ),
        )
        assert result.repos_scanned == 1
        assert set(result.staleness_scores) == {"repo_scanned"}
        assert result.dry_run is True
        assert result.overall_status == "breaking"
        assert all("rogue-in-ignored" not in v for v in result.violations)
        assert result.boundary_findings == []
    finally:
        await bus.close()


@pytest.mark.integration
async def test_contract_drift_pure_handler_matches_bus_result(
    integration_event_bus: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The in-process pure return equals the bus-transited terminal payload."""
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    _write_node(
        tmp_path,
        subscribe=["onex.cmd.omnimarket.synth-start.v1"],
        publish=["onex.evt.omnimarket.synth-done.v1"],
        terminal="onex.evt.omnimarket.synth-done.v1",
        handler_topics=["onex.evt.omnimarket.undeclared-rogue.v1"],
    )
    request = ModelContractDriftComputeRequest()
    direct = NodeContractDriftCompute().handle(request)
    assert direct.overall_status == "breaking"

    bus = integration_event_bus
    await bus.start()
    try:
        transited = await _run_over_bus(bus, request)
        assert transited.model_dump() == direct.model_dump()
    finally:
        await bus.close()


# ---------------------------------------------------------------------------
# Contract-declared publish-topic coverage (declared-but-unemitted surfaces).
#
# The contract declares three publish topics, but the handler only produces the
# terminal ``ModelContractDriftComputeResult`` (auto-emitted onto
# ``...contract-drift-compute-completed.v1`` by the runtime adapter). The
# per-finding ``...contract-drift-finding.v1`` and aggregate
# ``...sweep-result.v1`` topics are DECLARED but the current handler never emits
# them -- there is no publish path in ``NodeContractDriftCompute.handle``. These
# pins record the declared wire contract (and keep the state-coverage gate honest
# about the declared surface) without pretending a runtime emission exists.
# ---------------------------------------------------------------------------

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_contract_drift_compute"
    / "contract.yaml"
)

# Declared-but-unemitted publish topics (no producer in the handler today).
_DECLARED_UNEMITTED_TOPICS = [
    "onex.evt.omnimarket.contract-drift-finding.v1",
    "onex.evt.omnimarket.sweep-result.v1",
]


def test_contract_declares_all_publish_topics() -> None:
    """Every contract-declared publish topic keeps its literal wire string.

    The terminal topic is exercised over the bus above; the two remaining topics
    are declared-but-unemitted and pinned here so a silent rename/removal fails
    a test rather than only surfacing at a live boundary.
    """
    import yaml

    contract = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
    publish_topics = contract["event_bus"]["publish_topics"]
    assert _COMPLETED_TOPIC in publish_topics
    for topic in _DECLARED_UNEMITTED_TOPICS:
        assert topic in publish_topics
