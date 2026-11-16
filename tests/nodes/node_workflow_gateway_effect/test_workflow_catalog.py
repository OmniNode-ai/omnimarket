# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The workflow catalog: deny by default, validated whole at build, payloads checked by schema.

Exemplar: tests/nodes/node_plan_list/test_http_plans.py (typed refusals); here at the pure seam.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_workflow_gateway_effect.handlers.workflow_catalog import (
    WorkflowCatalog,
    WorkflowCatalogError,
    validate_workflow_payload,
)
from omnimarket.nodes.node_workflow_gateway_effect.models.model_workflow_catalog import (
    ModelWorkflowCatalog,
    ModelWorkflowCatalogEntry,
)
from tests.nodes.node_workflow_gateway_effect.fakes import SKILL_TOPIC, sample_catalog

pytestmark = pytest.mark.unit


def _with(index: int, **changes: object) -> ModelWorkflowCatalog:
    entries = list(sample_catalog().workflows)
    entries[index] = entries[index].model_copy(update=changes)
    return ModelWorkflowCatalog(workflows=tuple(entries))


def test_lookup_is_deny_by_default_and_separates_fenced_entries() -> None:
    catalog = WorkflowCatalog(sample_catalog())
    assert catalog.allowed() == ("delegate-skill", "delegation-inference")
    assert catalog.resolve("delegate-skill") is not None
    assert catalog.resolve("push-validation") is None
    assert catalog.fenced("push-validation") is not None
    assert catalog.fenced("delegate-skill") is None
    assert catalog.resolve("anything-else") is None
    assert [e.workflow_type for e in catalog.contracts()] == list(catalog.allowed())


@pytest.mark.parametrize(
    ("catalog", "needle"),
    [
        pytest.param(
            ModelWorkflowCatalog(
                workflows=(sample_catalog().workflows[0], sample_catalog().workflows[0])
            ),
            "duplicate",
            id="duplicate-type",
        ),
        pytest.param(
            _with(0, command_topic="delegate-skill"),
            "onex contract shape",
            id="bad-topic",
        ),
        pytest.param(
            _with(0, command_topic=SKILL_TOPIC.replace(".cmd.", ".evt.")),
            ".cmd.",
            id="not-a-command-topic",
        ),
        pytest.param(
            ModelWorkflowCatalog(
                workflows=tuple(
                    e.model_copy(update={"enabled": False})
                    for e in sample_catalog().workflows
                )
            ),
            "no enabled entry",
            id="all-fenced",
        ),
        pytest.param(
            _with(
                0,
                payload_schema={"type": "object", "properties": {}},
            ),
            "additionalProperties",
            id="open-schema",
        ),
        pytest.param(
            _with(
                0,
                payload_schema={"type": "not-a-type", "additionalProperties": False},
            ),
            "invalid payload_schema",
            id="invalid-schema",
        ),
    ],
)
def test_a_malformed_catalog_is_refused_at_build(
    catalog: ModelWorkflowCatalog, needle: str
) -> None:
    with pytest.raises(WorkflowCatalogError, match=needle):
        WorkflowCatalog(catalog)


def test_payload_validation_reports_sorted_field_errors() -> None:
    entry: ModelWorkflowCatalogEntry = sample_catalog().workflows[0]
    assert (
        validate_workflow_payload(
            entry, {"prompt": "p", "task_type": "t", "source": "codex"}
        )
        == []
    )
    errors = validate_workflow_payload(
        entry, {"prompt": "", "source": "nobody", "extra": 1}
    )
    assert [e["field"] for e in errors] == sorted(e["field"] for e in errors)
    fields = {e["field"] for e in errors}
    assert {
        "prompt",
        "source",
        "$",
    } <= fields  # empty prompt, bad source, missing/extra at the root


def test_an_invalid_schema_is_a_catalog_error() -> None:
    entry = (
        sample_catalog()
        .workflows[0]
        .model_copy(update={"payload_schema": {"type": "not-a-type"}})
    )
    with pytest.raises(WorkflowCatalogError):
        validate_workflow_payload(entry, {})
