# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The paid-rung burn reading through the delegation completion-rate node's CLI surface (OMN-23756)."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from omnimarket.models.delegation.model_paid_rung_burn import (
    ModelPaidRungCallRecord,
    ModelProviderCreditsReading,
)
from omnimarket.models.delegation.model_paid_rungs_overlay import (
    ModelPaidRungsOverlay,
)
from omnimarket.nodes.node_delegation_completion_rate_effect.adapters.paid_rung_calls_source import (
    InMemoryPaidRungCallSource,
    SshPostgresPaidRungCallSource,
    parse_calls,
)
from omnimarket.nodes.node_delegation_completion_rate_effect.handlers.handler_paid_rung_burn_read import (
    HandlerPaidRungBurnRead,
)
from tests.nodes.node_delegation_completion_rate_effect.rate_run_support import (
    FakeRunner,
    effect_overlay,
)

pytestmark = pytest.mark.unit

SWITCH_ON = datetime(2026, 11, 11, 14, 0, tzinfo=UTC)
NOW = SWITCH_ON + timedelta(hours=4)
BASELINE = Decimal("73.50")
SLUG = "vendor/cheap-a"


def _paid(*, enabled: bool) -> ModelPaidRungsOverlay:
    doc: dict[str, object] = {
        "enabled": enabled,
        "spend_cap_usd": "26",
        "after_tier": "cheap_cloud",
        "spend_source": {
            "credits_url": "https://provider.example/api/v1/credits",
            "secret_ref": "llm.hosted.api_key",
        },
        "tiers": [{"name": "paid_cheap"}],
        "backends": [
            {
                "backend_id": "paid-cheap-a",
                "provider": "hosted",
                "endpoint_url": "https://provider.example/api/v1/chat/completions",
                "model_name": SLUG,
                "secret_ref": "llm.hosted.api_key",
                "tier": "paid_cheap",
                "timeout_ms": 300000,
                "max_tokens": 8192,
                "explicit_pin_only": False,
                "surface": "internal",
                "tenant_scope": "house",
                "capabilities": ["code_generation"],
            }
        ],
        "rungs": [
            {
                "backend_id": "paid-cheap-a",
                "slug": SLUG,
                "tier": "paid_cheap",
                "price_in_usd_per_mtok": "0.05",
                "price_out_usd_per_mtok": "0.50",
                "context_length": 262144,
                "task_classes": ["code_generation"],
                "catalog_read_at": "2026-11-11T13:26:42Z",
            }
        ],
    }
    if enabled:
        doc["enabled_at"] = SWITCH_ON.isoformat()
        doc["usage_baseline_usd"] = str(BASELINE)
    return ModelPaidRungsOverlay.model_validate(doc)


def _credits(_source: object) -> ModelProviderCreditsReading:
    return ModelProviderCreditsReading(
        total_credits_usd=Decimal(100),
        total_usage_usd=BASELINE + Decimal(2),
        read_at=NOW,
    )


def _call(hours: float, tin: int, tout: int) -> ModelPaidRungCallRecord:
    return ModelPaidRungCallRecord(
        backend_id="paid-cheap-a",
        model_id=SLUG,
        tokens_in=tin,
        tokens_out=tout,
        booked_cost_usd=Decimal("0.0001"),
        completed_at=SWITCH_ON + timedelta(hours=hours),
    )


def _handler(paid, calls) -> HandlerPaidRungBurnRead:
    return HandlerPaidRungBurnRead(
        paid=paid, credits_reader=_credits, calls=calls, clock=lambda: NOW
    )


def test_switched_on_the_reading_has_balance_rate_projections_and_per_model_counts() -> (
    None
):
    calls = InMemoryPaidRungCallSource([_call(1, 1000, 200), _call(2, 500, 100)])
    result = _handler(_paid(enabled=True), calls).handle()
    assert result.status == "read"
    assert result.per_model_unread_reason is None
    reading = result.reading
    assert reading is not None
    assert reading.balance_usd == Decimal("24.50")
    assert reading.spent_since_switch_on_usd == Decimal("2.00")
    assert reading.usd_per_hour == Decimal("0.5")
    assert reading.hours_until_balance_gone == Decimal("49")
    assert reading.hours_until_cap_reached == Decimal("48")
    (model,) = reading.per_model
    assert (model.calls, model.tokens_in, model.tokens_out) == (2, 1500, 300)


def test_switched_off_reads_the_balance_and_names_per_model_unread() -> None:
    result = _handler(_paid(enabled=False), InMemoryPaidRungCallSource()).handle()
    assert result.status == "read"
    assert result.reading is not None
    assert result.reading.balance_usd == Decimal("24.50")
    assert result.reading.spent_since_switch_on_usd is None
    assert result.reading.usd_per_hour is None
    assert result.per_model_unread_reason == "switch-off-no-calls-since-switch-on"


def test_unreadable_calls_leave_the_reading_and_say_per_model_is_unread() -> None:
    """Failing control: a reading that kept per-model rows for calls it could not read would claim zero usage."""
    calls = InMemoryPaidRungCallSource(error="calls-read-timeout")
    result = _handler(_paid(enabled=True), calls).handle()
    assert result.status == "read"
    assert result.per_model_unread_reason == "calls-read-timeout"
    assert result.reading is not None
    assert result.reading.per_model == ()
    assert result.reading.spent_since_switch_on_usd == Decimal("2.00")


def test_unreadable_credits_are_a_failed_run_with_no_reading() -> None:
    def broken(_source: object) -> ModelProviderCreditsReading:
        raise ConnectionError("down")

    result = HandlerPaidRungBurnRead(
        paid=_paid(enabled=True),
        credits_reader=broken,
        calls=InMemoryPaidRungCallSource(),
        clock=lambda: NOW,
    ).handle()
    assert result.status == "failed"
    assert result.reading is None
    assert result.failure == "credits-unreadable-ConnectionError"


def test_the_ssh_source_reads_only_the_paid_slugs_since_switch_on_in_a_read_only_transaction() -> (
    None
):
    line = json.dumps(
        {
            "model": SLUG,
            "backend": None,
            "tin": 10,
            "tout": 5,
            "cost": "0.0002",
            "t": "2026-11-11T15:00:00.000000+00:00",
        }
    )
    runner = FakeRunner(line + "\n")
    source = SshPostgresPaidRungCallSource(
        effect_overlay().events_source,
        {"TEST_RUNTIME_SSH_HOST": "user@host.example"},
        runner,
    )
    read = source.read(SWITCH_ON, (SLUG,))
    assert read.error is None
    (call,) = read.calls
    assert (call.backend_id, call.tokens_in, call.booked_cost_usd) == (
        "not-recorded",
        10,
        Decimal("0.0002"),
    )
    ((_, script, _),) = runner.calls
    assert "BEGIN READ ONLY;" in script
    assert f"model_name IN ('{SLUG}')" in script
    assert "2026-11-11T14:00:00.000000+00:00" in script
    for word in ("INSERT", "UPDATE", "DELETE", "DROP", "TRUNCATE", "ALTER"):
        assert word not in script.upper()


def test_a_slug_that_could_carry_sql_is_refused_before_any_ssh() -> None:
    runner = FakeRunner("")
    source = SshPostgresPaidRungCallSource(
        effect_overlay().events_source, {"TEST_RUNTIME_SSH_HOST": "h"}, runner
    )
    read = source.read(SWITCH_ON, ("x'); DROP TABLE delegation_events; --",))
    assert read.error is not None
    assert read.error.startswith("calls-query-refused")
    assert runner.calls == []


def test_the_ssh_source_names_each_way_it_can_fail() -> None:
    env = {"TEST_RUNTIME_SSH_HOST": "h"}
    overlay = effect_overlay().events_source
    assert (
        SshPostgresPaidRungCallSource(overlay, {}, FakeRunner(""))
        .read(SWITCH_ON, (SLUG,))
        .error
        == "ssh-host-variable-unset"
    )
    assert (
        SshPostgresPaidRungCallSource(
            overlay, env, FakeRunner("", raises=subprocess.TimeoutExpired("ssh", 1))
        )
        .read(SWITCH_ON, (SLUG,))
        .error
        == "calls-read-timeout"
    )
    assert (
        SshPostgresPaidRungCallSource(overlay, env, FakeRunner("", returncode=255))
        .read(SWITCH_ON, (SLUG,))
        .error
        == "calls-read-exit-255"
    )
    assert parse_calls("not json\n").error == "calls-answer-malformed"
