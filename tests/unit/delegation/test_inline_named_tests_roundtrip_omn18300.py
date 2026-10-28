# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Inline tests survive the same JSON boundary used by deployed dispatch."""

import json

import pytest
from omnibase_core.models.delegation.wire import ModelDelegationNamedTests
from omnibase_infra.runtime.service_delegation_dispatch_port import _named_tests_wire

from omnimarket.models.delegation.wire.model_named_test_run_request import (
    ModelNamedTestRunRequest,
    ModelNamedTestSpec,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_named_test_run import (
    HandlerNamedTestRun,
)

pytestmark = pytest.mark.unit


def _spec() -> ModelNamedTestSpec:
    spec = ModelNamedTestSpec.from_metadata(
        {
            "named_tests": "test_solution.py::test_canonical",
            "named_tests_answer_path": "solution.py",
            "named_tests_files": json.dumps(
                {
                    "test_solution.py": (
                        "from solution import hash_canonical_json\n"
                        "def test_canonical():\n"
                        "    assert hash_canonical_json({'b': 2, 'a': 1}) == "
                        "hash_canonical_json({'a': 1, 'b': 2})\n"
                    )
                }
            ),
        }
    )
    assert spec is not None
    return spec


def test_inline_files_reach_the_deployed_wire_decoder() -> None:
    spec = _spec()
    payload = spec.model_dump(mode="json")
    assert payload["test_files"] == [
        {"path": f.path, "content": f.content} for f in spec.test_files
    ]
    wire = ModelDelegationNamedTests.model_validate(_named_tests_wire(spec))
    restored = ModelNamedTestSpec.model_validate_json(wire.model_dump_json())
    assert restored.test_files == spec.test_files


def test_nested_runner_request_keeps_inline_sources() -> None:
    request = ModelNamedTestRunRequest(
        spec=_spec(),
        answer_content=(
            "import json\n"
            "def hash_canonical_json(value):\n"
            "    return json.dumps(value, sort_keys=True)\n"
        ),
    )
    restored = ModelNamedTestRunRequest.model_validate_json(request.model_dump_json())
    result = HandlerNamedTestRun().handle(restored)
    assert result.run is not None, result.detail
    assert result.run.passed, result.detail
