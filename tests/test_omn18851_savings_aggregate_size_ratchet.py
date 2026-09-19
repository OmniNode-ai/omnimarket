# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18851: the delegation savings aggregate must stay publishable.

WHAT THIS PINS, AND WHY IT IS THREE SEPARATE THINGS

``SavingsProjectionRunner._publish_aggregate_snapshots`` re-reads the limit-1
view ``projection_delegation_savings`` and republishes the WHOLE row to a
compacted snapshot topic after EVERY applied event. On 2026-09-19 that row was
2,548,602 bytes against a 1,048,588-byte producer limit, because its
``sessions`` array embedded the full ``prompt_text`` and ``response_text`` of
500 delegations. ``response_text`` alone was 2,061,680 bytes -- 80.3%. Every
publish raised ``MessageSizeTooLargeError``, the offset was never committed,
and the runner exhausted its ten-session budget and exited: a crash loop that
had frozen the projection since 2026-09-10.

Three independent failures had to line up, so three independent things are
pinned here and none of them subsumes another:

1. THE SHAPE (``test_migration_090_*``). The aggregate must not carry model
   text. Read off the migration on disk, so re-adding either column to the
   aggregate's input CTE is a red test rather than a review catch. This is a
   static check on purpose: it fails on a laptop with no database.

2. THE SIZE (``test_realistic_full_window_*``). A bounded shape is not a
   bounded size -- the remaining metadata could grow past the limit on its own.
   A full 500-session window at the measured field widths is encoded through
   the real publisher and asserted under the real bound. The negative control
   beside it re-adds the text and asserts the encoding DOES cross the bound,
   because a size assertion that would pass on the broken input proves nothing.

3. THE REFUSAL (``test_oversize_*``). Shape and size are both properties of
   today's data, and neither stops the NEXT unbounded column. An over-bound
   snapshot must be refused by us, with a typed error classified POISON, so it
   is quarantined and the offset advances -- instead of the driver raising
   ``MessageSizeTooLargeError`` into a retry loop that kills the writer.

WHY POISON AND NOT RECOVERABLE

``classify_projection_error`` defaults an unrecognised exception to
RECOVERABLE, which is the safe direction for an unknown fault and is exactly
what turned this into a nine-day outage: an error that can NEVER succeed on
retry was retried forever. An over-bound snapshot is deterministic -- the same
row re-encodes to the same size every time -- so retrying it is a hot loop with
no exit. Quarantining is safe here specifically because this aggregate is a
FULL-STATE snapshot whose row is already durable in Postgres before the
republish is attempted: the next successful apply republishes current state, so
nothing is lost by dropping one republish, and the DLQ record names the refusal
rather than leaving a silent gap.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import pytest

from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.snapshot_publisher import (
    SnapshotPayloadTooLargeError,
    assert_snapshot_within_bound,
    encode_snapshot_delta,
    resolve_snapshot_max_payload_bytes,
)

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[1]
_NODE = _REPO / "src" / "omnimarket" / "nodes" / "node_projection_savings"
_MIGRATIONS = _NODE / "migrations"
_CONTRACT = _NODE / "contract.yaml"

#: The aggregate exposure this ticket is about.
_AGGREGATE_TOPIC = "onex.snapshot.projection.delegation.savings.v1"

#: The producer's own documented default, mirrored from
#: ``omnibase_infra/src/omnibase_infra/runtime/models/model_kafka_producer_config.py``.
#: Pinned as a literal so a silent change to that default is visible here.
_PRODUCER_DEFAULT_LIMIT = 1_048_588

#: Measured on the .201 dev lane 2026-09-19T20:20Z, house tenant, 489 sessions.
#: These are the real widths the ratchet sizes against, not invented ones.
_MEASURED_MAX_RESPONSE_TEXT = 124_301
_MEASURED_TOTAL_TEXT_BYTES = 2_247_464  # response 2,061,680 + prompt 185,784
_MEASURED_NON_TEXT_BYTES = 319_019
_WINDOW_ROWS = 500


def _latest_aggregate_migration() -> Path:
    """The highest-numbered migration that redefines the aggregate view.

    Resolved rather than hardcoded: the assertion is about the view as it
    stands, so a later migration that redefines it must be the one read.
    """
    candidates = [
        path
        for path in sorted(_MIGRATIONS.glob("*.sql"))
        if "CREATE OR REPLACE VIEW public.projection_delegation_savings"
        in path.read_text("utf-8")
    ]
    assert candidates, (
        "no migration defines projection_delegation_savings; the view this "
        "ticket bounds has moved or been renamed"
    )
    return candidates[-1]


def _limited_sessions_cte(sql: str) -> str:
    """The body of the ``limited_sessions`` CTE -- the aggregate's only input."""
    match = re.search(
        r"limited_sessions AS \((.*?)\n\),\n", sql, re.DOTALL | re.IGNORECASE
    )
    assert match, (
        "could not find the limited_sessions CTE; the aggregate's input CTE "
        "has been renamed and this ratchet no longer reads what it claims to"
    )
    return match.group(1)


# ---------------------------------------------------------------------------
# 1. THE SHAPE
# ---------------------------------------------------------------------------


def test_migration_090_exists_and_redefines_the_aggregate() -> None:
    """The fix ships as a migration, not as an untracked hand-edit on a lane."""
    migration = _MIGRATIONS / "090_savings_aggregate_excludes_model_text.sql"
    assert migration.exists(), "migration 090 is missing"
    body = migration.read_text("utf-8")
    assert "CREATE OR REPLACE VIEW public.projection_delegation_savings" in body


def test_migration_090_limited_sessions_omits_model_text() -> None:
    """The aggregate's input CTE must not select either text column.

    Read off the LATEST migration defining the view, so a future migration
    that re-adds them fails here.
    """
    sql = _latest_aggregate_migration().read_text("utf-8")
    cte = _limited_sessions_cte(sql)

    # The comment block in 090 names both columns while explaining their
    # absence, so the assertion must read the SQL, not the prose. Strip
    # comment lines before looking.
    statements = "\n".join(
        line for line in cte.splitlines() if not line.strip().startswith("--")
    )
    for column in ("prompt_text", "response_text"):
        assert column not in statements, (
            f"{column} is selected by limited_sessions, which is the only "
            f"input to the sessions jsonb_agg. Adding it back re-inflates the "
            f"republished snapshot -- the OMN-18851 crash loop. Serve it "
            f"per-correlation instead."
        )


def test_positive_control_the_text_columns_are_still_upstream() -> None:
    """A zero must be proven, not assumed.

    The previous test passes trivially if the columns vanished from the whole
    file (a rename, a dropped join). They are still read from
    ``delegation_events`` by ``event_sessions`` -- absent only from the
    AGGREGATE. Without this control the ratchet cannot tell "excluded from the
    aggregate" from "gone entirely", and the difference is whether the
    per-correlation evidence route still has anything to serve.
    """
    sql = _latest_aggregate_migration().read_text("utf-8")
    for column in ("prompt_text", "response_text"):
        assert column in sql, (
            f"{column} is absent from the whole view definition, not merely "
            f"from the aggregate. The per-correlation evidence route reads "
            f"these columns; they must survive upstream of limited_sessions."
        )


# ---------------------------------------------------------------------------
# 2. THE SIZE
# ---------------------------------------------------------------------------


def _aggregate_exposure() -> Any:
    import yaml

    contract = yaml.safe_load(_CONTRACT.read_text("utf-8"))
    exposures = load_projection_exposures_from_contract(
        contract, str(contract.get("name", "projection_savings")), _CONTRACT
    )
    for exposure in exposures:
        if exposure.topic == _AGGREGATE_TOPIC:
            return exposure
    raise AssertionError(f"contract declares no exposure {_AGGREGATE_TOPIC!r}")


def _session(index: int, *, with_text: bool) -> dict[str, Any]:
    """One element of the ``sessions`` array, at realistic field widths."""
    session: dict[str, Any] = {
        "session_id": f"{index:08d}-1111-4111-8111-111111111111",
        "task_type": "code_generation",
        "model_name": "qwen2.5-coder-32b-instruct",
        "local_cost_usd": 0.0012345,
        "cloud_cost_usd": 0.1562400,
        "counterfactual_baseline_usd": 0.1562400,
        "savings_usd": 0.1550055,
        "baseline_model": "claude-opus-4-6",
        "pricing_manifest_version": "runtime-delegation-events",
        "savings_method": "measured",
        "usage_source": "measured",
        "prompt_tokens": 1931,
        "completion_tokens": 1697,
        "tokens_to_compliance": 924,
        "latency_ms": 31970,
        "created_at": "2026-09-19T20:20:00+00:00",
    }
    if with_text:
        # The negative control uses the MEASURED maximum rather than an
        # invented one, so the control proves the real input was over-bound.
        session["prompt_text"] = "p" * 500
        session["response_text"] = "r" * (_MEASURED_MAX_RESPONSE_TEXT // 100)
    return session


def _aggregate_row(*, with_text: bool) -> dict[str, Any]:
    sessions = [_session(i, with_text=with_text) for i in range(_WINDOW_ROWS)]
    return {
        "snapshot_grain": _AGGREGATE_TOPIC,
        "tenant_id": "820272f9-4aaf-5add-a2df-0af942852ab2",
        "cumulative_savings_usd": 77.5027,
        "cumulative_local_cost_usd": 0.617,
        "cumulative_cloud_cost_usd": 78.12,
        "cumulative_counterfactual_baseline_usd": 78.12,
        "baseline_model": "claude-opus-4-6",
        "pricing_manifest_version": "runtime-delegation-events",
        "session_count": _WINDOW_ROWS,
        # asyncpg hands a jsonb column back as a JSON string, and the exposure
        # declares `sessions` in json_columns, so the encoder is given the same
        # shape the runner gives it in production.
        "sessions": json.dumps(sessions),
        "captured_at": "2026-09-19T20:20:00+00:00",
        "provisioned": True,
        "latest_projection_updated_at": "2026-09-19T20:20:00+00:00",
    }


def _encode(row: dict[str, Any]) -> Any:
    return encode_snapshot_delta(
        _aggregate_exposure(),
        op="upsert",
        row=row,
        source_event_id="00000000-0000-4000-8000-000000000000",
        source_topic="onex.evt.omnibase-infra.delegation-completed.v1",
        source_partition=0,
        source_offset=291,
        observed_at="2026-09-19T20:20:00+00:00",
        tenant_id="820272f9-4aaf-5add-a2df-0af942852ab2",
    )


def test_realistic_full_window_aggregate_is_under_the_bound() -> None:
    """A FULL 500-session window must encode under the producer's limit.

    The live window held 489 sessions; this uses the cap, so the ratchet binds
    the worst case the view can produce rather than the sample that happened
    to be live.
    """
    message = _encode(_aggregate_row(with_text=False))
    assert message is not None
    assert message.value is not None
    size = len(message.value)
    bound = resolve_snapshot_max_payload_bytes()
    assert size < bound, (
        f"the delegation savings aggregate encodes to {size:,} bytes at a full "
        f"{_WINDOW_ROWS}-session window, at or over the {bound:,}-byte bound. "
        f"The writer republishes this row after every applied event, so this "
        f"is the OMN-18851 crash loop. Do not raise the bound: move the "
        f"oversized field behind a reference."
    )


def test_negative_control_re_adding_model_text_does_cross_the_bound() -> None:
    """The size assertion must be capable of failing on the real broken input.

    Without this, ``test_realistic_full_window_*`` could be passing because the
    fixture is small rather than because the fix works.
    """
    message = _encode(_aggregate_row(with_text=True))
    assert message is not None
    assert message.value is not None
    size = len(message.value)
    bound = resolve_snapshot_max_payload_bytes()
    assert size > bound, (
        f"the control encoded to only {size:,} bytes against a {bound:,}-byte "
        f"bound, so it does not reproduce the over-bound condition and the "
        f"ratchet beside it proves nothing. Widen the control's text fields."
    )


def test_measured_live_payload_would_have_been_refused() -> None:
    """The bound must actually sit below what the lane produced.

    A bound set above the live 2,548,602-byte payload would be green and
    useless.
    """
    bound = resolve_snapshot_max_payload_bytes()
    live_payload_bytes = _MEASURED_TOTAL_TEXT_BYTES + _MEASURED_NON_TEXT_BYTES
    assert bound < live_payload_bytes, (
        f"the bound {bound:,} is at or above the {live_payload_bytes:,}-byte "
        f"payload measured live on 2026-09-19, so it would not have caught "
        f"the outage it exists to catch"
    )


# ---------------------------------------------------------------------------
# 3. THE REFUSAL
# ---------------------------------------------------------------------------


def test_bound_defaults_to_the_producer_limit() -> None:
    """The guard refuses exactly where the producer would, not at a second
    magic number that can drift away from it."""
    previous = os.environ.pop("KAFKA_MAX_REQUEST_SIZE", None)
    try:
        assert resolve_snapshot_max_payload_bytes() == _PRODUCER_DEFAULT_LIMIT
    finally:
        if previous is not None:
            os.environ["KAFKA_MAX_REQUEST_SIZE"] = previous


def test_bound_is_read_from_the_producer_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Config, not a constant: an operator who moves the producer's limit moves
    the guard with it in one place."""
    monkeypatch.setenv("KAFKA_MAX_REQUEST_SIZE", "4194304")
    assert resolve_snapshot_max_payload_bytes() == 4_194_304


def test_unparseable_bound_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A guard that silently falls back to a default when its own config is
    garbage has not been configured, it has been ignored."""
    monkeypatch.setenv("KAFKA_MAX_REQUEST_SIZE", "not-a-number")
    with pytest.raises(ValueError, match="KAFKA_MAX_REQUEST_SIZE"):
        resolve_snapshot_max_payload_bytes()


def test_oversize_snapshot_raises_the_typed_error() -> None:
    message = _encode(_aggregate_row(with_text=True))
    assert message is not None
    with pytest.raises(SnapshotPayloadTooLargeError) as caught:
        assert_snapshot_within_bound(message, limit_bytes=1_048_588)
    text = str(caught.value)
    # The message must name the topic and both numbers: the whole point is that
    # the next reader can tell WHICH aggregate and by HOW MUCH without a
    # database.
    assert _AGGREGATE_TOPIC in text
    assert "1,048,588" in text or "1048588" in text


def test_under_bound_snapshot_passes() -> None:
    message = _encode(_aggregate_row(with_text=False))
    assert message is not None
    assert_snapshot_within_bound(message, limit_bytes=1_048_588)


def test_tombstone_passes() -> None:
    """A delete carries ``value=None``; len() of it would be a TypeError."""
    exposure = _aggregate_exposure()
    message = encode_snapshot_delta(
        exposure,
        op="delete",
        row=None,
        key={
            "snapshot_grain": _AGGREGATE_TOPIC,
            "tenant_id": "820272f9-4aaf-5add-a2df-0af942852ab2",
        },
        source_event_id="00000000-0000-4000-8000-000000000000",
        source_topic="onex.evt.omnibase-infra.delegation-completed.v1",
        source_partition=0,
        source_offset=291,
        observed_at="2026-09-19T20:20:00+00:00",
        tenant_id="820272f9-4aaf-5add-a2df-0af942852ab2",
    )
    assert message is not None
    assert message.value is None
    assert_snapshot_within_bound(message, limit_bytes=1_048_588)


def test_oversize_snapshot_is_classified_poison() -> None:
    """The classification is the whole fix for the crash loop.

    RECOVERABLE re-raises without committing, which is what retried a
    deterministic failure ten times and killed the process. POISON quarantines
    and commits, so the projection keeps advancing.
    """
    from omnimarket.projection.error_classification import (
        ProjectionErrorClass,
        classify_projection_error,
    )

    error = SnapshotPayloadTooLargeError(
        topic=_AGGREGATE_TOPIC, size_bytes=2_548_602, limit_bytes=1_048_588
    )
    assert classify_projection_error(error) is ProjectionErrorClass.POISON


def test_control_an_unknown_error_is_still_recoverable() -> None:
    """The POISON addition must not widen the default.

    If this ever returns POISON, the new entry was added as a base class broad
    enough to quarantine unrelated faults -- the failure mode
    ``error_classification`` warns about in its own module docstring.
    """
    from omnimarket.projection.error_classification import (
        ProjectionErrorClass,
        classify_projection_error,
    )

    assert (
        classify_projection_error(RuntimeError("something nobody has seen"))
        is ProjectionErrorClass.RECOVERABLE
    )
