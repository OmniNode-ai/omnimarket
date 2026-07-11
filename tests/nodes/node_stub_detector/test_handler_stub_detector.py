# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Protocol tests for node_stub_detector — pure deterministic stub detection.

Fixtures are node source strings. They assert: N stub markers -> N detected; a
fully-implemented method -> 0; and that a marker appearing only inside a string
literal is not a false positive (comment markers are matched against real
comment tokens and NotImplementedError against the AST).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_stub_detector.handlers.handler_stub_detector import (
    HandlerStubDetector,
    detect_stubs,
)
from omnimarket.nodes.node_stub_detector.models.model_stub_detection_result import (
    ModelStubDetectionResult,
)
from omnimarket.nodes.node_stub_detector.models.model_stub_detector_request import (
    ModelStubDetectorRequest,
)

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_stub_detector"
    / "contract.yaml"
)

# --- fixture node sources ------------------------------------------------------

_TWO_STUBS_SOURCE = """\
class NodeThingEffect(NodeEffect):
    async def step_one(self):
        # IMPLEMENTATION REQUIRED
        ...

    async def step_two(self):
        raise NotImplementedError("later")
"""

_TODO_STUB_SOURCE = """\
class NodeTodoEffect(NodeEffect):
    async def do_it(self):  # TODO: implement
        return None
"""

_PASS_STUB_SOURCE = """\
class NodePassEffect(NodeEffect):
    async def later(self):
        pass  # Stub
"""

_FULLY_IMPLEMENTED_SOURCE = """\
class NodeRealEffect(NodeEffect):
    async def compute(self, x):
        total = 0
        for i in range(x):
            total += i
        return total
"""

# A method whose body only *mentions* markers inside string literals — must not
# be flagged (no comment token, no Raise node).
_MARKER_IN_STRING_SOURCE = """\
class NodeStringEffect(NodeEffect):
    async def describe(self):
        return "raise NotImplementedError is just text here"

    async def note(self):
        label = "# TODO: not a real marker"
        return label
"""

# Sync methods are never flagged (only async methods are candidate stubs).
_SYNC_METHOD_SOURCE = """\
class NodeSyncEffect(NodeEffect):
    def helper(self):
        raise NotImplementedError
"""

# Comment marker precedence over the raise marker.
_TODO_AND_RAISE_SOURCE = """\
class NodeBothEffect(NodeEffect):
    async def m(self):
        # TODO: finish
        raise NotImplementedError
"""

# IMPLEMENTATION REQUIRED precedence over TODO.
_IMPL_AND_TODO_SOURCE = """\
class NodePriorityEffect(NodeEffect):
    async def m(self):
        # IMPLEMENTATION REQUIRED
        # TODO: also this
        ...
"""

_SYNTAX_ERROR_SOURCE = "class Broken(NodeEffect):\n    async def m(:\n        pass\n"


def _detect(source: str) -> ModelStubDetectionResult:
    return HandlerStubDetector().handle(ModelStubDetectorRequest(source_text=source))


@pytest.mark.unit
class TestStubCounting:
    def test_two_stubs_detected(self) -> None:
        result = _detect(_TWO_STUBS_SOURCE)
        assert len(result.stubs) == 2
        names = {stub.method_name for stub in result.stubs}
        assert names == {"step_one", "step_two"}

    def test_fully_implemented_method_yields_zero(self) -> None:
        result = _detect(_FULLY_IMPLEMENTED_SOURCE)
        assert result.stubs == ()

    def test_sync_methods_are_never_flagged(self) -> None:
        result = _detect(_SYNC_METHOD_SOURCE)
        assert result.stubs == ()


@pytest.mark.unit
class TestMarkerClassification:
    def test_implementation_required_marker(self) -> None:
        result = _detect(_TWO_STUBS_SOURCE)
        step_one = next(s for s in result.stubs if s.method_name == "step_one")
        assert step_one.marker == "# IMPLEMENTATION REQUIRED"

    def test_raise_not_implemented_marker(self) -> None:
        result = _detect(_TWO_STUBS_SOURCE)
        step_two = next(s for s in result.stubs if s.method_name == "step_two")
        assert step_two.marker == "raise NotImplementedError"

    def test_todo_marker(self) -> None:
        result = _detect(_TODO_STUB_SOURCE)
        assert len(result.stubs) == 1
        assert result.stubs[0].marker == "# TODO:"

    def test_pass_stub_marker(self) -> None:
        result = _detect(_PASS_STUB_SOURCE)
        assert len(result.stubs) == 1
        assert result.stubs[0].marker == "pass  # Stub"

    def test_comment_marker_precedes_raise(self) -> None:
        result = _detect(_TODO_AND_RAISE_SOURCE)
        assert len(result.stubs) == 1
        assert result.stubs[0].marker == "# TODO:"

    def test_implementation_required_precedes_todo(self) -> None:
        result = _detect(_IMPL_AND_TODO_SOURCE)
        assert len(result.stubs) == 1
        assert result.stubs[0].marker == "# IMPLEMENTATION REQUIRED"


@pytest.mark.unit
class TestSignatureAndFalsePositives:
    def test_signature_is_first_line_of_method(self) -> None:
        result = _detect(_TODO_STUB_SOURCE)
        assert result.stubs[0].signature == "async def do_it(self):  # TODO: implement"

    def test_marker_only_in_string_literal_not_flagged(self) -> None:
        result = _detect(_MARKER_IN_STRING_SOURCE)
        assert result.stubs == ()

    def test_syntax_error_yields_empty(self) -> None:
        result = _detect(_SYNTAX_ERROR_SOURCE)
        assert result.stubs == ()


@pytest.mark.unit
class TestDeterminismAndPurity:
    def test_detection_is_deterministic(self) -> None:
        first = _detect(_TWO_STUBS_SOURCE)
        second = _detect(_TWO_STUBS_SOURCE)
        assert first == second

    def test_pure_function_matches_handler(self) -> None:
        via_fn = detect_stubs(_TWO_STUBS_SOURCE)
        via_handler = _detect(_TWO_STUBS_SOURCE)
        assert via_fn == via_handler.stubs


@pytest.mark.unit
class TestContractStateCoverage:
    """Cover the contract-declared output states and published topic."""

    def test_contract_declares_stubs_output(self) -> None:
        contract = yaml.safe_load(_CONTRACT_PATH.read_text())
        assert set(contract["outputs"]) == {"stubs"}

    def test_contract_declares_bus_topics(self) -> None:
        contract = yaml.safe_load(_CONTRACT_PATH.read_text())
        assert (
            contract["terminal_event"]
            == "onex.evt.omnimarket.stub-detection-completed.v1"
        )
        assert (
            "onex.evt.omnimarket.stub-detection-completed.v1"
            in contract["event_bus"]["publish_topics"]
        )
