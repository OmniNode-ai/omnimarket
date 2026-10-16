# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Protocol tests for the codegen handler-test acceptance control (OMN-21153).

The EFFECT genuinely shells out to pytest twice per control (real handler,
stubbed mutant), grading each run through the delegated test loop's computes.
The pinned sources discriminate the three outcomes the control exists to tell
apart: a real handler accepted, a behaviorally-broken handler rejected, and a
vacuous test that passes the mutant rejected as a control that did not fail.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.codegen.models import (
    ModelCodegenHandlerTestCommand,
    ModelCodegenPipelineState,
    ModelCodegenSpec,
)
from omnimarket.nodes.node_codegen_handler_test_effect.handlers.handler_codegen_handler_test import (
    HandlerCodegenHandlerTest,
)

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_codegen_handler_test_effect"
    / "contract.yaml"
)

_REAL_HANDLER_SOURCE = (
    "class NodeCompute:\n"
    "    pass\n"
    "\n"
    "\n"
    "class NodeGreeterCompute(NodeCompute):\n"
    "    def handle(self, name: str) -> str:\n"
    '        return f"hello {name}"\n'
)

# Behaviorally broken but structurally clean: non-empty body, no stub markers,
# type-checks — only the acceptance control can catch it.
_BROKEN_HANDLER_SOURCE = (
    "class NodeCompute:\n"
    "    pass\n"
    "\n"
    "\n"
    "class NodeGreeterCompute(NodeCompute):\n"
    "    def handle(self, name: str) -> str:\n"
    '        return ""\n'
)

_GOOD_TEST_SOURCE = """\
from node_greeter_compute.handlers.handler_greeter_compute import (
    NodeGreeterCompute,
)


def test_handle_greets_by_name():
    assert NodeGreeterCompute().handle("omni") == "hello omni"
"""

_VACUOUS_TEST_SOURCE = "def test_nothing():\n    assert True\n"

_SECRET_PROBE = "OMN_CODEGEN_HANDLER_TEST_SECRET_PROBE"

_ENV_PROBE_TEST_SOURCE = f"""\
import os

from node_greeter_compute.handlers.handler_greeter_compute import (
    NodeGreeterCompute,
)


def test_handle_greets_by_name():
    assert {_SECRET_PROBE!r} not in os.environ
    assert NodeGreeterCompute().handle("omni") == "hello omni"
"""

# A test that rigs the mutant run from the real run: the real run leaves a
# marker where a mutant laid out under the real run's directory would find it,
# and the test skips its assertion when the marker is there.
_RIGGING_TEST_SOURCE = """\
from pathlib import Path

from node_greeter_compute.handlers.handler_greeter_compute import (
    NodeGreeterCompute,
)

_ROOT = Path(__file__).resolve().parents[1]


def test_handle_greets_by_name():
    if (_ROOT / "rigged").exists():
        return
    assert NodeGreeterCompute().handle("omni") == "hello omni"
    (_ROOT / "mutant").mkdir(exist_ok=True)
    (_ROOT / "mutant" / "rigged").write_text("", encoding="utf-8")
"""

_ASYNC_HANDLER_SOURCE = (
    "class NodeCompute:\n"
    "    pass\n"
    "\n"
    "\n"
    "class NodeGreeterCompute(NodeCompute):\n"
    "    async def handle(self, name: str) -> str:\n"
    '        return f"hello {name}"\n'
)

_ASYNC_TEST_SOURCE = """\
import asyncio

from node_greeter_compute.handlers.handler_greeter_compute import (
    NodeGreeterCompute,
)


def test_handle_greets_by_name():
    assert asyncio.run(NodeGreeterCompute().handle("omni")) == "hello omni"
"""


def _state(
    source: str, handler_test_source: str = _GOOD_TEST_SOURCE
) -> ModelCodegenPipelineState:
    return ModelCodegenPipelineState(
        spec=ModelCodegenSpec(
            node_name="NodeGreeterCompute",
            namespace="omninode.services.greeter.compute",
            archetype="compute",
            handler_test_source=handler_test_source,
        ),
        correlation_id="corr-1",
        source_text=source,
    )


def _run(source: str, test_source: str = _GOOD_TEST_SOURCE):
    return HandlerCodegenHandlerTest().handle(
        ModelCodegenHandlerTestCommand(
            state=_state(source, handler_test_source=test_source)
        )
    )


@pytest.mark.unit
class TestAcceptanceControl:
    def test_real_handler_accepted_mutant_failed(self) -> None:
        result = _run(_REAL_HANDLER_SOURCE)
        assert result.real_outcome == "passed"
        assert result.mutant_outcome == "failed_call"
        assert result.grade_status == "accepted_call"
        assert result.accepted is True

    def test_broken_handler_rejected(self) -> None:
        result = _run(_BROKEN_HANDLER_SOURCE)
        assert result.real_outcome == "failed_call"
        assert result.accepted is False
        assert result.mutant_outcome == "no_tests"

    def test_vacuous_test_rejected_control_did_not_fail(self) -> None:
        result = _run(_REAL_HANDLER_SOURCE, test_source=_VACUOUS_TEST_SOURCE)
        assert result.real_outcome == "passed"
        assert result.mutant_outcome == "passed"
        assert result.grade_status == "control_did_not_fail"
        assert result.accepted is False

    def test_run_does_not_inherit_process_secrets(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_SECRET_PROBE, "not-for-generated-code")
        result = _run(_REAL_HANDLER_SOURCE, test_source=_ENV_PROBE_TEST_SOURCE)
        assert result.real_outcome == "passed"
        assert result.accepted is True

    def test_real_run_cannot_rig_mutant_run(self) -> None:
        result = _run(_REAL_HANDLER_SOURCE, test_source=_RIGGING_TEST_SOURCE)
        assert result.real_outcome == "passed"
        assert result.mutant_outcome == "failed_call"
        assert result.accepted is True

    def test_async_handler_mutant_derived(self) -> None:
        result = _run(_ASYNC_HANDLER_SOURCE, test_source=_ASYNC_TEST_SOURCE)
        assert result.real_outcome == "passed"
        assert result.mutant_outcome == "failed_call"
        assert result.accepted is True

    def test_missing_test_source_rejected(self) -> None:
        result = _run(_REAL_HANDLER_SOURCE, test_source="")
        assert result.accepted is False
        assert result.real_outcome == "no_tests"


@pytest.mark.unit
class TestContractShape:
    def test_contract_declares_bus_topics(self) -> None:
        contract = yaml.safe_load(_CONTRACT_PATH.read_text())
        event_bus = contract["event_bus"]
        assert event_bus["subscribe_topics"] == [
            "onex.cmd.omnimarket.codegen-handler-test-requested.v1"
        ]
        assert event_bus["publish_topics"] == [
            "onex.evt.omnimarket.codegen-handler-test-completed.v1"
        ]
