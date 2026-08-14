#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-15971 (R9) shadow-mode parity proof: old node_emit_daemon vs new
node_event_emit_effect, driven simultaneously against synthetic traffic for
all registered event types, publish calls captured against an in-process
fake broker (no live Kafka / no live daemon socket) and diffed.

Scope and honesty note (proof lane, OMN-15971):
    This is a hermetic proof. It exercises the REAL production code paths
    on both sides (EventRegistry -> EmitSocketServer._handle_emit ->
    BoundedEventQueue -> KafkaPublisherLoop on the old side;
    HandlerEventEmitEffect.handle() on the new side) but replaces the
    network boundary (EventBusKafka) with an in-memory recorder on both
    sides. It does NOT prove parity against a live Kafka broker or the
    live Unix-socket daemon process -- that requires the .201 runtime,
    which this proof lane does not touch. What it DOES prove: given
    identical raw input payloads, whether the two code paths as they
    exist on `dev` today produce byte-identical (topic, payload) output.

Determinism seam (OMN-16048):
    Two enriched fields are GENERATED, not derived -- ``emitted_at`` (wall
    clock) and ``correlation_id`` (a fresh UUID, only when the payload
    carries none). Left alone they are trivially unequal between the two
    runs, which would make byte-parity unmeasurable rather than false. Both
    implementations therefore expose the clock and the ID source as injected
    callables whose defaults are the production expressions
    (``datetime.now(UTC)`` / ``str(uuid4())``), and this harness drives BOTH
    sides from the SAME frozen clock and the SAME ID source below.

    This is not a normalization: no field is excluded from the diff, no
    value is rewritten after the fact, and both sides run their real
    generation branches. What is proven is the generation POLICY (which
    field is preserved vs. minted, and its exact format); what is NOT
    proven -- and is not provable, because the daemon has the same property
    against itself run-to-run -- is that two independent invocations pick
    the same instant and the same random UUID.

Usage:
    uv run python scripts/shadow_mode_parity_proof.py [--out FILE]

Exit code is 0 regardless of parity outcome (this is a report, not a CI
gate) -- read the printed summary / JSON artifact for the actual result.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

REGISTRY_PATH = (
    SRC_ROOT
    / "omnimarket"
    / "nodes"
    / "node_emit_daemon"
    / "registries"
    / "topics.yaml"
)


# =============================================================================
# Shared determinism seam -- injected identically into BOTH paths
# =============================================================================

#: Frozen publish instant. Both paths format it themselves (``.isoformat()``),
#: so the formatting step is still compared, only the instant is shared.
FROZEN_EMITTED_AT = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)

#: Shared source for a GENERATED correlation_id (used only on the events
#: whose payload does not already carry one -- the "mint it" branch).
FROZEN_CORRELATION_ID = "00000000-0000-4000-8000-000000000000"


def frozen_clock() -> datetime:
    return FROZEN_EMITTED_AT


def frozen_correlation_id_factory() -> str:
    return FROZEN_CORRELATION_ID


# =============================================================================
# Synthetic payload generation
# =============================================================================


def _dummy_value(field_name: str, event_type: str) -> object:
    """Deterministic, cheap synthetic value for a required field."""
    if field_name in ("session_id", "run_id", "gate_id", "task_id", "trace_id"):
        return f"{field_name}-{event_type.replace('.', '-')}"
    if field_name == "correlation_id":
        # Fixed (not random) so old-path metadata injection is observable
        # deterministically: the daemon only injects a fresh uuid4() when
        # correlation_id is ABSENT, so supplying one here removes that one
        # axis of non-determinism from the diff.
        return f"corr-{event_type.replace('.', '-')}"
    if field_name in ("prompt_preview",):
        return "synthetic prompt preview for parity proof"
    if field_name in ("body",):
        return "synthetic chat body for parity proof, long enough to matter"
    if field_name in ("outcome", "status", "decision", "final_status"):
        return "success"
    if field_name in ("feedback_status",):
        return "accepted"
    if field_name in ("injected_pattern_ids",):
        return ["pattern-1", "pattern-2"]
    if field_name in ("cohort",):
        return "control"
    if field_name in ("channel",):
        return "context"
    if field_name in ("agent_name", "selected_agent", "agent_id"):
        return "agent-x"
    if field_name in ("action_type",):
        return "delegate"
    if field_name in ("state",):
        return "running"
    if field_name in ("message",):
        return "synthetic status message"
    if field_name in ("model_id",):
        return "model-x"
    if field_name in ("total_tokens", "tokens_used", "tokens_budget"):
        return 100
    if field_name in ("source_path",):
        return "src/example.py"
    if field_name in ("applicable_patterns",):
        return ["pattern-a"]
    if field_name in ("timestamp", "timestamp_iso"):
        return datetime.now(UTC).isoformat()
    if field_name in ("language",):
        return "python"
    if field_name in ("domain",):
        return "backend"
    if field_name in ("pattern_name",):
        return "no-hardcoded-paths"
    if field_name in ("changed_file_count",):
        return 3
    if field_name in ("task_type",):
        return "build"
    if field_name in ("delegated_to",):
        return "sonnet"
    if field_name in ("commit_sha",):
        return "0" * 40
    if field_name in ("frame_id", "span_id"):
        return f"{field_name}-{event_type.replace('.', '-')}"
    if field_name in ("skill_name", "skill"):
        return "skill-x"
    if field_name in ("epic_id",):
        return "OMN-0000"
    if field_name in ("pr_number",):
        return 1
    if field_name in ("repo",):
        return "OmniNode-ai/example"
    if field_name in ("verdict",):
        return "approve"
    if field_name in ("metric_version",):
        return "v1"
    if field_name in ("ticket_id", "plan_file"):
        return f"{field_name}-x"
    if field_name in ("total_rounds",):
        return 1
    if field_name in ("total_tickets",):
        return 1
    if field_name in ("overall_status",):
        return "clean"
    if field_name in ("signal_type",):
        return "ready"
    if field_name in ("hook_name",):
        return "PostToolUse"
    if field_name in ("error_tier", "error_category"):
        return "warn"
    if field_name in ("probe",):
        return "ping"
    if field_name in ("artifact_ref", "artifact_hash"):
        return "sha256:0" * 4
    if field_name in ("artifact_size_bytes",):
        return 1024
    if field_name in ("artifact_kind", "source_system"):
        return "test"
    if field_name in ("tool_name", "tool_name_raw"):
        return "Bash"
    if field_name in ("suppression_decision",):
        return "allow"
    if field_name in ("daemon_id",):
        return "daemon-x"
    if field_name in ("pid",):
        return 1234
    if field_name in ("socket_path",):
        return "/tmp/onex-emit.sock"  # local-path-ok: synthetic test value
    if field_name in ("kafka_offset",):
        return 42
    if field_name in ("round_trip_ms",):
        return 12.5
    if field_name in ("routing_prompt_version",):
        return "v3"
    if field_name in ("fallback_reason",):
        return "timeout"
    if field_name in ("injection_id",):
        return "inj-1"
    if field_name in ("failure_count", "threshold"):
        return 1
    if field_name in ("contract_id", "agent_type"):
        return f"{field_name}-x"
    if field_name in ("violation_type", "enforcement_action"):
        return f"{field_name}-x"
    if field_name in ("span_kind", "operation_name"):
        return f"{field_name}-x"
    if field_name in ("surface", "severity"):
        return f"{field_name}-x"
    if field_name in ("event_id",):
        return "evt-x"
    if field_name in ("event_type",):
        return event_type
    if field_name in ("payload",):
        return {"nested": "value"}
    return f"synthetic-{field_name}"


def build_synthetic_payload(
    event_type: str, required_fields: list[str]
) -> dict[str, object]:
    payload: dict[str, object] = {}
    for f in required_fields:
        payload[f] = _dummy_value(f, event_type)
    return payload


# =============================================================================
# Recorder: shared fake-broker capture surface for both paths
# =============================================================================


@dataclass
class PublishedMessage:
    topic: str
    payload: object
    key: str | None
    correlation_id: str | None


@dataclass
class Recorder:
    label: str
    messages: list[PublishedMessage] = field(default_factory=list)

    def record(
        self,
        topic: str,
        payload: object,
        *,
        key: str | None,
        correlation_id: str | None,
    ) -> None:
        self.messages.append(
            PublishedMessage(
                topic=topic, payload=payload, key=key, correlation_id=correlation_id
            )
        )


# =============================================================================
# OLD path driver: node_emit_daemon
# =============================================================================


async def run_old_path(
    event_types: list[tuple[str, dict[str, object]]], tmp_root: Path
) -> tuple[dict[str, list[PublishedMessage]], list[tuple[str, str]]]:
    """Drive the old daemon path one event at a time, fully draining between
    events, so captured messages can be attributed to their source event_type
    without guessing from topic name or payload content (two topics in the
    registry are shared by two different event_types, e.g.
    ``onex.cmd.omniintelligence.claude-hook-event.v1`` is used by both
    ``prompt.submitted`` and ``response.stopped`` -- a topic-name join key
    would silently misattribute those).
    """
    from omnimarket.nodes.node_emit_daemon.event_queue import BoundedEventQueue
    from omnimarket.nodes.node_emit_daemon.event_registry import EventRegistry
    from omnimarket.nodes.node_emit_daemon.models.model_protocol import (
        ModelDaemonEmitRequest,
    )
    from omnimarket.nodes.node_emit_daemon.publisher_loop import KafkaPublisherLoop
    from omnimarket.nodes.node_emit_daemon.socket_server import EmitSocketServer

    all_messages: list[PublishedMessage] = []

    async def fake_publish_fn(
        topic: str, key: bytes | None, value: bytes, headers: dict[str, str]
    ) -> None:
        all_messages.append(
            PublishedMessage(
                topic=topic,
                payload=json.loads(value.decode("utf-8")),
                key=key.decode("utf-8") if key else None,
                correlation_id=headers.get("correlation_id"),
            )
        )

    registry = EventRegistry.from_yaml(REGISTRY_PATH)
    queue = BoundedEventQueue(
        max_memory_queue=1000,
        spool_dir=tmp_root / "old-spool",
        outbox_dir=tmp_root / "old-outbox",
    )
    loop = KafkaPublisherLoop(queue=queue, publish_fn=fake_publish_fn)
    server = EmitSocketServer(
        socket_path=str(tmp_root / "old.sock"),
        queue=queue,
        registry=registry,
        publisher_loop=loop,
        clock=frozen_clock,
        correlation_id_factory=frozen_correlation_id_factory,
    )

    await loop.start()

    errors: list[tuple[str, str]] = []
    by_event: dict[str, list[PublishedMessage]] = {}
    for event_type, payload in event_types:
        start = len(all_messages)
        request = ModelDaemonEmitRequest(event_type=event_type, payload=payload)
        # Private-method call is deliberate: drives the real fan-out/enrich/
        # enqueue logic directly, skipping only the socket wire itself,
        # which is not part of the topic+payload parity contract.
        raw = await server._handle_emit(request)  # noqa: SLF001
        resp = json.loads(raw)
        if resp.get("status") != "queued":
            errors.append((event_type, resp.get("reason", "unknown")))

        # Drain fully before moving to the next event type. Must include the
        # durable outbox (duty_critical events) -- memory_size()/spool_size()
        # only cover the bounded telemetry queue.
        for _ in range(500):
            if (
                queue.memory_size() == 0
                and queue.spool_size() == 0
                and queue.outbox_pending() == 0
            ):
                break
            await asyncio.sleep(0.005)
        by_event[event_type] = list(all_messages[start:])

    await loop.stop(drain_timeout=5.0)
    return by_event, errors


# =============================================================================
# NEW path driver: node_event_emit_effect
# =============================================================================


async def run_new_path(
    event_types: list[tuple[str, dict[str, object]]], tmp_root: Path
) -> tuple[dict[str, list[PublishedMessage]], list[tuple[str, str]]]:
    from omnimarket.nodes.node_event_emit_effect.handlers.handler_event_emit_effect import (
        HandlerEventEmitEffect,
        ProtocolPublishAdapter,
    )
    from omnimarket.nodes.node_event_emit_effect.models.model_emit_request import (
        ModelEmitRequest,
    )

    all_messages: list[PublishedMessage] = []

    class FakeAdapter:
        """Records instead of touching a real Kafka connection."""

        def publish(
            self,
            topic: str,
            payload: object,
            *,
            key: str | None,
            correlation_id: str | None,
            timeout_seconds: float | None = None,
        ) -> None:
            all_messages.append(
                PublishedMessage(
                    topic=topic, payload=payload, key=key, correlation_id=correlation_id
                )
            )

    adapter: ProtocolPublishAdapter = FakeAdapter()
    handler = HandlerEventEmitEffect(
        publish_adapter=adapter,
        spool_dir=tmp_root / "new-spool",
        clock=frozen_clock,
        correlation_id_factory=frozen_correlation_id_factory,
    )

    errors: list[tuple[str, str]] = []
    by_event: dict[str, list[PublishedMessage]] = {}
    for event_type, payload in event_types:
        start = len(all_messages)
        request = ModelEmitRequest(
            event_type=event_type,
            payload=payload,  # type: ignore[arg-type]
            event_id=f"evt-{event_type.replace('.', '-')}",
        )
        try:
            result = handler.handle(request)
            if not result.published:
                errors.append((event_type, "published=False"))
        except Exception as exc:
            errors.append(
                (event_type, f"UNHANDLED EXCEPTION: {type(exc).__name__}: {exc}")
            )
        by_event[event_type] = list(all_messages[start:])

    return by_event, errors


# =============================================================================
# Diff
# =============================================================================


def diff_event_type(
    event_type: str,
    old_msgs: list[PublishedMessage],
    new_msgs: list[PublishedMessage],
) -> dict[str, Any]:
    old_by_topic: dict[str, list[PublishedMessage]] = {}
    for m in old_msgs:
        old_by_topic.setdefault(m.topic, []).append(m)
    new_by_topic: dict[str, list[PublishedMessage]] = {}
    for m in new_msgs:
        new_by_topic.setdefault(m.topic, []).append(m)

    old_topics = set(old_by_topic)
    new_topics = set(new_by_topic)

    result: dict[str, Any] = {
        "event_type": event_type,
        "old_topic_count": len(old_msgs),
        "new_topic_count": len(new_msgs),
        "topics_only_old": sorted(old_topics - new_topics),
        "topics_only_new": sorted(new_topics - old_topics),
        "per_topic": {},
    }

    byte_identical = True
    if old_topics - new_topics or new_topics - old_topics:
        byte_identical = False

    for topic in sorted(old_topics & new_topics):
        old_payload = old_by_topic[topic][0].payload
        new_payload = new_by_topic[topic][0].payload
        old_keys = set(old_payload.keys()) if isinstance(old_payload, dict) else set()
        new_keys = set(new_payload.keys()) if isinstance(new_payload, dict) else set()
        missing_in_new = sorted(old_keys - new_keys)
        extra_in_new = sorted(new_keys - old_keys)
        differing_values = sorted(
            k
            for k in old_keys & new_keys
            if isinstance(old_payload, dict)
            and isinstance(new_payload, dict)
            and old_payload[k] != new_payload[k]
        )
        topic_identical = (
            not missing_in_new and not extra_in_new and not differing_values
        )
        if not topic_identical:
            byte_identical = False
        result["per_topic"][topic] = {
            "identical": topic_identical,
            "fields_missing_in_new": missing_in_new,
            "fields_extra_in_new": extra_in_new,
            "fields_differing_values": differing_values,
            "old_partition_key": old_by_topic[topic][0].key,
            "new_partition_key": new_by_topic[topic][0].key,
            "partition_key_identical": (
                old_by_topic[topic][0].key == new_by_topic[topic][0].key
            ),
        }
        if old_by_topic[topic][0].key != new_by_topic[topic][0].key:
            byte_identical = False

    result["byte_identical"] = byte_identical
    return result


# =============================================================================
# Main
# =============================================================================


async def main_async(out_path: Path) -> int:
    import yaml

    with open(REGISTRY_PATH, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    events_raw = raw.get("events", {})

    event_types: list[tuple[str, dict[str, object]]] = []
    for event_type, event_def in events_raw.items():
        required = event_def.get("required_fields", [])
        payload = build_synthetic_payload(event_type, required)
        event_types.append((event_type, payload))

    total_event_types = len(event_types)
    print(f"Loaded {total_event_types} event types from {REGISTRY_PATH}")

    tmp_root = Path(tempfile.mkdtemp(prefix="omn-15971-shadow-parity-"))
    try:
        old_by_event, old_errors = await run_old_path(event_types, tmp_root)
        new_by_event, new_errors = await run_new_path(event_types, tmp_root)
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    diffs = [
        diff_event_type(et, old_by_event.get(et, []), new_by_event.get(et, []))
        for et, _ in event_types
    ]

    identical_count = sum(1 for d in diffs if d["byte_identical"])
    mismatched = [d for d in diffs if not d["byte_identical"]]

    old_total_published = sum(len(v) for v in old_by_event.values())
    new_total_published = sum(len(v) for v in new_by_event.values())

    report = {
        "proof_class": "replay-proven",
        "scope": (
            "Hermetic in-process capture against a fake broker recorder on "
            "both sides; no live Kafka, no live daemon socket process. "
            "Proves/disproves code-path parity as of the current dev HEAD, "
            "not deployed-runtime parity."
        ),
        "generated_at": datetime.now(UTC).isoformat(),
        "total_event_types": total_event_types,
        "old_path_publish_count": old_total_published,
        "new_path_publish_count": new_total_published,
        "old_path_errors": old_errors,
        "new_path_errors": new_errors,
        "byte_identical_event_types": identical_count,
        "mismatched_event_types": len(mismatched),
        "diffs": diffs,
    }

    out_path.write_text(json.dumps(report, indent=2, default=str))

    print()
    print("=" * 78)
    print("OMN-15971 shadow-mode parity proof -- SUMMARY")
    print("=" * 78)
    print(f"Event types compared:        {total_event_types}")
    print(f"Old path publish count:      {old_total_published}")
    print(f"New path publish count:      {new_total_published}")
    print(f"Old path errors:             {len(old_errors)}")
    print(f"New path errors:             {len(new_errors)}")
    print(f"Byte-identical event types:  {identical_count}/{total_event_types}")
    print(f"Mismatched event types:      {len(mismatched)}/{total_event_types}")
    print()
    if mismatched:
        # Aggregate the mismatch reasons for a compact summary.
        reason_counts: dict[str, int] = {}
        for d in mismatched:
            for info in d["per_topic"].values():
                for fld in info["fields_missing_in_new"]:
                    reason_counts[f"missing_field:{fld}"] = (
                        reason_counts.get(f"missing_field:{fld}", 0) + 1
                    )
                for fld in info["fields_differing_values"]:
                    reason_counts[f"differing_value:{fld}"] = (
                        reason_counts.get(f"differing_value:{fld}", 0) + 1
                    )
                if not info["partition_key_identical"]:
                    reason_counts["partition_key_mismatch"] = (
                        reason_counts.get("partition_key_mismatch", 0) + 1
                    )
        print("Top mismatch reasons (topic-occurrences):")
        for reason, count in sorted(reason_counts.items(), key=lambda kv: -kv[1])[:15]:
            print(f"  {count:4d}  {reason}")
    print()
    print(f"Full diff artifact written to: {out_path}")
    print("=" * 78)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(
            "/tmp/omn-15971-shadow-parity-report.json"
        ),  # local-path-ok: script output default, overridable via --out
        help="Path to write the JSON diff artifact",
    )
    args = parser.parse_args()
    return asyncio.run(main_async(args.out))


if __name__ == "__main__":
    raise SystemExit(main())
