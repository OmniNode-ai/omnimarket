# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Red-first tests for the tenant key pool expiry alert compute (OMN-20993).

The handler is a pure function of a tenant's credential rows and a clock: no
catalog read, no clock read, no secret. It returns at most one alert per call,
naming every live key that expires inside the warning window.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_key_pool_expiry_alert_compute.handlers.handler_key_pool_expiry_alert import (
    HandlerKeyPoolExpiryAlert,
)
from omnimarket.nodes.node_key_pool_expiry_alert_compute.models import (
    ModelKeyPoolCredentialRow,
    ModelKeyPoolExpiryAlertRequest,
)
from omnimarket.nodes.node_slack_publish_effect.models.model_slack_publish import (
    ModelSlackPublish,
)

NOW = datetime(2026, 12, 1, 12, 0, tzinfo=UTC)
TENANT = "tenant-acme"
CORRELATION_ID = UUID("00000000-0000-4000-8000-000000000093")


def _row(
    ref: str,
    *,
    name: str | None = None,
    expires_in_days: float | None = None,
    revoked: bool = False,
    tenant: str = TENANT,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "api_key_ref": ref,
        "tenant_id": tenant,
        "name": name or f"name-{ref}",
        "provider": "anthropic",
        "revoked_at": NOW - timedelta(days=1) if revoked else None,
        "expires_at": (
            None if expires_in_days is None else NOW + timedelta(days=expires_in_days)
        ),
        **extra,
    }


def _request(
    rows: list[dict[str, Any]], **fields: Any
) -> ModelKeyPoolExpiryAlertRequest:
    return ModelKeyPoolExpiryAlertRequest.model_validate(
        {
            "tenant_id": TENANT,
            "credentials": rows,
            "now": NOW,
            "channel": "C0KEYPOOL",
            "correlation_id": CORRELATION_ID,
            **fields,
        }
    )


def test_key_pool_expiry_alert_names_every_key_in_one_alert() -> None:
    rows = [
        _row("ref-a", name="alpha", expires_in_days=3),
        _row("ref-b", name="bravo", expires_in_days=29.5),
        _row("ref-c", name="charlie", expires_in_days=90),
    ]
    result = HandlerKeyPoolExpiryAlert().handle(_request(rows))

    assert result.alert is not None
    assert [k.api_key_ref for k in result.alert.expiring] == ["ref-a", "ref-b"]
    assert result.alert.window_days == 30
    text = result.alert.slack_command.text
    for needle in ("alpha", "ref-a", "bravo", "ref-b", TENANT):
        assert needle in text
    assert "charlie" not in text
    assert "ref-c" not in text


def test_key_pool_expiry_alert_is_none_when_no_key_is_in_the_window() -> None:
    rows = [_row("ref-a", expires_in_days=31), _row("ref-b", expires_in_days=400)]
    assert HandlerKeyPoolExpiryAlert().handle(_request(rows)).alert is None


def test_key_pool_expiry_alert_is_none_for_an_empty_pool() -> None:
    assert HandlerKeyPoolExpiryAlert().handle(_request([])).alert is None


def test_key_pool_expiry_alert_window_is_configurable_and_inclusive() -> None:
    rows = [_row("ref-a", expires_in_days=7), _row("ref-b", expires_in_days=8)]
    result = HandlerKeyPoolExpiryAlert().handle(_request(rows, warning_window_days=7))
    assert result.alert is not None
    assert [k.api_key_ref for k in result.alert.expiring] == ["ref-a"]
    assert result.alert.window_days == 7


def test_key_pool_expiry_alert_orders_by_expiry_then_ref() -> None:
    rows = [
        _row("ref-z", expires_in_days=5),
        _row("ref-b", expires_in_days=9),
        _row("ref-a", expires_in_days=9),
    ]
    result = HandlerKeyPoolExpiryAlert().handle(_request(rows))
    assert result.alert is not None
    assert [k.api_key_ref for k in result.alert.expiring] == ["ref-z", "ref-a", "ref-b"]


def test_key_pool_expiry_alert_slack_command_is_accepted_by_the_publish_node() -> None:
    rows = [_row("ref-a", expires_in_days=3)]
    result = HandlerKeyPoolExpiryAlert().handle(_request(rows))
    assert result.alert is not None
    command = ModelSlackPublish.model_validate(
        result.alert.slack_command.model_dump(mode="json")
    )
    assert command.channel == "C0KEYPOOL"
    assert command.correlation_id == CORRELATION_ID


def test_key_pool_expiry_alert_idempotency_key_is_stable_per_set_and_changes_with_it() -> (
    None
):
    handler = HandlerKeyPoolExpiryAlert()
    first = handler.handle(_request([_row("ref-a", expires_in_days=3)])).alert
    again = handler.handle(
        _request([_row("ref-a", expires_in_days=3)], now=NOW + timedelta(hours=6))
    ).alert
    grown = handler.handle(
        _request([_row("ref-a", expires_in_days=3), _row("ref-b", expires_in_days=4)])
    ).alert
    assert first is not None
    assert again is not None
    assert grown is not None
    assert first.slack_command.idempotency_key == again.slack_command.idempotency_key
    assert first.slack_command.idempotency_key != grown.slack_command.idempotency_key


def test_key_pool_expiry_alert_excludes_revoked_keys() -> None:
    rows = [
        _row("ref-live", expires_in_days=3),
        _row("ref-revoked", expires_in_days=3, revoked=True),
    ]
    result = HandlerKeyPoolExpiryAlert().handle(_request(rows))
    assert result.alert is not None
    assert [k.api_key_ref for k in result.alert.expiring] == ["ref-live"]
    assert "ref-revoked" not in result.alert.slack_command.text


def test_key_pool_expiry_alert_excludes_already_expired_keys() -> None:
    rows = [
        _row("ref-live", expires_in_days=3),
        _row("ref-lapsed", expires_in_days=-2),
        _row("ref-lapsing-now", expires_in_days=0),
    ]
    result = HandlerKeyPoolExpiryAlert().handle(_request(rows))
    assert result.alert is not None
    assert [k.api_key_ref for k in result.alert.expiring] == ["ref-live"]
    assert "ref-lapsed" not in result.alert.slack_command.text


def test_key_pool_expiry_alert_excludes_means_no_alert_when_only_dead_keys_remain() -> (
    None
):
    rows = [
        _row("ref-revoked", expires_in_days=3, revoked=True),
        _row("ref-lapsed", expires_in_days=-2),
    ]
    assert HandlerKeyPoolExpiryAlert().handle(_request(rows)).alert is None


def test_key_pool_expiry_alert_excludes_a_revoked_undated_key_from_the_undated_list() -> (
    None
):
    rows = [
        _row("ref-live", expires_in_days=3),
        _row("ref-gone", revoked=True),
    ]
    result = HandlerKeyPoolExpiryAlert().handle(_request(rows))
    assert result.alert is not None
    assert result.alert.undated == ()


def test_key_pool_expiry_alert_excludes_reports_an_undated_key_once() -> None:
    rows = [
        _row("ref-live", expires_in_days=3),
        _row("ref-open", name="open-ended"),
        _row("ref-open", name="open-ended"),
    ]
    result = HandlerKeyPoolExpiryAlert().handle(_request(rows))
    assert result.alert is not None
    assert [k.api_key_ref for k in result.alert.undated] == ["ref-open"]
    assert result.alert.slack_command.text.count("ref-open") == 1
    assert "undated" in result.alert.slack_command.text.lower()


def test_key_pool_expiry_alert_excludes_does_not_alert_on_undated_keys_alone() -> None:
    result = HandlerKeyPoolExpiryAlert().handle(_request([_row("ref-open")]))
    assert result.alert is None


def test_key_pool_expiry_alert_refuses_a_row_from_another_tenant() -> None:
    with pytest.raises(ValidationError):
        _request([_row("ref-a", expires_in_days=3, tenant="tenant-other")])


def test_key_pool_expiry_alert_requires_a_timezone_aware_clock() -> None:
    with pytest.raises(ValidationError):
        _request([], now=datetime(2026, 12, 1, 12, 0))


def test_key_pool_expiry_alert_requires_a_positive_window() -> None:
    with pytest.raises(ValidationError):
        _request([], warning_window_days=0)


def test_key_pool_expiry_alert_no_secret_in_the_text() -> None:
    secret_value = "sk-ant-api03-SECRETVALUE0123456789"
    fingerprint = "fp-9f86d081884c7d659a2feaa0c55ad015"
    rows = [
        _row(
            "ref-a",
            name="alpha",
            expires_in_days=3,
            fingerprint=fingerprint,
            api_key=secret_value,
            value=secret_value,
        ),
        _row("ref-open", name="open-ended", fingerprint=fingerprint + "2"),
    ]
    result = HandlerKeyPoolExpiryAlert().handle(_request(rows))
    assert result.alert is not None

    serialized = result.model_dump_json()
    for forbidden in (secret_value, "SECRETVALUE", fingerprint, "fp-9f86"):
        assert forbidden not in result.alert.slack_command.text
        assert forbidden not in serialized


def test_key_pool_expiry_alert_no_secret_field_exists_on_the_row_model() -> None:
    declared = set(ModelKeyPoolCredentialRow.model_fields)
    assert declared.isdisjoint({"fingerprint", "api_key", "value", "secret", "key"})
