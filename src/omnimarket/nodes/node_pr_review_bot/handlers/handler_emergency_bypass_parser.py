# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerEmergencyBypassParser — OMN-8497.

Parses PR comments for the EMERGENCY-BYPASS: <reason> format.
- Only actors in authorized_actors list are accepted (contract-driven, not hardcoded).
- One-time per PR: consumed flag stored in Valkey with key
  review_bot:bypass:{owner}:{repo}:{pr_number} and TTL of 3600 s.
- On valid bypass:
  1. Emits Kafka event onex.evt.review_bot.bypass_used.v1
  2. Writes audit row to review_bot_bypass_log
  3. If DB write fails, emits compensating event onex.evt.review_bot.bypass_rolled_back.v1
- Never triggers thread resolutions — only records a valid bypass signal.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import uuid4

from omnimarket.nodes.node_pr_review_bot.topics import (
    TOPIC_BYPASS_ROLLED_BACK,
    TOPIC_BYPASS_USED,
)

logger = logging.getLogger(__name__)

_BYPASS_PREFIX = "EMERGENCY-BYPASS:"
_VALKEY_TTL_SECONDS = 3600

_INSERT_AUDIT_SQL = """
INSERT INTO review_bot_bypass_log
    (audit_id, pr_url, actor, reason, bypass_timestamp, kafka_event_id)
VALUES
    (:audit_id, :pr_url, :actor, :reason, :bypass_timestamp, :kafka_event_id)
"""


# ---------------------------------------------------------------------------
# Protocols for injected dependencies
# ---------------------------------------------------------------------------


class ProtocolKafkaPublisher(Protocol):
    def publish(self, topic: str, payload: dict[str, Any]) -> None: ...


class ProtocolDbConn(Protocol):
    def execute(self, sql: str, params: dict[str, Any]) -> None: ...


class ProtocolValkeyClient(Protocol):
    def get(self, key: str) -> bytes | None: ...
    def setex(self, key: str, ttl: int, value: str) -> None: ...


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


class BypassRejectionReason(StrEnum):
    UNAUTHORIZED_ACTOR = "unauthorized_actor"
    MALFORMED_COMMENT = "malformed_comment"
    ALREADY_CONSUMED = "already_consumed"
    AUDIT_FAILURE = "audit_failure"


@dataclass(frozen=True)
class BypassParseResult:
    granted: bool
    actor: str
    pr_number: int
    repo: str
    reason: str | None = None
    audit_id: str | None = None
    rejection_reason: BypassRejectionReason | None = None


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class HandlerEmergencyBypassParser:
    """Parses EMERGENCY-BYPASS comments and records the bypass signal.

    authorized_actors: list of GitHub handles allowed to trigger bypass.
                       Read from runtime config key
                       review_bot.emergency_bypass_actor — NOT hardcoded.
    """

    def __init__(
        self,
        authorized_actors: list[str],
        kafka_publisher: ProtocolKafkaPublisher,
        db_conn: ProtocolDbConn,
        valkey_client: ProtocolValkeyClient,
    ) -> None:
        # Filter empty strings to support lockdown mode (config set to "")
        self._authorized = {a for a in authorized_actors if a}
        if not self._authorized:
            logger.warning(
                "EmergencyBypassParser: no authorized actors configured — "
                "all bypass attempts will be rejected (lockdown mode)"
            )
        self._kafka = kafka_publisher
        self._db = db_conn
        self._valkey = valkey_client

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def parse(
        self,
        comment_body: str,
        actor: str,
        pr_number: int,
        repo: str,
        head_sha: str,
    ) -> BypassParseResult:
        """Parse a PR comment for an emergency bypass signal.

        Returns BypassParseResult with granted=True only when ALL of:
        - comment matches EMERGENCY-BYPASS: <non-empty reason>
        - actor is in authorized list
        - bypass has not already been consumed for this PR
        - Kafka event + DB audit write both succeed
        """
        # 1. Format check
        reason = self._extract_reason(comment_body)
        if reason is None:
            logger.debug(
                "EmergencyBypassParser: malformed comment from %s on PR #%d",
                actor,
                pr_number,
            )
            return BypassParseResult(
                granted=False,
                actor=actor,
                pr_number=pr_number,
                repo=repo,
                rejection_reason=BypassRejectionReason.MALFORMED_COMMENT,
            )

        # 2. Authorization check
        if actor not in self._authorized:
            logger.warning(
                "EmergencyBypassParser: unauthorized bypass attempt by %s on %s PR #%d",
                actor,
                repo,
                pr_number,
            )
            return BypassParseResult(
                granted=False,
                actor=actor,
                pr_number=pr_number,
                repo=repo,
                rejection_reason=BypassRejectionReason.UNAUTHORIZED_ACTOR,
            )

        # 3. One-time consumption check via Valkey
        valkey_key = self._valkey_key(repo, pr_number)
        if self._valkey.get(valkey_key) is not None:
            logger.warning(
                "EmergencyBypassParser: bypass already consumed for %s PR #%d",
                repo,
                pr_number,
            )
            return BypassParseResult(
                granted=False,
                actor=actor,
                pr_number=pr_number,
                repo=repo,
                rejection_reason=BypassRejectionReason.ALREADY_CONSUMED,
            )

        # 4. All pre-checks passed — emit Kafka event then write DB
        audit_id = str(uuid4())
        kafka_event_id = str(uuid4())
        now = datetime.now(tz=UTC)
        pr_url = f"https://github.com/{repo}/pull/{pr_number}"

        kafka_payload: dict[str, Any] = {
            "event_id": kafka_event_id,
            "pr_number": pr_number,
            "repo": repo,
            "actor": actor,
            "reason": reason,
            "timestamp": now.isoformat(),
            "sha": head_sha,
        }

        # Emit Kafka event first (always)
        self._kafka.publish(TOPIC_BYPASS_USED, kafka_payload)
        logger.info(
            "EmergencyBypassParser: emitted %s for %s PR #%d by %s",
            TOPIC_BYPASS_USED,
            repo,
            pr_number,
            actor,
        )

        # Write DB audit row — emit compensating event on failure
        try:
            self._db.execute(
                _INSERT_AUDIT_SQL,
                {
                    "audit_id": audit_id,
                    "pr_url": pr_url,
                    "actor": actor,
                    "reason": reason,
                    "bypass_timestamp": now.isoformat(),
                    "kafka_event_id": kafka_event_id,
                },
            )
        except Exception:
            logger.exception(
                "EmergencyBypassParser: DB write failed for %s PR #%d — "
                "emitting compensating event",
                repo,
                pr_number,
            )
            self._kafka.publish(
                TOPIC_BYPASS_ROLLED_BACK,
                {
                    "kafka_event_id": kafka_event_id,
                    "pr_number": pr_number,
                    "repo": repo,
                    "actor": actor,
                    "reason": reason,
                    "timestamp": now.isoformat(),
                    "sha": head_sha,
                },
            )
            return BypassParseResult(
                granted=False,
                actor=actor,
                pr_number=pr_number,
                repo=repo,
                rejection_reason=BypassRejectionReason.AUDIT_FAILURE,
            )

        # 5. Mark as consumed in Valkey
        self._valkey.setex(valkey_key, _VALKEY_TTL_SECONDS, "1")
        logger.info(
            "EmergencyBypassParser: bypass granted for %s PR #%d by %s (audit_id=%s)",
            repo,
            pr_number,
            actor,
            audit_id,
        )

        return BypassParseResult(
            granted=True,
            actor=actor,
            pr_number=pr_number,
            repo=repo,
            reason=reason,
            audit_id=audit_id,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_reason(comment_body: str) -> str | None:
        """Return the trimmed reason string, or None if comment is malformed."""
        first_line = comment_body.split("\n", 1)[0]
        if not first_line.startswith(_BYPASS_PREFIX):
            return None
        reason = first_line[len(_BYPASS_PREFIX) :].strip()
        if not reason:
            return None
        return reason

    @staticmethod
    def _valkey_key(repo: str, pr_number: int) -> str:
        owner, name = [*repo.split("/", 1), "unknown"][:2]
        return f"review_bot:bypass:{owner}:{name}:{pr_number}"


__all__: list[str] = [
    "BypassParseResult",
    "BypassRejectionReason",
    "HandlerEmergencyBypassParser",
]
