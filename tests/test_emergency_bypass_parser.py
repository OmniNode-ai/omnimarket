# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for HandlerEmergencyBypassParser — OMN-8497.

TDD: all 6 DoD cases must pass.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from omnimarket.nodes.node_pr_review_bot.handlers.handler_emergency_bypass_parser import (
    BypassRejectionReason,
    HandlerEmergencyBypassParser,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_default_valkey() -> MagicMock:
    v = MagicMock()
    v.get.return_value = None  # not consumed by default
    return v


def _make_handler(
    authorized_actors: list[str] | None = None,
    kafka_publisher: object | None = None,
    db_conn: object | None = None,
    valkey_client: object | None = None,
) -> HandlerEmergencyBypassParser:
    return HandlerEmergencyBypassParser(
        authorized_actors=authorized_actors or ["jonahgabriel"],
        kafka_publisher=kafka_publisher or MagicMock(),
        db_conn=db_conn or MagicMock(),
        valkey_client=valkey_client or _make_default_valkey(),
    )


# ---------------------------------------------------------------------------
# (a) Valid comment + authorized actor → bypass granted
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBypassGranted:
    def test_valid_comment_authorized_actor_grants_bypass(self) -> None:
        handler = _make_handler()
        result = handler.parse(
            comment_body="EMERGENCY-BYPASS: prod is on fire, pushing fix",
            actor="jonahgabriel",
            pr_number=42,
            repo="OmniNode-ai/omnimarket",
            head_sha="abc123",
        )
        assert result.granted is True
        assert result.reason == "prod is on fire, pushing fix"
        assert result.actor == "jonahgabriel"
        assert result.rejection_reason is None

    def test_bypass_with_multi_word_reason_granted(self) -> None:
        handler = _make_handler()
        result = handler.parse(
            comment_body="EMERGENCY-BYPASS: security patch must ship now",
            actor="jonahgabriel",
            pr_number=1,
            repo="OmniNode-ai/omnimarket",
            head_sha="def456",
        )
        assert result.granted is True
        assert result.reason == "security patch must ship now"


# ---------------------------------------------------------------------------
# (b) Valid format + unauthorized actor → rejected
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBypassUnauthorizedActor:
    def test_unauthorized_actor_is_rejected(self) -> None:
        handler = _make_handler()
        result = handler.parse(
            comment_body="EMERGENCY-BYPASS: hacking the bypass",
            actor="some-random-user",
            pr_number=10,
            repo="OmniNode-ai/omnimarket",
            head_sha="aaa111",
        )
        assert result.granted is False
        assert result.rejection_reason == BypassRejectionReason.UNAUTHORIZED_ACTOR

    def test_bot_actor_is_rejected(self) -> None:
        handler = _make_handler()
        result = handler.parse(
            comment_body="EMERGENCY-BYPASS: automated attempt",
            actor="onexbot[bot]",
            pr_number=10,
            repo="OmniNode-ai/omnimarket",
            head_sha="bbb222",
        )
        assert result.granted is False
        assert result.rejection_reason == BypassRejectionReason.UNAUTHORIZED_ACTOR


# ---------------------------------------------------------------------------
# (c) Invalid format → rejected
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBypassMalformedComment:
    def test_lowercase_prefix_rejected(self) -> None:
        handler = _make_handler()
        result = handler.parse(
            comment_body="emergency-bypass: lowercase not allowed",
            actor="jonahgabriel",
            pr_number=5,
            repo="OmniNode-ai/omnimarket",
            head_sha="ccc333",
        )
        assert result.granted is False
        assert result.rejection_reason == BypassRejectionReason.MALFORMED_COMMENT

    def test_missing_colon_rejected(self) -> None:
        handler = _make_handler()
        result = handler.parse(
            comment_body="EMERGENCY-BYPASS no colon",
            actor="jonahgabriel",
            pr_number=5,
            repo="OmniNode-ai/omnimarket",
            head_sha="ddd444",
        )
        assert result.granted is False
        assert result.rejection_reason == BypassRejectionReason.MALFORMED_COMMENT

    def test_empty_reason_rejected(self) -> None:
        handler = _make_handler()
        result = handler.parse(
            comment_body="EMERGENCY-BYPASS: ",
            actor="jonahgabriel",
            pr_number=5,
            repo="OmniNode-ai/omnimarket",
            head_sha="eee555",
        )
        assert result.granted is False
        assert result.rejection_reason == BypassRejectionReason.MALFORMED_COMMENT

    def test_whitespace_only_reason_rejected(self) -> None:
        handler = _make_handler()
        result = handler.parse(
            comment_body="EMERGENCY-BYPASS:    ",
            actor="jonahgabriel",
            pr_number=5,
            repo="OmniNode-ai/omnimarket",
            head_sha="fff666",
        )
        assert result.granted is False
        assert result.rejection_reason == BypassRejectionReason.MALFORMED_COMMENT

    def test_comment_not_starting_with_prefix_rejected(self) -> None:
        handler = _make_handler()
        result = handler.parse(
            comment_body="Please EMERGENCY-BYPASS: this should fail",
            actor="jonahgabriel",
            pr_number=5,
            repo="OmniNode-ai/omnimarket",
            head_sha="ggg777",
        )
        assert result.granted is False
        assert result.rejection_reason == BypassRejectionReason.MALFORMED_COMMENT


# ---------------------------------------------------------------------------
# (d) One-time consumption — second attempt on same PR rejected
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBypassOneTimeConsumption:
    def test_second_bypass_on_same_pr_is_rejected(self) -> None:
        # Valkey returns a truthy value (b"1") meaning bypass already consumed
        valkey = MagicMock()
        valkey.get.return_value = b"1"

        handler = _make_handler(valkey_client=valkey)
        result = handler.parse(
            comment_body="EMERGENCY-BYPASS: trying again",
            actor="jonahgabriel",
            pr_number=99,
            repo="OmniNode-ai/omnimarket",
            head_sha="hhh888",
        )
        assert result.granted is False
        assert result.rejection_reason == BypassRejectionReason.ALREADY_CONSUMED

    def test_first_bypass_sets_valkey_key(self) -> None:
        valkey = MagicMock()
        valkey.get.return_value = None  # not yet consumed
        db_conn = MagicMock()

        handler = _make_handler(valkey_client=valkey, db_conn=db_conn)
        result = handler.parse(
            comment_body="EMERGENCY-BYPASS: first and only",
            actor="jonahgabriel",
            pr_number=77,
            repo="OmniNode-ai/omnimarket",
            head_sha="iii999",
        )
        assert result.granted is True
        # Valkey setex was called to mark as consumed
        valkey.setex.assert_called_once()
        call_args = valkey.setex.call_args
        key = call_args[0][0]
        assert "77" in key
        assert "OmniNode-ai" in key or "omnimarket" in key


# ---------------------------------------------------------------------------
# (e) Kafka audit event emitted on successful bypass
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBypassKafkaEvent:
    def test_kafka_event_emitted_on_granted_bypass(self) -> None:
        kafka = MagicMock()
        valkey = MagicMock()
        valkey.get.return_value = None
        db_conn = MagicMock()

        handler = _make_handler(
            kafka_publisher=kafka, valkey_client=valkey, db_conn=db_conn
        )
        result = handler.parse(
            comment_body="EMERGENCY-BYPASS: infra outage",
            actor="jonahgabriel",
            pr_number=55,
            repo="OmniNode-ai/omnimarket",
            head_sha="jjj000",
        )
        assert result.granted is True
        kafka.publish.assert_called_once()
        topic, payload = kafka.publish.call_args[0]
        assert topic == "onex.evt.review_bot.bypass_used.v1"
        assert payload["actor"] == "jonahgabriel"
        assert payload["pr_number"] == 55
        assert payload["repo"] == "OmniNode-ai/omnimarket"
        assert payload["reason"] == "infra outage"
        assert "timestamp" in payload
        assert payload["sha"] == "jjj000"

    def test_kafka_event_not_emitted_on_rejected_bypass(self) -> None:
        kafka = MagicMock()
        handler = _make_handler(kafka_publisher=kafka)
        handler.parse(
            comment_body="EMERGENCY-BYPASS: unauthorized attempt",
            actor="intruder",
            pr_number=55,
            repo="OmniNode-ai/omnimarket",
            head_sha="kkk111",
        )
        kafka.publish.assert_not_called()


# ---------------------------------------------------------------------------
# (f) DB audit row written on successful bypass
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBypassDbAudit:
    def test_db_audit_row_written_on_granted_bypass(self) -> None:
        db_conn = MagicMock()
        valkey = MagicMock()
        valkey.get.return_value = None

        handler = _make_handler(db_conn=db_conn, valkey_client=valkey)
        result = handler.parse(
            comment_body="EMERGENCY-BYPASS: critical deploy needed",
            actor="jonahgabriel",
            pr_number=33,
            repo="OmniNode-ai/omnimarket",
            head_sha="lll222",
        )
        assert result.granted is True
        db_conn.execute.assert_called_once()
        sql, params = db_conn.execute.call_args[0]
        assert "review_bot_bypass_log" in sql
        assert params["actor"] == "jonahgabriel"
        assert params["pr_url"] is not None
        assert params["reason"] == "critical deploy needed"

    def test_db_failure_emits_compensating_kafka_event(self) -> None:
        kafka = MagicMock()
        db_conn = MagicMock()
        db_conn.execute.side_effect = Exception("DB write failed")
        valkey = MagicMock()
        valkey.get.return_value = None

        handler = _make_handler(
            kafka_publisher=kafka, db_conn=db_conn, valkey_client=valkey
        )
        result = handler.parse(
            comment_body="EMERGENCY-BYPASS: rollback test",
            actor="jonahgabriel",
            pr_number=22,
            repo="OmniNode-ai/omnimarket",
            head_sha="mmm333",
        )
        # The parse returns failure when DB write fails
        assert result.granted is False
        assert result.rejection_reason == BypassRejectionReason.AUDIT_FAILURE
        # Compensating Kafka event must have been emitted
        assert kafka.publish.call_count == 2
        topics = [call[0][0] for call in kafka.publish.call_args_list]
        assert "onex.evt.review_bot.bypass_used.v1" in topics
        assert "onex.evt.review_bot.bypass_rolled_back.v1" in topics

    def test_db_audit_row_not_written_on_rejected_bypass(self) -> None:
        db_conn = MagicMock()
        handler = _make_handler(db_conn=db_conn)
        handler.parse(
            comment_body="EMERGENCY-BYPASS: unauthorized",
            actor="hacker",
            pr_number=33,
            repo="OmniNode-ai/omnimarket",
            head_sha="nnn444",
        )
        db_conn.execute.assert_not_called()


# ---------------------------------------------------------------------------
# Contract-driven authorized actor list
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBypassContractDrivenConfig:
    def test_custom_authorized_actor_list_from_config(self) -> None:
        """Authorized actor list must come from config, not be hardcoded."""
        valkey = MagicMock()
        valkey.get.return_value = None
        db_conn = MagicMock()

        handler = _make_handler(
            authorized_actors=["alice", "bob"],
            valkey_client=valkey,
            db_conn=db_conn,
        )
        # jonahgabriel is NOT in this custom list
        result_jonah = handler.parse(
            comment_body="EMERGENCY-BYPASS: test",
            actor="jonahgabriel",
            pr_number=1,
            repo="OmniNode-ai/omnimarket",
            head_sha="ooo555",
        )
        assert result_jonah.granted is False

        # alice IS in the custom list
        result_alice = handler.parse(
            comment_body="EMERGENCY-BYPASS: test",
            actor="alice",
            pr_number=2,
            repo="OmniNode-ai/omnimarket",
            head_sha="ppp666",
        )
        assert result_alice.granted is True
