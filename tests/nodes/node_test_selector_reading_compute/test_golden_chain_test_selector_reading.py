# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain of the test selector reading, through the packaged contract (OMN-22951)."""

from __future__ import annotations

import importlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast, get_type_hints

import pytest
import yaml
from pydantic import BaseModel

pytestmark = pytest.mark.unit

NODE = "omnimarket.nodes.node_test_selector_reading_compute"
NOW = datetime(2026, 11, 7, 16, 0, tzinfo=UTC)
WORKFLOW = "FLAG: ${{ vars.ENABLE_SMART_TESTS == 'true' && 'on' || 'off' }}"
REPOS = ("omnibase_core", "omnibase_infra", "omniclaude", "omnimarket")


def _observation(repo: str, *, flag: str | None) -> dict[str, Any]:
    return {
        "repo": repo,
        "workflow_text": WORKFLOW,
        "flag_value": flag,
        "flag_readable": True,
        "required_contexts": ["CI Summary", "Detect Changes"],
    }


def _bound() -> tuple[Any, type[BaseModel], type[BaseModel], dict[str, Any]]:
    package = importlib.import_module(NODE)
    assert package.__file__ is not None
    contract = yaml.safe_load(
        (Path(package.__file__).parent / "contract.yaml").read_text()
    )
    binding = contract["handler"]
    handler_class = getattr(
        importlib.import_module(binding["module"]), binding["class"]
    )

    def model(ref: str) -> type[BaseModel]:
        module, _, name = ref.rpartition(".")
        result = getattr(importlib.import_module(module), name)
        assert issubclass(result, BaseModel)
        return cast("type[BaseModel]", result)

    return (
        handler_class,
        model(binding["input_model"]),
        model(
            contract["output_model"]["module"] + "." + contract["output_model"]["name"]
        ),
        contract,
    )


def test_contract_binds_the_handler_with_the_canonical_signature() -> None:
    handler_class, request_class, result_class, contract = _bound()
    hints = get_type_hints(handler_class.handle)
    assert hints["request"] is request_class
    assert hints["return"] is result_class
    assert contract["name"] == "node_test_selector_reading_compute"
    assert contract["metadata"]["related_tickets"] == ["OMN-22951"]
    assert contract["event_bus"]["subscribe_topics"] == [
        "onex.cmd.omnimarket.test-selector-read-requested.v1"
    ]
    assert contract["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.test-selector-read.v1"
    ]


def test_golden_four_active_repositories_read_working_with_zero_shadow_misses() -> None:
    handler_class, request_class, _, _ = _bound()
    request = request_class.model_validate(
        {"observations": [_observation(r, flag="true") for r in REPOS], "now": NOW}
    )
    reading = handler_class().handle(request)
    assert reading.state.value == "WORKING"
    assert reading.shadow_misses == 0
    assert [r.repo for r in reading.repos] == list(REPOS)


def test_error_a_disabled_selector_reads_not_working_with_a_shadow_miss() -> None:
    handler_class, request_class, _, _ = _bound()
    observations = [_observation(r, flag="true") for r in REPOS[:3]]
    observations.append(_observation(REPOS[3], flag=None))
    reading = handler_class().handle(
        request_class.model_validate({"observations": observations, "now": NOW})
    )
    assert reading.state.value == "NOT WORKING"
    assert reading.shadow_misses == 1
    assert "omnimarket" in reading.reason
