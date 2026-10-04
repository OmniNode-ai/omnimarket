# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19969: baseline selection and unresolved pricing stay explicit."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest

from omnimarket import pricing
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillResponse,
)
from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelDelegateSkillSavingsProjection,
    ModelDelegateSkillTerminalProjection,
)

DEFAULT_BASELINE_MODEL = pricing.DEFAULT_BASELINE_MODEL
_load_table = pricing._load_table
estimate_baseline_cost_usd = pricing.estimate_baseline_cost_usd


def _select_baseline(**kwargs: object) -> object:
    resolver = getattr(pricing, "resolve_baseline_model", None)
    assert callable(resolver), "pricing.resolve_baseline_model is missing"
    return resolver(**kwargs)


def test_fixed_default_is_configured_and_never_opus_46() -> None:
    assert DEFAULT_BASELINE_MODEL != "claude-opus-4-6"
    selected = _select_baseline(overlay={}, store={})
    assert selected.model == DEFAULT_BASELINE_MODEL
    assert selected.selection_case == "fixed_default"


def test_model_absent_from_manifest_has_no_numeric_baseline_cost() -> None:
    assert (
        estimate_baseline_cost_usd(
            prompt_tokens=100,
            completion_tokens=50,
            baseline_model="model-not-in-manifest-omn19969",
        )
        is None
    )


def test_baseline_model_uses_overlay_before_store_and_names_session_source() -> None:
    selected = _select_baseline(
        overlay={"pricing": {"baseline_model": "claude-sonnet-4-20250514"}},
        store={"pricing": {"baseline_model": "claude-opus-4-6"}},
    )

    assert selected.model == "claude-sonnet-4-20250514"
    assert selected.selection_case == "overlay"
    assert selected.state == "RESOLVED"


def test_baseline_model_falls_back_to_store_then_fixed_default() -> None:
    from_store = _select_baseline(
        overlay={},
        store={"pricing": {"baseline_model": "claude-sonnet-4-20250514"}},
    )
    fixed = _select_baseline(overlay={}, store={})

    assert from_store.model == "claude-sonnet-4-20250514"
    assert from_store.selection_case == "store"
    assert fixed.model == DEFAULT_BASELINE_MODEL
    assert fixed.selection_case == "fixed_default"


def test_known_session_model_has_priority_and_names_the_case() -> None:
    selected = _select_baseline(
        overlay={"pricing": {"baseline_model": "claude-sonnet-4-20250514"}},
        store={"pricing": {"baseline_model": "claude-sonnet-4-20250514"}},
        session_model="claude-opus-4-6",
    )
    assert selected.model == "claude-opus-4-6"
    assert selected.selection_case == "session_model"


def test_unknown_baseline_keeps_manifest_version_and_typed_unresolved_state() -> None:
    selected = _select_baseline(
        overlay={"pricing": {"baseline_model": "model-not-in-manifest-omn19969"}},
        store={},
    )

    assert selected.model == "model-not-in-manifest-omn19969"
    assert selected.state == "BASELINE_UNRESOLVED"
    assert selected.pricing_manifest_version == pricing.get_manifest_version_int()


def test_fixed_default_resolves_with_fixture_manifest_and_provenance(
    monkeypatch: object,
) -> None:
    fixture_manifest = SimpleNamespace(
        schema_version="7.4.1",
        get_entry=lambda model: object() if model == DEFAULT_BASELINE_MODEL else None,
        estimate_cost=lambda *_args: SimpleNamespace(estimated_cost_usd=Decimal("0.5")),
    )
    monkeypatch.setattr(pricing, "_load_table", lambda: fixture_manifest)
    selected = _select_baseline(overlay={}, store={})

    assert DEFAULT_BASELINE_MODEL == "claude-sonnet-5-5"
    assert selected.model == DEFAULT_BASELINE_MODEL
    assert selected.state == "RESOLVED"
    assert selected.pricing_manifest_version == 7

    receipt = ModelDelegateSkillResponse(
        status="completed",
        correlation_id=UUID("2e9f0b13-6c7d-5e8f-9012-3b4c5d6e7f81"),
        task_type="code_generation",
        model_cloud_baseline=selected.model,
        baseline_source=selected.selection_case,
        baseline_state=selected.state,
        pricing_manifest_version=selected.pricing_manifest_version,
        metrics={"cost_usd": 0.01, "cost_savings_usd": 0.02},
    )
    assert receipt.model_cloud_baseline == "claude-sonnet-5-5"
    assert receipt.pricing_manifest_version == 7

    terminal = ModelDelegateSkillTerminalProjection(
        status="completed",
        correlation_id=receipt.correlation_id,
        task_type=receipt.task_type,
        provider="local-qwen",
        model_name="local-qwen",
        model_cloud_baseline=receipt.model_cloud_baseline,
        baseline_source=receipt.baseline_source,
        baseline_state=receipt.baseline_state,
        pricing_manifest_version=receipt.pricing_manifest_version,
        quality_gate_passed=True,
        metrics={
            "input_tokens": 11,
            "output_tokens": 22,
            "cost_usd": 0.01,
            "cost_savings_usd": 0.02,
        },
    )
    projection = ModelDelegateSkillSavingsProjection.from_terminal_event(
        terminal,
        baseline_model=selected.model,
    )
    assert projection is not None
    assert projection.model_cloud_baseline == "claude-sonnet-5-5"
    assert projection.pricing_manifest_version == 7


def test_real_manifest_resolves_fixed_default_from_current_pricing_table() -> None:
    selected = pricing.resolve_baseline_model(overlay={}, store={})

    # The installed manifest prices the default from omnibase-infra 0.38.65
    # (omnibase_infra#4400), the floor this repo pins; below it this fails.
    assert selected.model == "claude-sonnet-5-5"
    assert _load_table().get_entry(selected.model) is not None
    assert selected.state == "RESOLVED"
    assert selected.selection_case == "fixed_default"
    assert estimate_baseline_cost_usd(
        prompt_tokens=100,
        completion_tokens=50,
        baseline_model=selected.model,
    ) == pytest.approx(0.0007)


def test_unresolved_receipt_keeps_null_savings_and_selection_reason() -> None:
    receipt = ModelDelegateSkillResponse(
        status="completed",
        correlation_id=UUID("2e9f0b13-6c7d-5e8f-9012-3b4c5d6e7f81"),
        task_type="code_generation",
        model_cloud_baseline="model-not-in-manifest-omn19969",
        baseline_source="session_model",
        baseline_state="BASELINE_UNRESOLVED",
        pricing_manifest_version=pricing.get_manifest_version_int(),
        metrics={"cost_usd": 0.01, "cost_savings_usd": None},
    )

    assert receipt.metrics.cost_savings_usd is None
    assert receipt.baseline_state == "BASELINE_UNRESOLVED"
    assert receipt.baseline_source == "session_model"


def test_unreadable_manifest_preserves_unresolved_receipt_without_savings(
    monkeypatch: object,
) -> None:
    unreadable_manifest_fallback = SimpleNamespace(
        schema_version="0",
        get_entry=lambda _model: None,
        estimate_cost=lambda *_args: SimpleNamespace(estimated_cost_usd=None),
    )
    monkeypatch.setattr(pricing, "_load_table", lambda: unreadable_manifest_fallback)

    selected = _select_baseline(overlay={}, store={})
    assert selected.model == DEFAULT_BASELINE_MODEL
    assert selected.state == "BASELINE_UNRESOLVED"
    assert selected.pricing_manifest_version == 0
    assert (
        estimate_baseline_cost_usd(
            prompt_tokens=100,
            completion_tokens=50,
            baseline_model=selected.model,
        )
        is None
    )

    receipt = ModelDelegateSkillResponse(
        status="completed",
        correlation_id=UUID("2e9f0b13-6c7d-5e8f-9012-3b4c5d6e7f81"),
        task_type="code_generation",
        model_cloud_baseline=selected.model,
        baseline_source=selected.selection_case,
        baseline_state=selected.state,
        pricing_manifest_version=selected.pricing_manifest_version,
        metrics={"cost_usd": 0.01, "cost_savings_usd": None},
    )
    terminal_data = receipt.model_dump()
    terminal_data["quality_gate_passed"] = True
    terminal = ModelDelegateSkillTerminalProjection(**terminal_data)

    assert receipt.model_cloud_baseline == DEFAULT_BASELINE_MODEL
    assert receipt.pricing_manifest_version == 0
    assert receipt.metrics.cost_savings_usd is None
    assert receipt.baseline_state == "BASELINE_UNRESOLVED"
    assert (
        ModelDelegateSkillSavingsProjection.from_terminal_event(
            terminal,
            baseline_model=selected.model,
        )
        is None
    )


def test_savings_projection_carries_baseline_and_manifest_provenance() -> None:
    terminal = ModelDelegateSkillTerminalProjection(
        status="completed",
        correlation_id=UUID("2e9f0b13-6c7d-5e8f-9012-3b4c5d6e7f81"),
        task_type="code_generation",
        provider="local-qwen",
        model_name="local-qwen",
        model_cloud_baseline="claude-sonnet-4-20250514",
        baseline_source="session_model",
        pricing_manifest_version=1,
        quality_gate_passed=True,
        metrics={
            "input_tokens": 11,
            "output_tokens": 22,
            "cost_usd": 0.01,
            "cost_savings_usd": 0.02,
        },
    )

    projection = ModelDelegateSkillSavingsProjection.from_terminal_event(
        terminal,
        baseline_model=DEFAULT_BASELINE_MODEL,
    )

    assert projection is not None
    assert projection.model_cloud_baseline == "claude-sonnet-4-20250514"
    assert projection.baseline_source == "session_model"
    assert projection.pricing_manifest_version == 1


@pytest.mark.parametrize(
    ("manifest_version", "expected_manifest_version"),
    [(1, "1"), (0, "0")],
)
def test_savings_row_persists_manifest_version(
    monkeypatch: object,
    manifest_version: int,
    expected_manifest_version: str,
) -> None:
    from omnimarket.nodes.node_projection_savings.handlers import (
        handler_projection_savings as module,
    )

    terminal = ModelDelegateSkillTerminalProjection(
        status="completed",
        correlation_id=UUID("2e9f0b13-6c7d-5e8f-9012-3b4c5d6e7f81"),
        task_type="code_generation",
        provider="local-qwen",
        model_name="local-qwen",
        model_cloud_baseline="claude-sonnet-4-20250514",
        baseline_source="session_model",
        pricing_manifest_version=manifest_version,
        quality_gate_passed=True,
        metrics={
            "input_tokens": 11,
            "output_tokens": 22,
            "cost_usd": 0.01,
            "cost_savings_usd": 0.02,
        },
    )
    projection = ModelDelegateSkillSavingsProjection.from_terminal_event(
        terminal,
        baseline_model=DEFAULT_BASELINE_MODEL,
    )
    assert projection is not None

    class CaptureDatabase:
        row: dict[str, object] | None = None

        def upsert(self, table: str, conflict_key: str, row: dict[str, object]) -> bool:
            self.row = row
            return True

        def query(
            self,
            table: str,
            filters: dict[str, object] | None = None,
            *,
            order_by: str | None = None,
            descending: bool = False,
            limit: int | None = None,
        ) -> list[dict[str, object]]:
            return []

    monkeypatch.setattr(
        module, "resolve_registry_tenant_uuid_or_none", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        module,
        "sync_house_tenant_write_uuid",
        lambda *_args, **_kwargs: "house-tenant-uuid",
    )
    db = CaptureDatabase()
    result = module.HandlerProjectionSavings().project_delegate_skill_savings(
        projection, db
    )

    assert result.rows_upserted == 1
    assert db.row is not None
    assert db.row["model_cloud_baseline"] == "claude-sonnet-4-20250514"
    assert db.row["pricing_manifest_version"] == expected_manifest_version
