# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Admission at the durable handler boundary, with real independent SQLite connections."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from threading import Barrier
from uuid import uuid4

import pytest
from pydantic import ValidationError

from omnimarket.models.claude_review_budget.model_claude_review_usage import (
    ModelClaudeReviewUsage,
)
from omnimarket.models.claude_review_budget.model_review_budget_admission import (
    ModelReviewBudgetCommand,
    ModelReviewBudgetPolicy,
    ModelReviewBudgetQuote,
    ModelReviewInputBlock,
)
from omnimarket.nodes.node_claude_review_budget_admission_effect import (
    HandlerClaudeReviewBudgetAdmission,
    SqliteReviewBudgetStore,
)
from omnimarket.nodes.node_claude_review_budget_compute import HandlerClaudeReviewBudget

pytestmark = pytest.mark.unit
NOW = datetime(2026, 10, 2, tzinfo=UTC)
MODEL = "claude-sonnet-5-5"


def policy(**updates):
    values = {
        "version": "review-policy-v2",
        "cycle_anchor_at": datetime(2026, 10, 1, tzinfo=UTC),
        "aggregate_limit_usd": "1",
        "pacing_reserve_usd": "0",
        "key_limits": {
            "key-a": {"allowance_usd": "1", "reserve_usd": "0"},
            "key-b": {"allowance_usd": "1", "reserve_usd": "0"},
        },
        "personas": {
            "primary": {"tier": "required", "enforce_pacing": False},
            "extra": {"tier": "budgeted", "enforce_pacing": True},
        },
    }
    values.update(updates)
    return ModelReviewBudgetPolicy(**values)


def command(call_id=None, **updates):
    values = {
        "operation": "reserve",
        "call_id": call_id or uuid4(),
        "api_key_ref": "key-a",
        "model_id": MODEL,
        "persona_id": "primary",
        "policy_version": "review-policy-v2",
        "quote": ModelReviewBudgetQuote(
            model_id=MODEL, blocks=(ModelReviewInputBlock(tokens=0),), max_tokens=60_000
        ),
    }
    values.update(updates)
    return ModelReviewBudgetCommand(**values)


def handler(path, cfg=None, now=NOW):
    return HandlerClaudeReviewBudgetAdmission(
        store=SqliteReviewBudgetStore(path), policy=cfg or policy(), clock=lambda: now
    )


def advance(h, request, operation, **updates):
    return h.handle(request.model_copy(update={"operation": operation, **updates}))


def test_two_concurrent_calls_with_balance_for_one_admit_exactly_one(tmp_path):
    path = tmp_path / "budget.db"
    left, right = handler(path), handler(path)
    barrier = Barrier(2)

    def reserve(h):
        barrier.wait()
        return h.handle(command()).admitted

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, (left, right)))
    assert sorted(results) == [False, True]
    assert handler(path).handle(command()).reason == "key_limit"


def test_outstanding_batch_items_reduce_synchronous_allowance_after_restart(tmp_path):
    path = tmp_path / "budget.db"
    batch = command(mode="batch")  # USD 0.30, versus USD 0.60 synchronous.
    assert handler(path).handle(batch).admitted
    large = command(
        quote=ModelReviewBudgetQuote(model_id=MODEL, blocks=(), max_tokens=80_000)
    )
    assert handler(path).handle(large).reason == "key_limit"
    assert handler(tmp_path / "without-batch.db").handle(large).admitted
    assert handler(path).handle(command()).admitted
    result = handler(path).handle(command())
    assert not result.admitted
    assert result.key_committed_usd == Decimal("0.90")


def test_aggregate_refuses_while_each_key_still_has_allowance(tmp_path):
    h = handler(tmp_path / "budget.db", policy(aggregate_limit_usd="0.9"))
    assert h.handle(command()).admitted
    quote = ModelReviewBudgetQuote(model_id=MODEL, blocks=(), max_tokens=40_000)
    for ref in ("key-a", "key-b"):
        result = h.handle(command(api_key_ref=ref, quote=quote))
        assert result.key_committed_usd + Decimal("0.4") <= Decimal(1)
        assert not result.admitted
        assert result.reason == "aggregate_limit"
        assert result.aggregate_committed_usd == Decimal("0.6")


def test_server_side_optional_tier_refused_before_required_primary(tmp_path):
    h = handler(tmp_path / "budget.db")
    assert h.handle(command()).admitted
    small = ModelReviewBudgetQuote(model_id=MODEL, blocks=(), max_tokens=1)
    result = h.handle(command(persona_id="extra", quote=small))
    assert result.reason == "paced_budget"
    assert h.handle(command(quote=small)).admitted
    with pytest.raises(ValidationError):
        ModelReviewBudgetCommand(**command().model_dump(), tier="required")


def test_mixed_cache_ttls_schema_and_batch_are_priced_at_worst_case():
    compute = HandlerClaudeReviewBudget()
    quote = ModelReviewBudgetQuote(
        model_id=MODEL,
        blocks=(
            ModelReviewInputBlock(tokens=1000),
            ModelReviewInputBlock(tokens=1000, cache_ttl="5m"),
            ModelReviewInputBlock(tokens=1000, cache_ttl="1h"),
        ),
        schema_tokens=1000,
        max_tokens=1000,
    )
    assert compute.quote(quote) == Decimal("0.0225")
    assert compute.quote(quote.model_copy(update={"mode": "batch"})) == Decimal(
        "0.01125"
    )
    # Per-model dispatch uses the injected manifest, never an in-code Opus fallback.
    from omnibase_infra.models.pricing.model_pricing_entry import ModelPricingEntry
    from omnibase_infra.models.pricing.model_pricing_table import ModelPricingTable

    table = ModelPricingTable.from_yaml()
    table = table.model_copy(
        update={
            "models": {
                **table.models,
                "claude-opus-5-5": ModelPricingEntry(
                    input_cost_per_1k=0.004,
                    output_cost_per_1k=0.020,
                    effective_date="2026-10-18",
                    source="TEST_FIXTURE",
                ),
            }
        }
    )
    assert HandlerClaudeReviewBudget(pricing_table=table).quote(
        quote.model_copy(update={"model_id": "claude-opus-5-5"})
    ) == Decimal("0.045")


def test_settle_replaces_reservation_with_measured_usage_idempotently(tmp_path):
    path = tmp_path / "budget.db"
    h = handler(path)
    request = command()
    assert h.handle(request).admitted
    advance(h, request, "started")
    usage = ModelClaudeReviewUsage(
        call_id=request.call_id,
        api_key_ref="key-a",
        model_id=MODEL,
        observed_at=NOW,
        input_tokens=1000,
        cache_creation_input_tokens=2000,
        cache_creation_1h_input_tokens=1000,
        cache_read_input_tokens=1000,
        output_tokens=1000,
    )
    result = advance(h, request, "settle", usage=usage)
    assert result.aggregate_committed_usd == Decimal("0.0187")
    assert advance(handler(path), request, "settle", usage=usage) == result
    assert h.handle(command()).admitted


def test_cancel_after_dispatch_keeps_reservation_until_confirmed_zero_charge(tmp_path):
    h = handler(tmp_path / "budget.db")
    request = command()
    assert h.handle(request).admitted
    advance(h, request, "started")
    result = advance(h, request, "release")
    assert result.reason == "charge_unknown"
    assert not h.handle(command()).admitted
    result = advance(h, request, "release", zero_charge_evidence="provider receipt")
    assert result.aggregate_committed_usd == 0
    assert h.handle(command()).admitted


def test_cancel_before_dispatch_releases_and_never_reuses_call_id(tmp_path):
    h = handler(tmp_path / "budget.db")
    request = command()
    assert h.handle(request).admitted
    assert advance(h, request, "release").aggregate_committed_usd == 0
    assert not h.handle(request).admitted
    assert h.handle(command()).admitted


def test_retry_is_idempotent_and_changed_payload_is_refused(tmp_path):
    h = handler(tmp_path / "budget.db")
    request = command()
    first = h.handle(request)
    assert h.handle(request) == first
    changed = request.model_copy(update={"api_key_ref": "key-b"})
    assert h.handle(changed).reason == "reservation_conflict"
    assert not h.handle(command()).admitted


def test_reconcile_requires_evidence_and_releases_only_named_reservations(tmp_path):
    h = handler(tmp_path / "budget.db")
    request = command(mode="batch")
    other = command(mode="batch")
    assert h.handle(request).admitted
    assert h.handle(other).admitted
    advance(h, request, "started")
    reconciled = advance(
        h,
        request,
        "reconcile",
        reconciled_spend_usd=Decimal("0.1"),
        reconcile_evidence="organization balance observation",
        resolved_call_ids=(request.call_id,),
    )
    assert reconciled.aggregate_committed_usd == Decimal("0.4")
    assert h.handle(command()).admitted


def test_unknown_persona_or_policy_or_model_fail_closed(tmp_path):
    h = handler(tmp_path / "budget.db")
    assert h.handle(command(persona_id="client-invented")).reason == "unknown_persona"
    assert h.handle(command(policy_version="old")).reason == "policy_mismatch"
    quote = ModelReviewBudgetQuote(model_id="not-priced", blocks=(), max_tokens=1)
    assert (
        h.handle(command(model_id="not-priced", quote=quote)).reason
        == "model_not_priced"
    )


def test_pending_reservation_survives_month_rollover(tmp_path):
    path = tmp_path / "budget.db"
    assert handler(path).handle(command()).admitted
    next_month = datetime(2026, 11, 2, tzinfo=UTC)
    assert not handler(path, now=next_month).handle(command()).admitted


def test_contract_dispatch_uses_server_policy_and_durable_state(tmp_path, monkeypatch):
    """Exercise the contract-resolved handler, as the runtime dispatcher constructs it."""
    import importlib
    from pathlib import Path

    import yaml

    node = (
        Path(__file__).resolve().parents[4]
        / "src/omnimarket/nodes/node_claude_review_budget_admission_effect"
    )
    contract = yaml.safe_load((node / "contract.yaml").read_text())
    route = contract["handler"]
    cls = getattr(importlib.import_module(route["module"]), route["class"])
    model_module, model_name = route["input_model"].rsplit(".", 1)
    input_model = getattr(importlib.import_module(model_module), model_name)
    overlay = tmp_path / "policy.yaml"
    now = datetime.now(UTC)
    overlay.write_text(
        yaml.safe_dump(
            policy(cycle_anchor_at=now.replace(day=1)).model_dump(mode="json")
        )
    )
    monkeypatch.setenv("CLAUDE_REVIEW_BUDGET_POLICY", str(overlay))
    monkeypatch.setenv("CLAUDE_REVIEW_BUDGET_DB", str(tmp_path / "authority.db"))
    first = command()
    assert cls().handle(input_model.model_validate(first.model_dump())).admitted
    assert (
        cls()
        .handle(
            input_model.model_validate(
                first.model_copy(update={"operation": "started"}).model_dump()
            )
        )
        .admitted
    )
    assert (
        cls().handle(first.model_copy(update={"operation": "started"})).reason
        == "already_started"
    )
    assert cls().handle(command()).reason == "key_limit"
    assert contract["runtime_dispatch"]["command_topic"].startswith("onex.cmd.")


def test_store_failure_never_returns_admission_or_leaves_a_reservation(tmp_path):
    from omnimarket.models.claude_review_budget.model_review_budget_admission import (
        ModelReviewBudgetTransition,
    )
    from omnimarket.nodes.node_claude_review_budget_reducer import (
        HandlerClaudeReviewBudgetReducer,
    )

    path = tmp_path / "budget.db"
    store = SqliteReviewBudgetStore(path)

    def reduce_then_fail(state):
        HandlerClaudeReviewBudgetReducer().handle(
            ModelReviewBudgetTransition(
                state=state,
                command=command(),
                policy=policy(),
                as_of=NOW,
                worst_case_usd=Decimal("0.6"),
            )
        )
        raise OSError("storage failure before commit")

    with pytest.raises(OSError, match="storage failure before commit"):
        store.transact(reduce_then_fail)
    assert handler(path).handle(command()).admitted


def test_per_key_reserve_is_preserved(tmp_path):
    cfg = policy(key_limits={"key-a": {"allowance_usd": "1", "reserve_usd": "0.5"}})
    assert handler(tmp_path / "budget.db", cfg).handle(command()).reason == "key_limit"


def test_one_hour_cache_usage_is_a_subset_of_total_creation():
    with pytest.raises(ValidationError, match="1-hour cache creation exceeds"):
        ModelClaudeReviewUsage(
            call_id=uuid4(),
            api_key_ref="key-a",
            model_id=MODEL,
            observed_at=NOW,
            input_tokens=0,
            cache_creation_input_tokens=1,
            cache_creation_1h_input_tokens=2,
            cache_read_input_tokens=0,
            output_tokens=0,
        )


def test_settlement_event_needs_only_call_id_and_measured_usage(tmp_path):
    h = handler(tmp_path / "budget.db")
    request = command(mode="batch")
    assert h.handle(request).admitted
    assert h.handle(
        ModelReviewBudgetCommand(operation="started", call_id=request.call_id)
    ).admitted
    usage = ModelClaudeReviewUsage(
        call_id=request.call_id,
        api_key_ref="key-a",
        model_id=MODEL,
        observed_at=NOW,
        input_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        output_tokens=1000,
    )
    result = h.handle(
        ModelReviewBudgetCommand(
            operation="settle", call_id=request.call_id, usage=usage
        )
    )
    assert result.aggregate_committed_usd == Decimal("0.005")


def test_schema_in_cached_prefix_is_reserved_at_cold_write_price():
    quote = ModelReviewBudgetQuote(
        model_id=MODEL,
        blocks=(ModelReviewInputBlock(tokens=1000, cache_ttl="1h"),),
        schema_tokens=1000,
        max_tokens=1,
    )
    assert HandlerClaudeReviewBudget().quote(quote) == Decimal("0.00801")


@pytest.mark.asyncio
async def test_contract_dispatch_bus_roundtrip_preserves_batch_liability(
    tmp_path, monkeypatch
):
    """Exercise the canonical adapter and its event bus on the lab host, with temporary state."""
    import yaml

    from omnimarket.adapters.codex.local_runtime_dispatch import LocalRuntimeDispatch
    from omnimarket.adapters.codex.runtime_client import (
        ModelDispatchBusCommand,
        default_command_topic,
        default_response_topic,
    )
    from omnimarket.models.claude_review_budget.model_review_budget_admission import (
        ModelReviewBudgetDecision,
    )

    overlay = tmp_path / "policy.yaml"
    now = datetime.now(UTC)
    overlay.write_text(
        yaml.safe_dump(
            policy(cycle_anchor_at=now.replace(day=1)).model_dump(mode="json")
        )
    )
    monkeypatch.setenv("CLAUDE_REVIEW_BUDGET_POLICY", str(overlay))
    monkeypatch.setenv("CLAUDE_REVIEW_BUDGET_DB", str(tmp_path / "authority.db"))

    async def dispatch(request):
        runtime = LocalRuntimeDispatch(
            adapter_command_topic=default_command_topic(),
            state_root=tmp_path / "runtime",
        )
        terminal, evidence = await runtime.dispatch(
            ModelDispatchBusCommand(
                command_name="node_claude_review_budget_admission_effect",
                requester="review-budget-test",
                correlation_id=uuid4(),
                payload=request.model_dump(mode="json"),
                response_topic=default_response_topic(),
                timeout_seconds=10,
            )
        )
        assert terminal.status == "completed", terminal.error_message
        assert evidence.runtime_observation.status == "OBSERVED"
        assert evidence.adapter_dispatch_binding.node_contract.status == "RESOLVED"
        assert terminal.payload is not None
        return ModelReviewBudgetDecision.model_validate(terminal.payload)

    assert (await dispatch(command(mode="batch"))).admitted
    assert (await dispatch(command())).admitted
    result = await dispatch(command())
    assert result.reason == "key_limit"
    assert result.aggregate_committed_usd == Decimal("0.9")


def test_key_rollover_and_aggregate_rollover_are_independent(tmp_path):
    cfg = policy(
        aggregate_limit_usd="0.9",
        key_limits={
            "key-a": {
                "allowance_usd": "1",
                "reserve_usd": "0",
                "cycle_anchor_at": datetime(2026, 9, 15, tzinfo=UTC),
            }
        },
    )
    path = tmp_path / "budget.db"
    h = handler(path, cfg)
    request = command()
    assert h.handle(request).admitted
    advance(h, request, "started")
    usage = ModelClaudeReviewUsage(
        call_id=request.call_id,
        api_key_ref="key-a",
        model_id=MODEL,
        observed_at=NOW,
        input_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        output_tokens=60_000,
    )
    advance(h, request, "settle", usage=usage)
    h = handler(path, cfg, now=datetime(2026, 10, 16, tzinfo=UTC))
    decision = h.handle(command())
    assert decision.reason == "aggregate_limit"
    assert decision.key_committed_usd == 0
    assert decision.aggregate_committed_usd == Decimal("0.6")
    smaller = command(
        quote=ModelReviewBudgetQuote(model_id=MODEL, blocks=(), max_tokens=20_000)
    )
    assert h.handle(smaller).admitted
    advance(h, smaller, "started")
    usage = usage.model_copy(
        update={"call_id": smaller.call_id, "output_tokens": 20_000}
    )
    advance(h, smaller, "settle", usage=usage)
    h = handler(path, cfg, now=datetime(2026, 11, 2, tzinfo=UTC))
    larger = command(
        quote=ModelReviewBudgetQuote(model_id=MODEL, blocks=(), max_tokens=90_000)
    )
    decision = h.handle(larger)
    assert decision.reason == "key_limit"
    assert decision.key_committed_usd == Decimal("0.2")
    assert decision.aggregate_committed_usd == 0


def test_failed_commit_rolls_back_before_returning_a_grant(tmp_path, monkeypatch):
    import sqlite3

    from omnimarket.nodes.node_claude_review_budget_admission_effect.handlers import (
        handler_review_budget_admission as module,
    )

    h = handler(tmp_path / "budget.db")
    request = command()
    original_connect = sqlite3.connect

    class CommitFailure(sqlite3.Connection):
        def commit(self):
            raise sqlite3.OperationalError("simulated commit failure")

    with monkeypatch.context() as patch:
        patch.setattr(
            module.sqlite3,
            "connect",
            lambda *args, **kwargs: original_connect(
                *args, **kwargs, factory=CommitFailure
            ),
        )
        with pytest.raises(sqlite3.OperationalError, match="simulated commit failure"):
            h.handle(request)
    assert h.handle(request).admitted
    assert not h.handle(command()).admitted


def test_synchronous_request_cannot_claim_batch_quote_discount(tmp_path):
    h = handler(tmp_path / "budget.db", policy(aggregate_limit_usd="0.5"))
    quote = ModelReviewBudgetQuote(
        model_id=MODEL, blocks=(), max_tokens=60_000, mode="batch"
    )
    assert h.handle(command(quote=quote)).reason == "aggregate_limit"


def test_persona_pacing_is_explicit_overlay_policy(tmp_path):
    cfg = policy(
        personas={
            "primary": {"tier": "deployment-primary", "enforce_pacing": False},
            "extra": {"tier": "deployment-extra", "enforce_pacing": True},
        }
    )
    h = handler(tmp_path / "budget.db", cfg)
    assert h.handle(command()).admitted
    quote = ModelReviewBudgetQuote(model_id=MODEL, blocks=(), max_tokens=1)
    assert h.handle(command(persona_id="extra", quote=quote)).reason == "paced_budget"
    assert h.handle(command(quote=quote)).admitted


def test_pricing_contract_has_no_deployment_model_default():
    from omnimarket.nodes.node_claude_review_budget_compute.handlers.handler_claude_review_budget import (
        load_budget_config,
    )
    from omnimarket.nodes.node_claude_review_budget_compute.models.model_claude_review_budget import (
        ModelClaudeReviewBudgetRequest,
    )

    assert load_budget_config().model_id is None
    result = HandlerClaudeReviewBudget().handle(
        ModelClaudeReviewBudgetRequest(
            as_of=NOW, prompt_tokens=1, max_tokens=1, keys=()
        )
    )
    assert result.chosen_api_key_ref is None
    assert result.worst_case_usd is None
