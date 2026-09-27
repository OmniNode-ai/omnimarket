# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19841: per-occurrence text must not mint a fingerprint per occurrence.

Measured over the 1730 rows of omninode_internal.runtime_error_fingerprints on
the .201 dev lane on 2026-09-27: 1550 sat at occurrence_count 1. 567 carried a
pydantic ``input_value=<repr>`` and 560 an ``0x...`` object address that the
producer's templatizer leaves in place, so the same error class hashed to a new
fingerprint every time it happened and the ranking could not rank it.

The templates below are the lab rows' own text, trimmed.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_projection_runtime_error_fingerprints.handlers.handler_projection_runtime_error_fingerprints import (
    HandlerProjectionRuntimeErrorFingerprints,
    normalize_message_template,
)
from omnimarket.nodes.node_projection_runtime_error_fingerprints.models import (
    ModelRuntimeErrorEventWire,
    ModelRuntimeErrorFingerprintRequest,
)

pytestmark = pytest.mark.unit

_LOGGER = "omnibase_infra.runtime.auto_wiring.handler_wiring"

_PYDANTIC_WITH_ADDRESS = (
    "Projection handler error: handler=TopicActivityProjectionWriter "
    "topic=onex.evt.omnimarket.topic-activity-sampled.v1 error_type=ValidationError "
    "error={} validation errors for ModelTopicActivitySampleEvent\n_db\n"
    "  Extra inputs are not permitted [type=extra_forbidden, "
    "input_value=<omnibase_infra.runtime.a...bject at {address}>, "
    "input_type=ProjectionDatabaseOperations]\n"
    "    For further information visit https://errors.pydantic.dev/{}/v/extra_forbidden"
)


def _fingerprint(message_template: str, *, logger_family: str = _LOGGER) -> str:
    event = ModelRuntimeErrorEventWire(
        logger_family=logger_family,
        log_level="ERROR",
        message_template=message_template,
        exception_type="ValidationError",
    )
    result = HandlerProjectionRuntimeErrorFingerprints().handle(
        ModelRuntimeErrorFingerprintRequest(event=event)
    )
    return result.row.fingerprint


@pytest.mark.parametrize(
    ("first", "second"),
    [
        pytest.param(
            _PYDANTIC_WITH_ADDRESS.replace("{address}", "0x721fae46ab10"),
            _PYDANTIC_WITH_ADDRESS.replace("{address}", "0x725ec44cbb00"),
            id="memory-address",
        ),
        pytest.param(
            "sample_id\n  Field required [type=missing, "
            "input_value={'topic': 'onex.evt.a.v1', 'rate': 3}, input_type=dict]",
            "sample_id\n  Field required [type=missing, "
            "input_value={'topic': 'onex.evt.b.v1', 'rate': [1, 2]}, input_type=dict]",
            id="pydantic-input-value",
        ),
        pytest.param(
            "Extra inputs are not permitted [type=extra_forbidden, "
            "input_value=<omnibase_infra.runtime.a...bject at 0x7",
            "Extra inputs are not permitted [type=extra_forbidden, "
            "input_value={'a': 'truncated by the producer length c",
            id="pydantic-input-value-cut-by-the-length-cap",
        ),
        pytest.param(
            "dispatch failed for envelope 0b8e5c1e-6a4f-4c3e-9a51-3f5b2d7e9c10",
            "dispatch failed for envelope 9f5752c5-1b2c-4d3e-8f40-5a6b7c8d9e0f",
            id="uuid",
        ),
        pytest.param(
            "lease expired at 2026-09-27T02:03:56.123456+00:00",
            "lease expired at 2026-09-26 18:22:36Z",
            id="timestamp",
        ),
    ],
)
def test_errors_differing_only_in_occurrence_text_share_one_fingerprint(
    first: str, second: str
) -> None:
    assert first != second
    assert _fingerprint(first) == _fingerprint(second)


@pytest.mark.parametrize(
    ("first", "second"),
    [
        pytest.param(
            _PYDANTIC_WITH_ADDRESS.replace("{address}", "0x721fae46ab10"),
            _PYDANTIC_WITH_ADDRESS.replace("{address}", "0x721fae46ab10").replace(
                "extra_forbidden,", "missing,"
            ),
            id="different-pydantic-error-type",
        ),
        pytest.param(
            "sample_id\n  Field required [type=missing, input_value={}, input_type=dict]",
            "topic\n  Field required [type=missing, input_value={}, input_type=dict]",
            id="different-field",
        ),
        pytest.param(
            "Projection handler error: handler=TopicActivityProjectionWriter",
            "Projection handler error: handler=RuntimeErrorFingerprintProjectionWriter",
            id="different-handler",
        ),
    ],
)
def test_genuinely_different_errors_keep_distinct_fingerprints(
    first: str, second: str
) -> None:
    assert _fingerprint(first) != _fingerprint(second)


def test_the_same_template_from_a_different_logger_is_a_different_error() -> None:
    template = "handler raised at 0x7f00dead"
    assert _fingerprint(template) != _fingerprint(
        template, logger_family="omnimarket.projection.api_server"
    )


def test_normalization_is_idempotent() -> None:
    once = normalize_message_template(
        _PYDANTIC_WITH_ADDRESS.replace("{address}", "0x721fae46ab10")
        + " at 2026-09-27T02:03:56Z id 0b8e5c1e-6a4f-4c3e-9a51-3f5b2d7e9c10"
    )
    assert normalize_message_template(once) == once
    assert "0x721fae46ab10" not in once
    assert "input_value={}, input_type=ProjectionDatabaseOperations" in once


def test_the_row_displays_the_text_its_fingerprint_was_hashed_from() -> None:
    template = _PYDANTIC_WITH_ADDRESS.replace("{address}", "0x721fae46ab10")
    event = ModelRuntimeErrorEventWire(
        logger_family=_LOGGER, log_level="ERROR", message_template=template
    )
    row = (
        HandlerProjectionRuntimeErrorFingerprints()
        .handle(ModelRuntimeErrorFingerprintRequest(event=event))
        .row
    )
    assert row.message_template == normalize_message_template(template)
    assert "input_value={}, input_type=" in row.message_template
    assert "0x721fae46ab10" not in row.message_template
