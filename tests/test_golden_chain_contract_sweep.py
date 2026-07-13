# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain tests for node_contract_sweep.

Covers the command -> handler -> completion event path over
EventBusInmemory, plus the OMN-14542 (class fix, parent OMN-14531)
scope-invariant contract: a required, harness-collected `repos` census, a
`scanned_count > 0` fail-closed assertion, and an explicit ERROR verdict
(never a silent PASS) over an unresolvable or empty scope.

All checks operate on a real filesystem under tmp_path — no DB, no network.
"""

from __future__ import annotations

import json
import os
import textwrap
from pathlib import Path

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_contract_sweep.handlers.handler_contract_sweep import (
    ContractSweepRequest,
    EnumSweepStatus,
    NodeContractSweep,
)

CMD_TOPIC = "onex.cmd.omnimarket.contract-sweep-start.v1"
EVT_TOPIC = "onex.evt.omnimarket.contract-sweep-completed.v1"

_VALID_CONTRACT = textwrap.dedent("""\
    name: node_test_valid
    node_type: COMPUTE_GENERIC
    contract_version:
      major: 1
      minor: 0
      patch: 0
    node_version: "1.0.0"
    description: "A valid test node"
    event_bus:
      publish_topics:
        - "onex.evt.platform.test-event.v1"
""")


def _write_contract(base: Path, node_name: str, content: str) -> Path:
    node_dir = base / "nodes" / node_name
    node_dir.mkdir(parents=True, exist_ok=True)
    contract = node_dir / "contract.yaml"
    contract.write_text(content)
    return contract


@pytest.mark.unit
class TestContractSweepGoldenChain:
    """Golden chain: command -> handler -> completion event."""

    async def test_event_bus_wiring(
        self, event_bus: EventBusInmemory, tmp_path: Path
    ) -> None:
        """Handler can publish a completion event to EventBusInmemory,
        driven by a command carrying a harness-collected repos census."""
        omni_home = tmp_path / "omni_home"
        _write_contract(omni_home / "myrepo" / "src", "node_x", _VALID_CONTRACT)

        handler = NodeContractSweep()
        events_captured: list[dict[str, object]] = []

        async def on_command(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[union-attr]
            os.environ["OMNI_HOME"] = payload["omni_home"]
            try:
                request = ContractSweepRequest(repos=payload["repos"])
                result = handler.handle(request)
            finally:
                del os.environ["OMNI_HOME"]
            evt = {"status": result.status, "scanned_count": result.scanned_count}
            events_captured.append(evt)
            await event_bus.publish(EVT_TOPIC, key=None, value=json.dumps(evt).encode())

        await event_bus.start()
        await event_bus.subscribe(
            CMD_TOPIC, on_message=on_command, group_id="test-contract-sweep"
        )

        cmd_payload = json.dumps(
            {"omni_home": str(omni_home), "repos": ["myrepo"]}
        ).encode()
        await event_bus.publish(CMD_TOPIC, key=None, value=cmd_payload)

        assert len(events_captured) == 1
        assert events_captured[0]["status"] == "PASS"
        assert events_captured[0]["scanned_count"] == 1

        history = await event_bus.get_event_history(topic=EVT_TOPIC)
        assert len(history) == 1

        await event_bus.close()

    async def test_handler_default_scans_all_requested_repos(
        self, event_bus: EventBusInmemory, tmp_path: Path
    ) -> None:
        """Handler scans every repo named in the census and aggregates."""
        omni_home = tmp_path / "omni_home"
        _write_contract(omni_home / "repo_a" / "src", "node_x", _VALID_CONTRACT)
        _write_contract(omni_home / "repo_b" / "src", "node_y", _VALID_CONTRACT)

        os.environ["OMNI_HOME"] = str(omni_home)
        try:
            handler = NodeContractSweep()
            request = ContractSweepRequest(repos=["repo_a", "repo_b"])
            result = handler.handle(request)
        finally:
            del os.environ["OMNI_HOME"]

        assert result.status == EnumSweepStatus.PASS
        assert result.scanned_count == 2
        assert result.contracts_checked == 2

    async def test_red_unresolvable_scope_is_error_not_pass(
        self, event_bus: EventBusInmemory, tmp_path: Path
    ) -> None:
        """MANDATORY RED PROOF: a real EXISTS-but-WRONG scope (a repo name
        that resolves to zero repos on disk) must be a hard ERROR verdict,
        never a silent, empty-but-successful PASS."""
        os.environ["OMNI_HOME"] = str(tmp_path)
        try:
            handler = NodeContractSweep()
            request = ContractSweepRequest(repos=["does_not_exist_repo"])
            result = handler.handle(request)
        finally:
            del os.environ["OMNI_HOME"]

        assert result.status == EnumSweepStatus.ERROR
        assert result.scanned_count == 0
        assert result.status != EnumSweepStatus.PASS

    async def test_green_healthy_scope_reports_pass(
        self, event_bus: EventBusInmemory, tmp_path: Path
    ) -> None:
        """GREEN PROOF: a genuinely-healthy populated scope reports PASS
        with scanned_count matching the planted corpus size exactly."""
        omni_home = tmp_path / "omni_home"
        for i in range(5):
            _write_contract(omni_home / "myrepo" / "src", f"node_{i}", _VALID_CONTRACT)

        os.environ["OMNI_HOME"] = str(omni_home)
        try:
            handler = NodeContractSweep()
            request = ContractSweepRequest(repos=["myrepo"])
            result = handler.handle(request)
        finally:
            del os.environ["OMNI_HOME"]

        assert result.status == EnumSweepStatus.PASS
        assert result.scanned_count == 5
        assert result.violations == []
