# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Envelope-enrichment parity tests (OMN-16048 / OMN-16018 / OMN-16020).

OMN-16048 ruled REPLICATE: ``node_event_emit_effect`` must reproduce the
legacy ``node_emit_daemon``'s publish-time envelope enrichment byte-for-byte.
The parity spec was NOT narrowed, so these tests assert the full bar.

The load-bearing test here is ``test_shadow_parity_is_byte_identical_for_every_event_type``:
it runs the SAME harness that measured the 1/62 failure
(``scripts/shadow_mode_parity_proof.py``) in-process and asserts 62/62. It is
the regression guard -- unit assertions below it exist to localise a failure,
not to substitute for it.

RED-first evidence (dev HEAD, pre-fix): 1/62 byte-identical, 61 event types
mismatched on {causation_id, emitted_at, entity_id, schema_version,
correlation_id} plus 63/65 partition keys null on the new side.
"""

from __future__ import annotations

import asyncio
import importlib.util
import shutil
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_event_emit_effect.enrichment import (
    UNCONDITIONAL_ENRICHMENT_FIELDS,
    apply_transform,
    default_clock,
    default_correlation_id_factory,
    derive_partition_key,
    inject_metadata,
)
from omnimarket.nodes.node_event_emit_effect.handlers.handler_event_emit_effect import (
    HandlerEventEmitEffect,
)
from omnimarket.nodes.node_event_emit_effect.models.model_emit_request import (
    ModelEmitRequest,
)
from omnimarket.nodes.node_event_emit_effect.spool.spool_outbox import SpoolOutbox
from omnimarket.nodes.node_event_emit_effect.spool.topic_resolver import (
    default_registry_path,
    resolve_event_type,
    resolve_partition_key_field,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
HARNESS_PATH = REPO_ROOT / "scripts" / "shadow_mode_parity_proof.py"

FROZEN_AT = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
FROZEN_CORR = "00000000-0000-4000-8000-000000000000"


def _frozen_clock() -> datetime:
    return FROZEN_AT


def _frozen_corr() -> str:
    return FROZEN_CORR


class FakePublishAdapter:
    """Records publish calls; never touches the network."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object, str | None]] = []

    def publish(
        self,
        topic: str,
        payload: object,
        *,
        key: str | None,
        correlation_id: str | None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.calls.append((topic, payload, key))


def _handler(tmp_path: Path) -> tuple[HandlerEventEmitEffect, FakePublishAdapter]:
    adapter = FakePublishAdapter()
    handler = HandlerEventEmitEffect(
        spool=SpoolOutbox(tmp_path / "spool"),
        publish_adapter=adapter,
        clock=_frozen_clock,
        correlation_id_factory=_frozen_corr,
    )
    return handler, adapter


# ---------------------------------------------------------------------------
# The parity harness, run as a test (the actual bar)
# ---------------------------------------------------------------------------


def _load_harness() -> Any:
    spec = importlib.util.spec_from_file_location(
        "_omn16048_shadow_parity", HARNESS_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_shadow_parity_is_byte_identical_for_every_event_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Old daemon vs new node, every registered event type, byte-for-byte.

    Drives both paths from one frozen clock + one ID source (the OMN-16048
    determinism seam) so the two GENERATED fields are comparable rather than
    trivially unequal. Nothing is excluded from the diff and no value is
    rewritten after the fact -- see the harness module docstring.
    """
    # Both paths read the same env, so parity holds either way -- pinned only
    # so the local and CI runs measure the identical surface.
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("KAFKA_BOOTSTRAP_SERVERS", raising=False)

    harness = _load_harness()

    registry_raw = yaml.safe_load(harness.REGISTRY_PATH.read_text(encoding="utf-8"))
    events_raw = registry_raw["events"]

    event_types = [
        (
            event_type,
            harness.build_synthetic_payload(
                event_type, event_def.get("required_fields", [])
            ),
        )
        for event_type, event_def in events_raw.items()
    ]

    async def run() -> tuple[dict[str, Any], dict[str, Any]]:
        tmp_root = Path(tempfile.mkdtemp(prefix="omn-16048-parity-test-"))
        try:
            old_by_event, _ = await harness.run_old_path(event_types, tmp_root)
            new_by_event, _ = await harness.run_new_path(event_types, tmp_root)
        finally:
            shutil.rmtree(tmp_root, ignore_errors=True)
        return old_by_event, new_by_event

    old_by_event, new_by_event = asyncio.run(run())

    diffs = [
        harness.diff_event_type(et, old_by_event.get(et, []), new_by_event.get(et, []))
        for et, _ in event_types
    ]
    mismatched = [d for d in diffs if not d["byte_identical"]]

    assert len(event_types) == 62, (
        f"registry drifted to {len(event_types)} event types; update the "
        "expected parity count deliberately, do not auto-follow it"
    )
    assert not mismatched, (
        f"{len(mismatched)}/{len(diffs)} event types are NOT byte-identical: "
        + "; ".join(
            f"{d['event_type']}: "
            + ", ".join(
                f"{topic}(missing={info['fields_missing_in_new']}, "
                f"extra={info['fields_extra_in_new']}, "
                f"differing={info['fields_differing_values']}, "
                f"key {info['old_partition_key']!r}->{info['new_partition_key']!r})"
                for topic, info in d["per_topic"].items()
                if not info["identical"] or not info["partition_key_identical"]
            )
            for d in mismatched[:5]
        )
    )

    # Non-vacuity: a harness that published nothing on both sides would also
    # report "no mismatches". Assert the compared surface is real.
    total_new = sum(len(v) for v in new_by_event.values())
    total_old = sum(len(v) for v in old_by_event.values())
    assert total_old == total_new == 65
    enriched = sum(
        1
        for msgs in new_by_event.values()
        for m in msgs
        if isinstance(m.payload, dict)
        and all(f in m.payload for f in UNCONDITIONAL_ENRICHMENT_FIELDS)
    )
    keyed = sum(1 for msgs in new_by_event.values() for m in msgs if m.key is not None)
    assert enriched == 65, f"only {enriched}/65 new-path messages were enriched"
    # 2 of the 62 registered events declare no partition_key_field; the daemon
    # publishes those with a null key, so 63 is the correct non-null count.
    assert keyed == 63, f"only {keyed}/63 new-path messages carried a partition key"


# ---------------------------------------------------------------------------
# Metadata injection (OMN-16018)
# ---------------------------------------------------------------------------


def test_handler_injects_all_enrichment_fields(tmp_path: Path) -> None:
    handler, adapter = _handler(tmp_path)
    handler.handle(
        ModelEmitRequest(event_type="session.started", payload={"session_id": "sess-1"})
    )

    (_topic, payload, _key) = adapter.calls[0]
    assert isinstance(payload, dict)
    assert payload["correlation_id"] == FROZEN_CORR
    assert payload["causation_id"] is None
    assert payload["emitted_at"] == FROZEN_AT.isoformat()
    assert payload["schema_version"] == "1.0.0"
    assert payload["session_id"] == "sess-1"
    # sess-1 is not a UUID -> sha256[:32] rendered as a UUID
    assert payload["entity_id"] == "abe633f3-a47a-2758-174e-abe9160daf36"


def test_payload_correlation_id_wins_over_generation(tmp_path: Path) -> None:
    handler, adapter = _handler(tmp_path)
    handler.handle(
        ModelEmitRequest(
            event_type="session.started",
            payload={"session_id": "s", "correlation_id": "from-payload"},
            correlation_id="from-request",
        )
    )
    (_t, payload, _k) = adapter.calls[0]
    assert isinstance(payload, dict)
    assert payload["correlation_id"] == "from-payload"


def test_request_correlation_id_seeds_when_payload_has_none(tmp_path: Path) -> None:
    handler, adapter = _handler(tmp_path)
    handler.handle(
        ModelEmitRequest(
            event_type="session.started",
            payload={"session_id": "s"},
            correlation_id="from-request",
        )
    )
    (_t, payload, _k) = adapter.calls[0]
    assert isinstance(payload, dict)
    assert payload["correlation_id"] == "from-request"


def test_uuid_session_id_passes_through_as_entity_id() -> None:
    out = inject_metadata(
        {"session_id": "3f2504e0-4f89-41d3-9a0c-0305e82c3301"},
        clock=_frozen_clock,
        correlation_id_factory=_frozen_corr,
        env={},
    )
    assert out["entity_id"] == "3f2504e0-4f89-41d3-9a0c-0305e82c3301"


def test_session_id_falls_back_to_env() -> None:
    out = inject_metadata(
        {},
        clock=_frozen_clock,
        correlation_id_factory=_frozen_corr,
        env={"CLAUDE_CODE_SESSION_ID": "env-session"},
    )
    assert out["session_id"] == "env-session"
    assert out["entity_id"] is not None


def test_no_session_id_anywhere_means_no_entity_id() -> None:
    out = inject_metadata(
        {}, clock=_frozen_clock, correlation_id_factory=_frozen_corr, env={}
    )
    assert "session_id" not in out
    assert "entity_id" not in out
    # ...but the unconditional fields are still injected.
    assert out["schema_version"] == "1.0.0"
    assert out["causation_id"] is None


def test_inject_metadata_does_not_mutate_input() -> None:
    original = {"session_id": "s"}
    inject_metadata(
        original, clock=_frozen_clock, correlation_id_factory=_frozen_corr, env={}
    )
    assert original == {"session_id": "s"}


def test_production_defaults_are_the_daemon_expressions() -> None:
    """The seam must not change runtime behavior: defaults are now()/uuid4()."""
    before = datetime.now(UTC)
    stamped = default_clock()
    after = datetime.now(UTC)
    assert before <= stamped <= after
    assert stamped.tzinfo is UTC

    first = default_correlation_id_factory()
    second = default_correlation_id_factory()
    assert first != second
    from uuid import UUID

    assert UUID(first).version == 4


# ---------------------------------------------------------------------------
# Partition-key derivation (OMN-16020)
# ---------------------------------------------------------------------------


def test_partition_key_derived_from_registry_field(tmp_path: Path) -> None:
    handler, adapter = _handler(tmp_path)
    handler.handle(
        ModelEmitRequest(event_type="session.started", payload={"session_id": "abc"})
    )
    (_topic, _payload, key) = adapter.calls[0]
    assert key == "abc"


def test_partition_key_null_when_declared_field_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # session.started keys on session_id; with no session_id in the payload
    # AND no CLAUDE_CODE_SESSION_ID to fall back to, the daemon publishes a
    # null key -- so must the node.
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    handler, adapter = _handler(tmp_path)
    handler.handle(ModelEmitRequest(event_type="session.started", payload={}))
    (_topic, _payload, key) = adapter.calls[0]
    assert key is None


def test_env_session_id_fallback_also_supplies_the_partition_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The key is derived POST-enrichment, so the env fallback feeds it."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "env-session")
    handler, adapter = _handler(tmp_path)
    handler.handle(ModelEmitRequest(event_type="session.started", payload={}))
    (_topic, _payload, key) = adapter.calls[0]
    assert key == "env-session"


def test_explicit_partition_key_overrides_derivation(tmp_path: Path) -> None:
    handler, adapter = _handler(tmp_path)
    handler.handle(
        ModelEmitRequest(
            event_type="session.started",
            payload={"session_id": "abc"},
            partition_key="explicit",
        )
    )
    (_topic, _payload, key) = adapter.calls[0]
    assert key == "explicit"


def test_partition_key_stringifies_non_string_values() -> None:
    """The daemon does str(value), so an int pr_number becomes '7', not 7."""
    assert derive_partition_key("pr_number", {"pr_number": 7}) == "7"
    assert derive_partition_key(None, {"pr_number": 7}) is None
    assert derive_partition_key("pr_number", {"pr_number": None}) is None


def test_every_registered_event_partition_key_field_resolves() -> None:
    """resolve_partition_key_field must cover the whole registry, not a subset."""
    registry = yaml.safe_load(default_registry_path().read_text(encoding="utf-8"))
    for event_type, event_def in registry["events"].items():
        assert resolve_partition_key_field(event_type) == event_def.get(
            "partition_key_field"
        )


# ---------------------------------------------------------------------------
# Per-fan-out-rule transforms
# ---------------------------------------------------------------------------


def test_fan_out_topics_carry_different_payloads(tmp_path: Path) -> None:
    """prompt.submitted's evt topic is strip_prompt'd; its cmd topic is not.

    A single shared payload for both topics -- the pre-OMN-16048 shape --
    cannot represent this.
    """
    handler, adapter = _handler(tmp_path)
    handler.handle(
        ModelEmitRequest(
            event_type="prompt.submitted",
            payload={
                "session_id": "s",
                "prompt_preview": "hello",
                "prompt": "hello world",
            },
        )
    )
    by_topic = {topic: payload for topic, payload, _ in adapter.calls}
    cmd = by_topic["onex.cmd.omniintelligence.claude-hook-event.v1"]
    evt = by_topic["onex.evt.omniclaude.prompt-submitted.v1"]
    assert isinstance(cmd, dict)
    assert isinstance(evt, dict)

    assert cmd["prompt"] == "hello world"  # untransformed rule
    assert "prompt" not in evt  # strip_prompt dropped it
    assert evt["prompt_length"] == len("hello world")
    assert evt["prompt_preview"] == "hello"


def test_strip_body_transform_matches_daemon() -> None:
    out = apply_transform("strip_body", {"body": "abc"})
    assert out == {"body_length": 3, "body_preview": "abc"}


def test_unknown_transform_falls_back_to_passthrough() -> None:
    payload = {"a": 1}
    assert apply_transform("no-such-transform", payload) == payload
    assert apply_transform("passthrough", payload) == payload
    assert apply_transform(None, payload) == payload


def test_transform_registry_covers_every_declared_registry_transform() -> None:
    """No registry fan-out rule may name a transform this node cannot apply."""
    from omnimarket.nodes.node_event_emit_effect.enrichment import TRANSFORM_REGISTRY

    registry = yaml.safe_load(default_registry_path().read_text(encoding="utf-8"))
    declared = {
        rule.get("transform")
        for event_def in registry["events"].values()
        for rule in (event_def.get("fan_out") or [])
        if rule.get("transform") is not None
    }
    assert declared <= set(TRANSFORM_REGISTRY)


def test_resolved_topics_carry_their_transform_name() -> None:
    topics = {t.topic: t.transform_name for t in resolve_event_type("prompt.submitted")}
    assert topics["onex.evt.omniclaude.prompt-submitted.v1"] == "strip_prompt"


# ---------------------------------------------------------------------------
# Non-dict payloads
# ---------------------------------------------------------------------------


def test_non_dict_payload_is_published_verbatim(tmp_path: Path) -> None:
    """A list payload has no field surface to enrich, transform, or key off."""
    handler, adapter = _handler(tmp_path)
    handler.handle(ModelEmitRequest(event_type="session.started", payload=["a", "b"]))
    (_topic, payload, key) = adapter.calls[0]
    assert payload == ["a", "b"]
    assert key is None


# ---------------------------------------------------------------------------
# Spool records freeze the enriched bytes
# ---------------------------------------------------------------------------


def test_spooled_record_stores_enriched_payload_and_key(tmp_path: Path) -> None:
    """A restart-replayed event must go out with the bytes it was enriched
    with, not be re-enriched under a later clock."""
    spool = SpoolOutbox(tmp_path / "spool")
    handler = HandlerEventEmitEffect(
        spool=spool,
        publish_adapter=None,  # spool-only: nothing is acked
        clock=_frozen_clock,
        correlation_id_factory=_frozen_corr,
    )
    handler.handle(
        ModelEmitRequest(event_type="session.started", payload={"session_id": "abc"})
    )

    pending = spool.list_pending()
    assert len(pending) == 1
    record = pending[0].record
    assert record.topic == "onex.evt.omniclaude.session-started.v1"
    assert record.partition_key == "abc"
    assert isinstance(record.payload, dict)
    assert record.payload["emitted_at"] == FROZEN_AT.isoformat()
    assert record.payload["schema_version"] == "1.0.0"


def test_fan_out_spools_one_record_per_topic(tmp_path: Path) -> None:
    spool = SpoolOutbox(tmp_path / "spool")
    handler = HandlerEventEmitEffect(
        spool=spool,
        publish_adapter=None,
        clock=_frozen_clock,
        correlation_id_factory=_frozen_corr,
    )
    handler.handle(
        ModelEmitRequest(
            event_type="prompt.submitted",
            payload={"session_id": "s", "prompt_preview": "p"},
        )
    )
    assert spool.pending_count() == 2
    assert {f.record.topic for f in spool.list_pending()} == {
        "onex.cmd.omniintelligence.claude-hook-event.v1",
        "onex.evt.omniclaude.prompt-submitted.v1",
    }
