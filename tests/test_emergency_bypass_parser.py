# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for HandlerEmergencyBypassParser — OMN-8497.

TDD: all 6 DoD cases must pass.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from omnimarket.nodes.node_pr_review_bot.handlers.handler_emergency_bypass_parser import (
    BypassRejectionReason,
    HandlerEmergencyBypassParser,
)
from omnimarket.nodes.node_pr_review_bot.topics import (
    TOPIC_BYPASS_ROLLED_BACK,
    TOPIC_BYPASS_USED,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_default_valkey() -> MagicMock:
    v = MagicMock()
    v.set.return_value = True  # atomic claim succeeds by default
    return v


_SENTINEL: list[str] = []  # sentinel to distinguish None from explicit []


def _make_handler(
    authorized_actors: list[str] | None = None,
    kafka_publisher: object | None = None,
    db_conn: object | None = None,
    valkey_client: object | None = None,
) -> HandlerEmergencyBypassParser:
    actors = ["jonahgabriel"] if authorized_actors is None else authorized_actors
    return HandlerEmergencyBypassParser(
        authorized_actors=actors,
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
        # SET NX returns False meaning key already exists (bypass already consumed)
        valkey = MagicMock()
        valkey.set.return_value = False

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
        valkey.set.return_value = True  # atomic claim succeeds
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
        # Valkey set(nx=True) was called to atomically claim the slot
        valkey.set.assert_called_once()
        call_kwargs = valkey.set.call_args[1]
        key = valkey.set.call_args[0][0]
        assert "77" in key
        assert "OmniNode-ai" in key
        assert "omnimarket" in key
        assert call_kwargs.get("nx") is True


# ---------------------------------------------------------------------------
# (e) Kafka audit event emitted on successful bypass
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBypassKafkaEvent:
    def test_kafka_event_emitted_on_granted_bypass(self) -> None:
        kafka = MagicMock()
        valkey = MagicMock()
        valkey.set.return_value = True
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
        assert topic == TOPIC_BYPASS_USED
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
        valkey.set.return_value = True

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
        valkey.set.return_value = True

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
        assert TOPIC_BYPASS_USED in topics
        assert TOPIC_BYPASS_ROLLED_BACK in topics

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
        valkey.set.return_value = True
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

    def test_empty_authorized_actors_rejects_all(self) -> None:
        """Empty authorized_actors = lockdown mode: all bypasses rejected."""
        handler = _make_handler(authorized_actors=[])
        result = handler.parse(
            comment_body="EMERGENCY-BYPASS: lockdown test",
            actor="jonahgabriel",
            pr_number=1,
            repo="OmniNode-ai/omnimarket",
            head_sha="qqq777",
        )
        assert result.granted is False
        assert result.rejection_reason == BypassRejectionReason.UNAUTHORIZED_ACTOR

    def test_whitespace_only_actors_are_filtered(self) -> None:
        """Actors that are empty strings after strip are treated as lockdown."""
        handler = _make_handler(authorized_actors=["", "  "])
        result = handler.parse(
            comment_body="EMERGENCY-BYPASS: test",
            actor="jonahgabriel",
            pr_number=1,
            repo="OmniNode-ai/omnimarket",
            head_sha="rrr888",
        )
        assert result.granted is False
        assert result.rejection_reason == BypassRejectionReason.UNAUTHORIZED_ACTOR


# ---------------------------------------------------------------------------
# (g) Topic namespace — must use omnimarket-namespaced topics  [Finding 1]
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBypassTopicNamespace:
    def test_bypass_used_topic_uses_omnimarket_namespace(self) -> None:
        """TOPIC_BYPASS_USED must follow onex.evt.omnimarket.* convention."""
        assert TOPIC_BYPASS_USED.startswith("onex.evt.omnimarket."), (
            f"Expected omnimarket namespace but got: {TOPIC_BYPASS_USED}"
        )

    def test_bypass_rolled_back_topic_uses_omnimarket_namespace(self) -> None:
        """TOPIC_BYPASS_ROLLED_BACK must follow onex.evt.omnimarket.* convention."""
        assert TOPIC_BYPASS_ROLLED_BACK.startswith("onex.evt.omnimarket."), (
            f"Expected omnimarket namespace but got: {TOPIC_BYPASS_ROLLED_BACK}"
        )

    def test_kafka_event_published_to_namespaced_topic(self) -> None:
        """Handler must publish to the omnimarket-namespaced topic, not review_bot.*."""
        kafka = MagicMock()
        valkey = MagicMock()
        valkey.set.return_value = True
        db_conn = MagicMock()

        handler = _make_handler(
            kafka_publisher=kafka, valkey_client=valkey, db_conn=db_conn
        )
        result = handler.parse(
            comment_body="EMERGENCY-BYPASS: namespace check",
            actor="jonahgabriel",
            pr_number=88,
            repo="OmniNode-ai/omnimarket",
            head_sha="sss999",
        )
        assert result.granted is True
        topic = kafka.publish.call_args_list[0][0][0]
        assert topic.startswith("onex.evt.omnimarket."), (
            f"Expected omnimarket namespace but got: {topic}"
        )


# ---------------------------------------------------------------------------
# (h) Concurrent bypass — atomic SET NX prevents TOCTOU race  [Finding 2]
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBypassTOCTOUAtomicClaim:
    def test_atomic_set_nx_used_instead_of_get_then_setex(self) -> None:
        """Handler must call valkey.set(..., nx=True) not get() + setex()."""
        valkey = MagicMock()
        valkey.set.return_value = True  # claim succeeded
        db_conn = MagicMock()

        handler = _make_handler(valkey_client=valkey, db_conn=db_conn)
        result = handler.parse(
            comment_body="EMERGENCY-BYPASS: atomic test",
            actor="jonahgabriel",
            pr_number=101,
            repo="OmniNode-ai/omnimarket",
            head_sha="ttt000",
        )
        assert result.granted is True
        # Must NOT call .get() or .setex()
        valkey.get.assert_not_called()
        valkey.setex.assert_not_called()
        # Must call .set() with nx=True
        valkey.set.assert_called_once()
        call_kwargs = valkey.set.call_args[1]
        assert call_kwargs.get("nx") is True
        assert "ex" in call_kwargs

    def test_atomic_set_nx_returns_false_means_already_consumed(self) -> None:
        """When SET NX returns falsy, bypass is already consumed — reject."""
        valkey = MagicMock()
        valkey.set.return_value = False  # key already exists

        handler = _make_handler(valkey_client=valkey)
        result = handler.parse(
            comment_body="EMERGENCY-BYPASS: second attempt",
            actor="jonahgabriel",
            pr_number=102,
            repo="OmniNode-ai/omnimarket",
            head_sha="uuu111",
        )
        assert result.granted is False
        assert result.rejection_reason == BypassRejectionReason.ALREADY_CONSUMED

    def test_concurrent_bypass_only_one_succeeds(self) -> None:
        """Two concurrent bypass calls: exactly one must be granted."""
        call_count = 0

        def atomic_set(key: str, value: str, *, ex: int, nx: bool) -> bool:
            nonlocal call_count
            call_count += 1
            # Only the first caller wins; subsequent callers get False
            return call_count == 1

        valkey = MagicMock()
        valkey.set.side_effect = atomic_set
        db_conn = MagicMock()
        kafka = MagicMock()

        handler = _make_handler(
            kafka_publisher=kafka, valkey_client=valkey, db_conn=db_conn
        )

        async def _run_both() -> list[bool]:
            async def _call() -> bool:
                result = handler.parse(
                    comment_body="EMERGENCY-BYPASS: concurrent attempt",
                    actor="jonahgabriel",
                    pr_number=200,
                    repo="OmniNode-ai/omnimarket",
                    head_sha="vvv222",
                )
                return result.granted

            return list(await asyncio.gather(_call(), _call()))

        results = asyncio.run(_run_both())
        granted = [r for r in results if r]
        rejected = [r for r in results if not r]
        assert len(granted) == 1, f"Expected exactly 1 granted, got {granted}"
        assert len(rejected) == 1, f"Expected exactly 1 rejected, got {rejected}"

    def test_valkey_key_deleted_on_db_failure_after_atomic_claim(self) -> None:
        """If DB write fails after atomic claim, the Valkey key must be deleted (rollback)."""
        valkey = MagicMock()
        valkey.set.return_value = True
        db_conn = MagicMock()
        db_conn.execute.side_effect = Exception("DB down")
        kafka = MagicMock()

        handler = _make_handler(
            kafka_publisher=kafka, valkey_client=valkey, db_conn=db_conn
        )
        result = handler.parse(
            comment_body="EMERGENCY-BYPASS: db failure rollback",
            actor="jonahgabriel",
            pr_number=300,
            repo="OmniNode-ai/omnimarket",
            head_sha="www333",
        )
        assert result.granted is False
        assert result.rejection_reason == BypassRejectionReason.AUDIT_FAILURE
        # Valkey key must be deleted to roll back the atomic claim
        valkey.delete.assert_called_once()
