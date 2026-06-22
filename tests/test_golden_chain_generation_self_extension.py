# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-12831 (D2) — self-extension loop golden chain.

Close-the-loop plan §D2: prove the full self-extension chain end to end through
the REAL wired emission + execution code (no mocks of the logic under test):

    generate → deploy-consumed → registered → invoked → output verified

The chain is exercised across the two handlers that own the live runtime path:

  1. ``HandlerGenerationConsumer._emit_deploy`` / ``_emit_registration`` — the
     exact methods the generation flow calls once a contract passes validation
     (``handle`` → ``_emit_deploy`` → ``_emit_registration``). They publish the
     ``onex.cmd.omnimarket.node-deploy.v1`` command and the
     ``onex.evt.platform.node-registration.v1`` registration event.
  2. ``HandlerGeneratedExecutor.on_deploy_event`` — the consumer wired onto
     ``onex.cmd.omnimarket.node-deploy.v1``. It sandbox-loads the generated
     handler, invokes it via importlib, and emits the terminal result on
     ``onex.evt.omnimarket.generated-node-invoked.v1``.

The deploy command captured from step 1 is fed VERBATIM into step 2, so the two
handlers are connected exactly as the runtime connects them through the bus.

Proof captured by the golden chain (the §D2 acceptance items):
  - generated output hash    — the deploy command's ``generated_contract_hash`` /
                               ``generated_handler_hash`` are the SHA-256 digests
                               of the FULL generated artifacts (no truncation),
                               and the terminal invocation result carries the
                               invoked output.
  - registration event       — emitted on ``platform.node-registration.v1`` with
                               the §A4 payload conformance (event_type,
                               service_name, ``tags``) and the SAME
                               ``correlation_id`` that flows to invocation.
  - invocation event         — emitted on ``generated-node-invoked.v1`` with the
                               future-dispatch terminal shape, status=completed,
                               and the same ``correlation_id``.

All topics are resolved from ``contract.yaml`` (never hardcoded in the handlers),
so this also guards the contract-driven wiring.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_generation_consumer.handlers.handler_generated_executor import (
    HandlerGeneratedExecutor,
)
from omnimarket.nodes.node_generation_consumer.handlers.handler_generation_consumer import (
    HandlerGenerationConsumer,
)
from omnimarket.nodes.node_generation_consumer.models.model_generation import (
    ModelGenerationBenchmark,
)

# Topics are read from the SAME contract the handlers read, asserting the chain
# is contract-driven rather than coupled to literals duplicated in the test.
_CONTRACT_PATH = (
    Path(__file__).parent.parent
    / "src/omnimarket/nodes/node_generation_consumer/contract.yaml"
)


def _contract_topics() -> dict[str, str]:
    data = yaml.safe_load(_CONTRACT_PATH.read_text())
    publish: list[str] = data["event_bus"]["publish_topics"]
    return {
        "registration": next(t for t in publish if "node-registration" in t),
        "deploy": next(t for t in publish if "node-deploy" in t),
        "invoked": next(t for t in publish if "generated-node-invoked" in t),
    }


# A complete, valid generated node: the contract.yaml declares the node name, the
# handler.py exposes the module-level ``handle`` the executor invokes. These are
# the artifacts the generation flow produces and persists in generation_events.
_GENERATED_NODE_NAME = "node_golden_echo_compute"
_GENERATED_CONTRACT_YAML = (
    f'name: {_GENERATED_NODE_NAME}\ncontract_version: "1.0.0"\nnode_type: compute\n'
)
_GENERATED_HANDLER_SOURCE = (
    "def handle(input_data):\n"
    '    """Echo the payload back with a deterministic marker."""\n'
    '    return {"echoed": input_data, "node": "golden_echo"}\n'
)
_CORRELATION_ID = "omn-12831-golden-chain-001"
_INVOKE_INPUT: dict[str, Any] = {"value": 42}


def _make_benchmark() -> ModelGenerationBenchmark:
    """The post-validation benchmark the generation flow holds before emitting."""
    return ModelGenerationBenchmark(
        correlation_id=_CORRELATION_ID,
        task_description="Generate an echo compute node",
        provider="local",
        model_id="Qwen3.6-35B-A3B",
        endpoint_class="local",
        attempt_count=1,
        contract_passed=True,
        contract_yaml=_GENERATED_CONTRACT_YAML,
        handler_source=_GENERATED_HANDLER_SOURCE,
        routing_source="contract",
        resolved_endpoint="http://local-coder:8000/v1/chat/completions",
    )


class _Capture:
    """Records every (topic, payload) the wired handlers publish."""

    def __init__(self) -> None:
        self.events: list[tuple[str, bytes]] = []

    def publish(self, topic: str, payload: bytes) -> None:
        self.events.append((topic, payload))

    def payloads_for(self, topic: str) -> list[dict[str, Any]]:
        return [json.loads(p.decode()) for (t, p) in self.events if t == topic]


@pytest.fixture
def topics() -> dict[str, str]:
    return _contract_topics()


@pytest.mark.unit
@pytest.mark.integration
class TestGenerationSelfExtensionGoldenChain:
    """generate → deploy-consumed → registered → invoked → output verified."""

    async def _run_chain(
        self, sandbox_dir: Path
    ) -> tuple[_Capture, _Capture, dict[str, Any]]:
        """Drive the full chain through the real handlers.

        Returns (generation_capture, executor_capture, terminal_result).

        OMN-13467: _emit_deploy and _emit_registration are now async so handle()
        can await broker-ack before returning. _run_chain is updated to async and
        the test methods below are marked asyncio accordingly.
        """
        gen_capture = _Capture()
        # The generation consumer reads its routing config + topics from the real
        # contract.yaml (default contract_path); only the publisher is injected.
        gen_handler = HandlerGenerationConsumer(
            event_publisher=gen_capture.publish,
        )
        benchmark = _make_benchmark()

        # Step 1+2 — generate → emit deploy command + registration event. These
        # are the REAL methods the generation flow calls post-validation.
        # OMN-13467: await async emit methods so the chain runs correctly.
        deploy_ok = await gen_handler._emit_deploy(benchmark)
        assert deploy_ok is True
        await gen_handler._emit_registration(benchmark)

        # Step 3 — the executor consumes the deploy command VERBATIM and invokes.
        exec_capture = _Capture()
        executor = HandlerGeneratedExecutor(
            sandbox_dir=sandbox_dir,
            event_publisher=exec_capture.publish,
        )
        deploy_payloads = gen_capture.payloads_for(_contract_topics()["deploy"])
        assert len(deploy_payloads) == 1, "exactly one deploy command expected"
        deploy_cmd = deploy_payloads[0]

        terminal = executor.on_deploy_event(
            json.dumps(deploy_cmd).encode(), _INVOKE_INPUT
        )
        return gen_capture, exec_capture, terminal

    # -- generate → deploy ------------------------------------------------

    @pytest.mark.asyncio
    async def test_deploy_command_carries_full_artifacts_and_hashes(
        self, tmp_path: Path, topics: dict[str, str]
    ) -> None:
        gen_capture, _exec, _terminal = await self._run_chain(tmp_path)
        deploy = gen_capture.payloads_for(topics["deploy"])[0]

        # FULL artifacts (no truncation) flow on the deploy command.
        assert deploy["contract_yaml"] == _GENERATED_CONTRACT_YAML
        assert deploy["handler_source"] == _GENERATED_HANDLER_SOURCE
        assert deploy["node_name"] == _GENERATED_NODE_NAME
        assert deploy["correlation_id"] == _CORRELATION_ID

        # generated output hash — SHA-256 of the FULL artifacts (a truncated
        # value would yield a different digest).
        assert (
            deploy["generated_contract_hash"]
            == "sha256:" + hashlib.sha256(_GENERATED_CONTRACT_YAML.encode()).hexdigest()
        )
        assert (
            deploy["generated_handler_hash"]
            == "sha256:"
            + hashlib.sha256(_GENERATED_HANDLER_SOURCE.encode()).hexdigest()
        )

    # -- registration -----------------------------------------------------

    @pytest.mark.asyncio
    async def test_registration_event_emitted_on_platform_topic(
        self, tmp_path: Path, topics: dict[str, str]
    ) -> None:
        gen_capture, _exec, _terminal = await self._run_chain(tmp_path)
        # Registration goes to onex.evt.platform.node-registration.v1 — the topic
        # MCP sync consumes — not the dead node-registered topic (§A4).
        assert topics["registration"] == "onex.evt.platform.node-registration.v1"
        regs = gen_capture.payloads_for(topics["registration"])
        assert len(regs) == 1, "exactly one registration event expected"

    @pytest.mark.asyncio
    async def test_registration_payload_conformance(
        self, tmp_path: Path, topics: dict[str, str]
    ) -> None:
        gen_capture, _exec, _terminal = await self._run_chain(tmp_path)
        reg = gen_capture.payloads_for(topics["registration"])[0]
        # §A4 payload conformance.
        assert reg["event_type"] == "registered"
        assert reg["service_name"] == _GENERATED_NODE_NAME
        assert reg["node_name"] == _GENERATED_NODE_NAME
        assert reg["tags"] == [
            "mcp-enabled",
            "node-type:orchestrator",
            f"mcp-tool:{_GENERATED_NODE_NAME}",
        ]
        # correlation_id is preserved registration → invocation (proof packet).
        assert reg["correlation_id"] == _CORRELATION_ID

    # -- deploy-consumed → invoked ---------------------------------------

    @pytest.mark.asyncio
    async def test_invocation_event_emitted_on_terminal_topic(
        self, tmp_path: Path, topics: dict[str, str]
    ) -> None:
        _gen, exec_capture, terminal = await self._run_chain(tmp_path)
        invoked = exec_capture.payloads_for(topics["invoked"])
        assert len(invoked) == 1, "exactly one invocation event expected"
        # The emitted invocation event equals the returned terminal result.
        assert invoked[0] == terminal

    @pytest.mark.asyncio
    async def test_invocation_terminal_shape_and_correlation(
        self, tmp_path: Path
    ) -> None:
        _gen, _exec, terminal = await self._run_chain(tmp_path)
        assert terminal["status"] == "completed"
        assert terminal["correlation_id"] == _CORRELATION_ID
        assert terminal["node_name"] == _GENERATED_NODE_NAME
        # Non-hot-load sandbox invoke, future-dispatch evidence shape (§B1).
        assert terminal["hot_load"] is False
        assert terminal["_runtime_backend"] == "sandbox"

    # -- output verified --------------------------------------------------

    @pytest.mark.asyncio
    async def test_invoked_output_is_verified(self, tmp_path: Path) -> None:
        _gen, _exec, terminal = await self._run_chain(tmp_path)
        # The generated handler ran against the supplied input and the chain
        # carries its real output back as the terminal result.
        assert terminal["output"] == {
            "echoed": _INVOKE_INPUT,
            "node": "golden_echo",
        }

    # -- end-to-end correlation continuity -------------------------------

    @pytest.mark.asyncio
    async def test_correlation_id_continuous_generate_to_invoke(
        self, tmp_path: Path, topics: dict[str, str]
    ) -> None:
        gen_capture, exec_capture, terminal = await self._run_chain(tmp_path)
        deploy = gen_capture.payloads_for(topics["deploy"])[0]
        reg = gen_capture.payloads_for(topics["registration"])[0]
        invoked = exec_capture.payloads_for(topics["invoked"])[0]
        # ONE correlation_id threads the whole self-extension loop.
        assert (
            deploy["correlation_id"]
            == reg["correlation_id"]
            == invoked["correlation_id"]
            == terminal["correlation_id"]
            == _CORRELATION_ID
        )

    @pytest.mark.asyncio
    async def test_full_chain_proof_packet(
        self, tmp_path: Path, topics: dict[str, str]
    ) -> None:
        """All §D2 proof artifacts present together in one assertion block."""
        gen_capture, exec_capture, terminal = await self._run_chain(tmp_path)
        deploy = gen_capture.payloads_for(topics["deploy"])[0]
        reg = gen_capture.payloads_for(topics["registration"])[0]
        invoked = exec_capture.payloads_for(topics["invoked"])[0]

        proof = {
            "generated_contract_hash": deploy["generated_contract_hash"],
            "generated_handler_hash": deploy["generated_handler_hash"],
            "registration_event_topic": topics["registration"],
            "registration_correlation_id": reg["correlation_id"],
            "invocation_event_topic": topics["invoked"],
            "invocation_status": invoked["status"],
            "invocation_output": invoked["output"],
        }
        # generated output hash present
        assert proof["generated_contract_hash"].startswith("sha256:")
        assert proof["generated_handler_hash"].startswith("sha256:")
        # registration event present + correct topic
        assert proof["registration_event_topic"] == (
            "onex.evt.platform.node-registration.v1"
        )
        # invocation event present + verified output
        assert proof["invocation_event_topic"] == (
            "onex.evt.omnimarket.generated-node-invoked.v1"
        )
        assert proof["invocation_status"] == "completed"
        assert proof["invocation_output"]["node"] == "golden_echo"
        # terminal result is the emitted invocation event (no divergence).
        assert invoked == terminal
