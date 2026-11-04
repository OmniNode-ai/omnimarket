# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22346: the runtime's own /health reply carries every field a caller reads.

After the onex-api cutover the runtime's ``GET /health`` is the one liveness
endpoint. The one-runtime plan (T18e liveness callers) lists every caller and
probe of onex-api's ``GET /health`` and what each reads. Every caller reads the
HTTP status or prints the body; the single body-field reader selects ``status``.
This test pins that statement against the runtime's real health handler and its
declared reply model, so a caller list that names a field the runtime lacks, or
a runtime change that drops ``status``, fails here.

The caller list is spelled in this file and not read from the plan, so the test
runs on every runner without another clone. Update ``_LISTED_BODY_FIELDS`` in
the same change that adds a body-field reader to the plan's list.

Failure modes each test is written against:

* F1 the reply lacks a field that a listed caller reads (the check passes
  vacuously, or reads a stale copy of the model);
* F2 the reply model declares the field with a default, so the value is
  hard-coded and not supplied by the runtime;
* F3 the failure reply (health check raised) drops the field, so a caller that
  reads it sees nothing exactly when the runtime is down;
* F4 the check cannot fail: a field that onex-api answered and the runtime does
  not (``ok``, ``service``, ``time``) is accepted as present.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import make_mocked_request
from omnibase_infra.runtime.models import ModelHealthCheckResponse
from omnibase_infra.services.health_checker import ServiceHealth

# Body fields that a listed caller of GET /health reads, by caller, from the plan's
# "T18e liveness callers" list. Every other caller there reads the HTTP status only,
# or prints the body and selects no field.
_LISTED_BODY_FIELDS: dict[str, frozenset[str]] = {
    "API key auth test script, health step": frozenset({"status"}),
}

# What onex-api answered on GET /health and the runtime does not (masked by the
# plan's named rule liveness-body-differs-by-design). No listed caller reads these.
_ONEX_API_ONLY_FIELDS: frozenset[str] = frozenset({"ok", "service", "time"})

_VERSION = "9.9.9"


def _absent_fields(
    listed: dict[str, frozenset[str]], reply: dict[str, object]
) -> list[str]:
    """Return ``caller: field`` for each listed field the reply does not carry."""
    return sorted(
        f"{caller}: {field}"
        for caller, fields in listed.items()
        for field in sorted(fields)
        if field not in reply
    )


def _served_reply(*, health_check: AsyncMock) -> tuple[int, dict[str, object]]:
    """Drive the runtime's real ``GET /health`` handler and parse its JSON body."""
    runtime = MagicMock()
    runtime.health_check = health_check
    service = ServiceHealth(runtime=runtime, port=0, version=_VERSION)
    response = asyncio.run(
        service._handle_health(make_mocked_request("GET", "/health"))
    )
    assert response.text is not None
    return response.status, json.loads(response.text)


@pytest.mark.unit
def test_served_reply_carries_every_listed_caller_field() -> None:
    status, reply = _served_reply(
        health_check=AsyncMock(
            return_value={"healthy": True, "is_running": True, "handlers": {}}
        )
    )
    assert status == 200
    assert _absent_fields(_LISTED_BODY_FIELDS, reply) == []
    assert reply["status"] == "healthy"
    assert reply["version"] == _VERSION


@pytest.mark.unit
def test_failure_reply_carries_every_listed_caller_field() -> None:
    status, reply = _served_reply(
        health_check=AsyncMock(side_effect=RuntimeError("probe failure"))
    )
    assert status == 503
    assert _absent_fields(_LISTED_BODY_FIELDS, reply) == []
    assert reply["status"] == "unhealthy"


@pytest.mark.unit
def test_listed_fields_are_declared_without_a_hard_coded_value() -> None:
    declared = ModelHealthCheckResponse.model_fields
    for caller, fields in _LISTED_BODY_FIELDS.items():
        for field in fields:
            assert field in declared, f"{caller}: {field} is not declared"
            assert declared[field].is_required(), (
                f"{caller}: {field} has a default, so its value is hard-coded"
            )


@pytest.mark.unit
def test_check_fails_for_a_field_the_runtime_lacks() -> None:
    """Positive control: a listed field the reply lacks is reported, not accepted."""
    _status, reply = _served_reply(
        health_check=AsyncMock(return_value={"healthy": True})
    )
    for missing in sorted(_ONEX_API_ONLY_FIELDS):
        assert missing not in reply
        listed = {"hypothetical caller": frozenset({missing})}
        assert _absent_fields(listed, reply) == [f"hypothetical caller: {missing}"]
    assert "ok" not in ModelHealthCheckResponse.model_fields
    assert all(
        not (fields & _ONEX_API_ONLY_FIELDS) for fields in _LISTED_BODY_FIELDS.values()
    )
