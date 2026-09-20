# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure derivation of one ranked runtime-error fingerprint row (OMN-18770).

WHY THE CATEGORY IS DERIVED HERE AND NOT READ OFF THE EVENT
    The only classifier in this pipeline lived in the PRODUCER
    (``RuntimeLogEventBridge._categorize_logger``) and answered from a
    logger-NAME prefix map with eight entries — ``aiokafka``, ``asyncpg``,
    ``aiohttp``, ``uvicorn`` and variants. Every one of the 69 rows on the lab
    surface came back ``unknown`` because none of the emitting logger names
    began with any of those prefixes, and a per-run synthetic logger name never
    will. The producing process also cannot be the authority on its own error
    class for the same reason a node cannot grade its own health: it reports
    what it believes, and this projection exists because that belief was wrong
    in one direction for 100% of the population.

PRECEDENCE, DECLARED — AND IT IS FOUR TIERS, NOT THREE
    1. a SIDED exception class — one whose name settles which side of a seam
       failed (``ConsumerStoppedError``, ``ProducerFenced``, ``PostgresError``,
       ``ClientConnectorError``). The most specific evidence there is: a class
       name is a fact about what was raised, independent of who logged it.
    2. ``logger_family`` prefix — a well-behaved library logger is strong
       evidence, and this is the producer's rule, kept rather than discarded.
    3. an UNSIDED exception family token — ``KafkaTimeoutError`` says the
       subsystem and NOT the side. This tier sits BELOW the logger prefix on
       purpose: ``aiokafka.producer`` raising ``KafkaTimeoutError`` is a
       producer failure, and a flat "exception always wins" ordering called it
       a consumer failure. That was a real defect, caught by the parametrized
       AC2 case, and the fix was the ordering rather than the expectation.
    4. a keyword in ``message_template`` — the weakest, and the one that
       rescues the whole synthetic-logger population on the lab.
    5. ``UNKNOWN``. Reachable on purpose: a classifier that always answers is a
       classifier that is sometimes lying, and the row records WHICH rule fired
       so a category is never an unexplained assertion.

The handler is def-B — ``handle(ModelX) -> ModelY`` — stateless, deterministic,
no I/O. Every database fact it needs (the accumulated count, the earliest
sighting) arrives on the request.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime

from omnimarket.nodes.node_projection_runtime_error_fingerprints.models import (
    EnumRuntimeErrorCategory,
    EnumRuntimeErrorSeverity,
    ModelRuntimeErrorEventWire,
    ModelRuntimeErrorFingerprintRequest,
    ModelRuntimeErrorFingerprintResult,
    ModelRuntimeErrorFingerprintRow,
)

_CATEGORY = EnumRuntimeErrorCategory

# --- rule 1: SIDED exception class name ------------------------------------
# A class name that settles WHICH SIDE of a seam failed. Matched as a
# case-insensitive SUBSTRING, so `asyncpg.exceptions.PostgresConnectionError`
# and a bare `PostgresConnectionError` land the same way. Ordered
# most-specific first; first match wins.
_SIDED_EXCEPTION_RULES: tuple[tuple[str, EnumRuntimeErrorCategory], ...] = (
    ("consumerstopped", _CATEGORY.KAFKA_CONSUMER),
    ("commitfailed", _CATEGORY.KAFKA_CONSUMER),
    ("offsetoutofrange", _CATEGORY.KAFKA_CONSUMER),
    ("recordtoolarge", _CATEGORY.KAFKA_PRODUCER),
    ("producerclosed", _CATEGORY.KAFKA_PRODUCER),
    ("producerfenced", _CATEGORY.KAFKA_PRODUCER),
    ("postgres", _CATEGORY.DATABASE),
    ("asyncpg", _CATEGORY.DATABASE),
    ("undefinedtable", _CATEGORY.DATABASE),
    ("uniqueviolation", _CATEGORY.DATABASE),
    ("interfaceerror", _CATEGORY.DATABASE),
    ("clientconnector", _CATEGORY.HTTP_CLIENT),
    ("clientresponse", _CATEGORY.HTTP_CLIENT),
    ("clientpayload", _CATEGORY.HTTP_CLIENT),
    ("httpstatus", _CATEGORY.HTTP_SERVER),
)

# --- rule 3: UNSIDED exception family token --------------------------------
# Names the subsystem and NOT the side, so it must NOT outrank a logger prefix
# that does. `KafkaTimeoutError` on `aiokafka.producer` is a producer failure;
# with this token in the tier-1 table it read as a consumer failure, and the
# ranked surface would have blamed the wrong half of the seam.
_UNSIDED_EXCEPTION_RULES: tuple[tuple[str, EnumRuntimeErrorCategory], ...] = (
    ("kafka", _CATEGORY.KAFKA_CONSUMER),
)

# --- rule 2: logger name prefix --------------------------------------------
# The producer's eight entries, plus the ONEX logger families the producer's
# map never covered. Longest prefix wins, so `aiokafka.producer` is not
# shadowed by `aiokafka`.
_LOGGER_PREFIX_RULES: dict[str, EnumRuntimeErrorCategory] = {
    "aiokafka.consumer": _CATEGORY.KAFKA_CONSUMER,
    "aiokafka.producer": _CATEGORY.KAFKA_PRODUCER,
    "aiokafka": _CATEGORY.KAFKA_CONSUMER,
    "kafka": _CATEGORY.KAFKA_CONSUMER,
    "asyncpg": _CATEGORY.DATABASE,
    "psycopg": _CATEGORY.DATABASE,
    "sqlalchemy": _CATEGORY.DATABASE,
    "aiohttp.client": _CATEGORY.HTTP_CLIENT,
    "aiohttp.server": _CATEGORY.HTTP_SERVER,
    "aiohttp.access": _CATEGORY.HTTP_SERVER,
    "aiohttp": _CATEGORY.HTTP_CLIENT,
    "httpx": _CATEGORY.HTTP_CLIENT,
    "uvicorn": _CATEGORY.HTTP_SERVER,
    "hypercorn": _CATEGORY.HTTP_SERVER,
    "fastapi": _CATEGORY.HTTP_SERVER,
    "omnibase_infra.runtime": _CATEGORY.RUNTIME,
    "omnibase_infra.event_bus": _CATEGORY.KAFKA_CONSUMER,
    "omnibase_infra.database": _CATEGORY.DATABASE,
    "omnibase_core.runtime": _CATEGORY.RUNTIME,
    "omnimarket.projection": _CATEGORY.RUNTIME,
    "omnimarket.nodes": _CATEGORY.RUNTIME,
}

# --- rule 3: message keyword ------------------------------------------------
# Whole-word (or phrase) matches against the TEMPLATIZED message, so a host
# name or an id that happens to contain "kafka" cannot trip a rule: the
# producer has already replaced every variable span with `{}`.
_MESSAGE_RULES: tuple[tuple[re.Pattern[str], EnumRuntimeErrorCategory], ...] = (
    (re.compile(r"\bconsumer group\b|\brebalanc", re.I), _CATEGORY.KAFKA_CONSUMER),
    (
        re.compile(r"\bcommit(ting)? offset|\boffset\b.*\bcommit", re.I),
        _CATEGORY.KAFKA_CONSUMER,
    ),
    (
        re.compile(r"\bproduc(e|er|ing)\b.*\btopic\b|\bsend_and_wait\b", re.I),
        _CATEGORY.KAFKA_PRODUCER,
    ),
    (
        re.compile(r"\b(kafka|redpanda|broker|topic|partition|dlq)\b", re.I),
        _CATEGORY.KAFKA_CONSUMER,
    ),
    (
        re.compile(
            r"\b(database|postgres|postgresql|asyncpg|sql|relation|connection pool|"
            r"deadlock|transaction)\b",
            re.I,
        ),
        _CATEGORY.DATABASE,
    ),
    (
        re.compile(
            r"\b(http request|upstream|request to|client session|outbound call)\b", re.I
        ),
        _CATEGORY.HTTP_CLIENT,
    ),
    (
        re.compile(
            r"\b(request handler|asgi|wsgi|endpoint|route handler|http server)\b", re.I
        ),
        _CATEGORY.HTTP_SERVER,
    ),
    (
        re.compile(r"\b(handler|dispatch|node|contract|envelope|runtime)\b", re.I),
        _CATEGORY.RUNTIME,
    ),
)

_SEVERITY_BY_LEVEL: dict[str, EnumRuntimeErrorSeverity] = {
    "critical": EnumRuntimeErrorSeverity.CRITICAL,
    "fatal": EnumRuntimeErrorSeverity.CRITICAL,
    "error": EnumRuntimeErrorSeverity.ERROR,
    "warning": EnumRuntimeErrorSeverity.WARNING,
    "warn": EnumRuntimeErrorSeverity.WARNING,
}

EVIDENCE_EXCEPTION = "exception_type"
EVIDENCE_LOGGER = "logger_prefix"
EVIDENCE_MESSAGE = "message_keyword"
EVIDENCE_NONE = "none"


def derive_error_category(
    *,
    exception_type: str,
    logger_family: str,
    message_template: str,
) -> tuple[EnumRuntimeErrorCategory, str]:
    """Classify one runtime error, and say which rule did it.

    Returns the category and the evidence label. The label is returned rather
    than logged because it lands on the row: an operator ranking by occurrence
    count needs to know whether a category is a class-name fact or a keyword
    guess before acting on it.
    """
    haystack = exception_type.lower()
    for needle, category in _SIDED_EXCEPTION_RULES:
        if needle in haystack:
            return category, EVIDENCE_EXCEPTION

    logger_lower = logger_family.lower()
    best_prefix = ""
    best_category: EnumRuntimeErrorCategory | None = None
    for prefix, category in _LOGGER_PREFIX_RULES.items():
        if logger_lower.startswith(prefix) and len(prefix) > len(best_prefix):
            best_prefix, best_category = prefix, category
    if best_category is not None:
        return best_category, EVIDENCE_LOGGER

    for needle, category in _UNSIDED_EXCEPTION_RULES:
        if needle in haystack:
            return category, EVIDENCE_EXCEPTION

    for pattern, category in _MESSAGE_RULES:
        if pattern.search(message_template):
            return category, EVIDENCE_MESSAGE

    return EnumRuntimeErrorCategory.UNKNOWN, EVIDENCE_NONE


def derive_fingerprint(
    *,
    logger_family: str,
    message_template: str,
    error_category: EnumRuntimeErrorCategory,
) -> str:
    """Stable identity for one error class.

    Deliberately NOT the producer's fingerprint. The producer hashed its own
    category into the digest, so the same error emitted before and after a
    classifier change would mint two identities for one error class — and on
    the lab every producer fingerprint encodes ``unknown``. The reducer hashes
    the category it derived, so the identity and the ranking agree.
    """
    raw = f"{logger_family}:{error_category.value}:{message_template}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _severity(event: ModelRuntimeErrorEventWire) -> EnumRuntimeErrorSeverity:
    for candidate in (event.severity, event.log_level):
        hit = _SEVERITY_BY_LEVEL.get(candidate.strip().lower())
        if hit is not None:
            return hit
    return EnumRuntimeErrorSeverity.ERROR


class HandlerProjectionRuntimeErrorFingerprints:
    """Derive one ranked fingerprint row from one runtime-error event."""

    def handle(
        self, request: ModelRuntimeErrorFingerprintRequest
    ) -> ModelRuntimeErrorFingerprintResult:
        """Classify, fingerprint, and accumulate. Pure and deterministic."""
        event = request.event

        category, evidence = derive_error_category(
            exception_type=event.exception_type,
            logger_family=event.logger_family,
            message_template=event.message_template or event.raw_message,
        )
        fingerprint = derive_fingerprint(
            logger_family=event.logger_family,
            message_template=event.message_template,
            error_category=category,
        )

        # Event time, never a wall clock: the row is a statement about the
        # event, so a replay has to reproduce it rather than re-date it.
        seen_at = event.timestamp or datetime(1970, 1, 1, tzinfo=UTC)
        first_seen = (
            min(request.prior_first_seen_at, seen_at)
            if request.prior_first_seen_at is not None
            else seen_at
        )

        row = ModelRuntimeErrorFingerprintRow(
            fingerprint=fingerprint,
            logger_name=event.logger_family,
            error_category=category,
            severity=_severity(event),
            message_template=event.message_template,
            exception_type=event.exception_type,
            occurrence_count=request.prior_occurrence_count
            + event.occurrence_count_local,
            correlation_id=event.correlation_id,
            service_name=event.service_label,
            hostname=event.hostname,
            first_seen_at=first_seen,
            last_seen_at=seen_at,
            category_evidence=evidence,
        )

        return ModelRuntimeErrorFingerprintResult(
            row=row,
            producer_category_disagreed=(
                event.error_category.strip().lower() != category.value
            ),
        )


__all__ = [
    "EVIDENCE_EXCEPTION",
    "EVIDENCE_LOGGER",
    "EVIDENCE_MESSAGE",
    "EVIDENCE_NONE",
    "HandlerProjectionRuntimeErrorFingerprints",
    "derive_error_category",
    "derive_fingerprint",
]
