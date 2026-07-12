# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for the co-located RT-5 fail-closed producer effect assertions.

The invariant under test: any job whose purpose is to emit an artifact must fail
closed (raise) when it would emit zero — whether a precondition is missing or the
emit itself delivered nothing. "Ran successfully" is not completion for a
producer; "produced N>0" is.

Ticket: OMN-14470 (RT-5 mirror); sibling OMN-14467; epic OMN-13674.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# The assertion module is co-located in scripts/ (not the omnimarket package),
# so make scripts/ importable for this test.
_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from producer_effect_assertion import (  # noqa: E402
    ProducerZeroOutputError,
    assert_producer_emitted,
    require_producer_preconditions,
)

_ARTIFACT = "onex.cmd.deploy.rebuild-requested.v1"


class TestRequireProducerPreconditions:
    """require_producer_preconditions: absent precondition => zero output => RED."""

    @pytest.mark.unit
    def test_all_present_does_not_raise(self) -> None:
        require_producer_preconditions(
            artifact=_ARTIFACT,
            preconditions={
                "KAFKA_BOOTSTRAP_SERVERS": "broker:9092",
                "DEPLOY_AGENT_HMAC_SECRET": "secret",
            },
        )

    @pytest.mark.unit
    def test_empty_string_precondition_is_missing(self) -> None:
        """The exists-but-WRONG case: broker unset (empty string) must go RED."""
        with pytest.raises(ProducerZeroOutputError) as exc_info:
            require_producer_preconditions(
                artifact=_ARTIFACT,
                preconditions={"KAFKA_BOOTSTRAP_SERVERS": ""},
            )
        msg = str(exc_info.value)
        assert "KAFKA_BOOTSTRAP_SERVERS" in msg
        assert _ARTIFACT in msg
        assert "fail closed" in msg

    @pytest.mark.unit
    def test_none_precondition_is_missing(self) -> None:
        with pytest.raises(ProducerZeroOutputError, match="TOKEN"):
            require_producer_preconditions(
                artifact=_ARTIFACT,
                preconditions={"TOKEN": None},
            )

    @pytest.mark.unit
    def test_reports_every_missing_precondition(self) -> None:
        with pytest.raises(ProducerZeroOutputError) as exc_info:
            require_producer_preconditions(
                artifact=_ARTIFACT,
                preconditions={
                    "KAFKA_BOOTSTRAP_SERVERS": "",
                    "KAFKA_SASL_USERNAME": "user",
                    "KAFKA_SASL_PASSWORD": None,
                    "DEPLOY_AGENT_HMAC_SECRET": "",
                },
            )
        msg = str(exc_info.value)
        assert "KAFKA_BOOTSTRAP_SERVERS" in msg
        assert "KAFKA_SASL_PASSWORD" in msg
        assert "DEPLOY_AGENT_HMAC_SECRET" in msg
        assert "KAFKA_SASL_USERNAME" not in msg

    @pytest.mark.unit
    def test_empty_preconditions_mapping_does_not_raise(self) -> None:
        require_producer_preconditions(artifact=_ARTIFACT, preconditions={})


class TestAssertProducerEmitted:
    """assert_producer_emitted: an emit that delivered zero must go RED."""

    @pytest.mark.unit
    def test_zero_emitted_raises(self) -> None:
        with pytest.raises(ProducerZeroOutputError) as exc_info:
            assert_producer_emitted(0, artifact=_ARTIFACT)
        msg = str(exc_info.value)
        assert "emitted 0" in msg
        assert "expected at least 1" in msg
        assert _ARTIFACT in msg

    @pytest.mark.unit
    def test_negative_emitted_raises(self) -> None:
        with pytest.raises(ProducerZeroOutputError):
            assert_producer_emitted(-1, artifact=_ARTIFACT)

    @pytest.mark.unit
    def test_one_emitted_does_not_raise(self) -> None:
        assert_producer_emitted(1, artifact=_ARTIFACT)

    @pytest.mark.unit
    def test_detail_is_included_in_message(self) -> None:
        with pytest.raises(ProducerZeroOutputError, match="correlation_id=abc"):
            assert_producer_emitted(0, artifact=_ARTIFACT, detail="correlation_id=abc")


@pytest.mark.unit
def test_error_is_runtimeerror_subclass() -> None:
    assert issubclass(ProducerZeroOutputError, RuntimeError)
