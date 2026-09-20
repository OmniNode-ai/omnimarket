# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18770 — the error-fingerprint exposure, pinned AC by AC.

Every test here is named for the acceptance criterion it falsifies. The
categorisation tests carry the real reason this ticket exists: the surface the
Lab Errors widget wants to read held 69 rows and every single one of them said
``error_category = unknown``, because the ONLY classifier in the pipeline was a
logger-NAME prefix map evaluated by the PRODUCER, and none of the loggers that
actually emitted matched a prefix in it. A ranking whose every row is
``unknown`` ranks noise exactly as confidently as signal.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_projection_runtime_error_fingerprints.handlers.handler_projection_runtime_error_fingerprints import (
    HandlerProjectionRuntimeErrorFingerprints,
)
from omnimarket.nodes.node_projection_runtime_error_fingerprints.models import (
    EnumRuntimeErrorCategory,
    ModelRuntimeErrorEventWire,
    ModelRuntimeErrorFingerprintRequest,
)

CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_runtime_error_fingerprints"
    / "contract.yaml"
)

SNAPSHOT_TOPIC = "onex.snapshot.projection.runtime-error-fingerprints.v1"


def _contract() -> dict:
    with open(CONTRACT_PATH) as handle:
        return yaml.safe_load(handle)


def _event(**overrides: object) -> ModelRuntimeErrorEventWire:
    base: dict[str, object] = {
        "event_id": "11111111-1111-4111-8111-111111111111",
        "correlation_id": "22222222-2222-4222-8222-222222222222",
        "logger_family": "test.logger.da1a030e",
        "log_level": "ERROR",
        "message_template": "Database connection failed to host db-primary",
        "raw_message": "Database connection failed to host db-primary",
        "error_category": "unknown",
        "severity": "error",
        "fingerprint": "abc123def4567890",
        "occurrence_count_local": 1,
        "exception_type": "",
        "exception_message": "",
        "hostname": "lab-201",
        "service_label": "onex-kernel",
        "timestamp": datetime(2026, 9, 18, 23, 0, 0, tzinfo=UTC),
    }
    base.update(overrides)
    return ModelRuntimeErrorEventWire.model_validate(base)


def _handle(event: ModelRuntimeErrorEventWire, **kw: object):
    return HandlerProjectionRuntimeErrorFingerprints().handle(
        ModelRuntimeErrorFingerprintRequest(event=event, **kw)  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------
# AC2 — categorisation produces something other than `unknown`
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_ac2_recorded_lab_event_no_longer_categorises_as_unknown() -> None:
    """The exact shape of a row recorded on the lab surface, replayed.

    ``test.logger.da1a030e`` matches no logger-prefix rule and never will — a
    per-run synthetic logger name cannot be enumerated in advance. The producer
    therefore stamped ``unknown``, and the reducer must not inherit it: the
    message itself says ``Database connection failed``.
    """
    result = _handle(_event())
    assert result.row.error_category is EnumRuntimeErrorCategory.DATABASE


@pytest.mark.unit
def test_ac2_the_producer_stamped_category_is_never_trusted_over_evidence() -> None:
    """Envelope purity: a producer that grades its own error can be wrong.

    The event arrives stamped ``kafka_consumer``; every piece of evidence on it
    says database. The reducer derives, it does not copy.
    """
    result = _handle(
        _event(
            error_category="kafka_consumer",
            exception_type="PostgresConnectionError",
            message_template="relation {} does not exist",
        )
    )
    assert result.row.error_category is EnumRuntimeErrorCategory.DATABASE


@pytest.mark.unit
@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        (
            {"exception_type": "ConsumerStoppedError"},
            EnumRuntimeErrorCategory.KAFKA_CONSUMER,
        ),
        (
            {
                "exception_type": "KafkaTimeoutError",
                "logger_family": "aiokafka.producer",
            },
            EnumRuntimeErrorCategory.KAFKA_PRODUCER,
        ),
        ({"logger_family": "asyncpg.pool"}, EnumRuntimeErrorCategory.DATABASE),
        (
            {"logger_family": "uvicorn.error", "message_template": "boom"},
            EnumRuntimeErrorCategory.HTTP_SERVER,
        ),
        (
            {
                "logger_family": "omnibase_infra.runtime.service_kernel",
                "message_template": "boom",
            },
            EnumRuntimeErrorCategory.RUNTIME,
        ),
        (
            {"exception_type": "ClientConnectorError", "message_template": "boom"},
            EnumRuntimeErrorCategory.HTTP_CLIENT,
        ),
        (
            {"message_template": "consumer group rebalance failed for topic {}"},
            EnumRuntimeErrorCategory.KAFKA_CONSUMER,
        ),
    ],
)
def test_ac2_each_evidence_source_yields_its_category(
    kwargs: dict, expected: EnumRuntimeErrorCategory
) -> None:
    assert _handle(_event(**kwargs)).row.error_category is expected


@pytest.mark.unit
def test_ac2_exception_type_outranks_logger_prefix() -> None:
    """Precedence is declared, not incidental.

    An ``asyncpg`` logger reporting a ``ClientConnectorError`` is an HTTP
    failure surfacing through a database logger. The most specific evidence —
    the exception class — wins, and a test pins the order so a later edit
    cannot silently reverse it.
    """
    result = _handle(
        _event(logger_family="asyncpg.pool", exception_type="ClientConnectorError")
    )
    assert result.row.error_category is EnumRuntimeErrorCategory.HTTP_CLIENT


@pytest.mark.unit
def test_ac2_genuinely_unclassifiable_stays_unknown() -> None:
    """UNKNOWN must remain reachable. A classifier that always answers is a
    classifier that is sometimes lying, and `unknown` is the honest answer for
    an event carrying no category evidence at all."""
    result = _handle(
        _event(logger_family="some.app.module", message_template="it went wrong")
    )
    assert result.row.error_category is EnumRuntimeErrorCategory.UNKNOWN


@pytest.mark.unit
def test_ac2_census_of_the_recorded_lab_surface_falls_below_one_hundred_percent() -> (
    None
):
    """The AC2 falsifier in miniature, over the message templates actually
    recorded on the lab surface on 2026-09-10."""
    recorded = [
        ("test.ratelimit.c4d2f698", "Repeated error message"),
        ("test.metrics.aec64287", "Error B unique {}"),
        ("test.logger.da1a030e", "Database connection failed to host db-primary"),
    ]
    categories = [
        _handle(_event(logger_family=lg, message_template=tpl)).row.error_category
        for lg, tpl in recorded
    ]
    unknown_share = sum(
        1 for c in categories if c is EnumRuntimeErrorCategory.UNKNOWN
    ) / len(categories)
    assert unknown_share < 1.0


# --------------------------------------------------------------------------
# AC1 / AC3 — fingerprint identity and occurrence accumulation
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_ac3_fingerprint_is_derived_here_and_binds_the_derived_category() -> None:
    """The producer's fingerprint hashed the producer's WRONG category into
    itself, so two events the reducer classifies identically would carry
    different producer fingerprints. The reducer re-derives."""
    a = _handle(_event(error_category="unknown", fingerprint="aaaa")).row
    b = _handle(_event(error_category="kafka_consumer", fingerprint="bbbb")).row
    assert a.fingerprint == b.fingerprint
    assert a.fingerprint not in {"aaaa", "bbbb"}


@pytest.mark.unit
def test_ac3_occurrence_count_accumulates_across_replays() -> None:
    """The ranking IS the product: a flood of identical traces must collapse
    into ONE row whose count rises, not N rows."""
    first = _handle(_event(occurrence_count_local=3)).row
    second = _handle(
        _event(occurrence_count_local=4), prior_occurrence_count=first.occurrence_count
    ).row
    assert first.occurrence_count == 3
    assert second.occurrence_count == 7
    assert second.fingerprint == first.fingerprint


@pytest.mark.unit
def test_ac5_correlation_id_is_carried_through_to_the_row() -> None:
    """Without it the Errors widget cannot hand a correlation id to the trace
    widget, and that handoff is the whole debugging affordance."""
    row = _handle(_event(correlation_id="33333333-3333-4333-8333-333333333333")).row
    assert row.correlation_id == "33333333-3333-4333-8333-333333333333"


@pytest.mark.unit
def test_replaying_the_same_event_derives_an_identical_row() -> None:
    """Determinism: the row is a statement about the event, so a replay
    reproduces it rather than appending a second one."""
    assert _handle(_event()).row == _handle(_event()).row


# --------------------------------------------------------------------------
# AC3 / AC4 — the contract's exposure declaration
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_ac4_exposure_is_declared_bus_backed_on_the_named_topic() -> None:
    exposure = _contract()["projection_api"]
    assert exposure["topic"] == SNAPSHOT_TOPIC
    assert exposure["expose"] is True
    assert exposure["bus_backed"] is True


@pytest.mark.unit
def test_ac4_exposure_is_ranked_by_descending_occurrence_count() -> None:
    """`occurrence_count DESC` is the ranking the ticket names, and it leads
    the order_by rather than appearing anywhere in it: a served page that is
    truncated is only useful when the loudest fingerprints lead it."""
    order_by = _contract()["projection_api"]["order_by"]
    assert order_by.split(",")[0].strip().lower() == "occurrence_count desc"


@pytest.mark.unit
def test_ac4_snapshot_key_is_the_fingerprint_alone() -> None:
    """One row per fingerprint in the bus-backed cache. Keying on anything
    finer (the event id, the timestamp) mints a new key per occurrence and the
    cache fills with history it never serves — the exact eviction failure
    recorded on the consumer-flow exposure."""
    assert _contract()["projection_api"]["key_columns"] == ["fingerprint"]


@pytest.mark.unit
def test_ac5_correlation_id_is_a_first_class_exposed_column() -> None:
    assert "correlation_id" in _contract()["projection_api"]["columns"]


@pytest.mark.unit
def test_ac6_the_contract_names_no_table_that_is_empty_on_the_lab() -> None:
    """AC6 — the epic bars building on the eight declared-but-zero-row tables.
    This contract may name exactly one table: the read model it creates and
    writes itself."""
    declared = {t["name"] for t in _contract()["db_io"]["db_tables"]}
    forbidden = {
        "hook_events",
        "event_chain",
        "traces",
        "log_entries",
        "gate_activity",
        "merge_state_transitions",
        "receipt_gate_rows",
        "consumer_health_events",
    }
    assert declared == {"runtime_error_fingerprints"}
    assert not declared & forbidden


@pytest.mark.unit
def test_no_refusal_or_comment_cites_the_closed_tracker_ticket() -> None:
    """OMN-17426 owns the fact that the live refusal still names OMN-15800,
    Done since 2026-08-24. A new surface must not copy the stale name into
    itself."""
    node_dir = CONTRACT_PATH.parent
    offenders = [
        str(p)
        for p in node_dir.rglob("*")
        if p.is_file()
        and p.suffix in {".py", ".yaml", ".sql"}
        and "OMN-15800" in p.read_text()
    ]
    assert offenders == []


@pytest.mark.unit
def test_the_writer_declares_in_process_runtime_dispatch() -> None:
    """A projection whose writer is never dispatched writes nothing while every
    offset advances — the OMN-16874 failure. The dispatch branch is declared,
    never inferred from the class name's spelling."""
    from omnimarket.nodes.node_projection_runtime_error_fingerprints.handlers.handler_runtime_error_fingerprint_runner import (
        RuntimeErrorFingerprintProjectionWriter,
    )

    assert (
        RuntimeErrorFingerprintProjectionWriter.onex_runtime_inprocess_dispatch is True
    )


@pytest.mark.unit
def test_the_writer_subscribes_to_the_runtime_error_topic() -> None:
    from omnimarket.nodes.node_projection_runtime_error_fingerprints.handlers.handler_runtime_error_fingerprint_runner import (
        RuntimeErrorFingerprintProjectionWriter,
    )

    writer = RuntimeErrorFingerprintProjectionWriter()
    assert writer.subscribe_topics == ["onex.evt.omnibase-infra.runtime-error.v1"]
    assert writer._snapshot_exposure is not None
    assert writer._snapshot_exposure.topic == SNAPSHOT_TOPIC


@pytest.mark.unit
def test_ac2_an_unsided_exception_token_does_not_outrank_a_sided_logger() -> None:
    """The defect the parametrized AC2 case caught, pinned on its own.

    ``KafkaTimeoutError`` names the subsystem and not the side. With it in the
    tier-1 exception table, an ``aiokafka.producer`` failure was categorised
    ``kafka_consumer`` — the ranked surface would have blamed the wrong half of
    the seam. Tier 3 exists so it cannot happen again.
    """
    from omnimarket.nodes.node_projection_runtime_error_fingerprints.handlers.handler_projection_runtime_error_fingerprints import (
        EVIDENCE_LOGGER,
    )

    result = _handle(
        _event(exception_type="KafkaTimeoutError", logger_family="aiokafka.producer")
    )
    assert result.row.error_category is EnumRuntimeErrorCategory.KAFKA_PRODUCER
    assert result.row.category_evidence == EVIDENCE_LOGGER


@pytest.mark.unit
def test_ac2_an_unsided_exception_token_still_beats_a_message_keyword() -> None:
    """Tier 3 sits below the logger prefix, not below everything: with no
    logger evidence at all, the exception family is still better than a word
    in the message."""
    from omnimarket.nodes.node_projection_runtime_error_fingerprints.handlers.handler_projection_runtime_error_fingerprints import (
        EVIDENCE_EXCEPTION,
    )

    result = _handle(
        _event(
            exception_type="KafkaTimeoutError",
            logger_family="some.app.module",
            message_template="the database is on fire",
        )
    )
    assert result.row.error_category is EnumRuntimeErrorCategory.KAFKA_CONSUMER
    assert result.row.category_evidence == EVIDENCE_EXCEPTION


@pytest.mark.unit
def test_every_row_records_which_rule_produced_its_category() -> None:
    """A category with no recorded basis is indistinguishable from a guess,
    and this surface exists because 69 unexplained `unknown`s were."""
    from omnimarket.nodes.node_projection_runtime_error_fingerprints.handlers.handler_projection_runtime_error_fingerprints import (
        EVIDENCE_EXCEPTION,
        EVIDENCE_LOGGER,
        EVIDENCE_MESSAGE,
        EVIDENCE_NONE,
    )

    cases = [
        ({"exception_type": "PostgresError"}, EVIDENCE_EXCEPTION),
        ({"logger_family": "asyncpg.pool", "message_template": "x"}, EVIDENCE_LOGGER),
        (
            {"logger_family": "z.z", "message_template": "topic {} lost"},
            EVIDENCE_MESSAGE,
        ),
        ({"logger_family": "z.z", "message_template": "it went wrong"}, EVIDENCE_NONE),
    ]
    for kwargs, expected in cases:
        assert _handle(_event(**kwargs)).row.category_evidence == expected
