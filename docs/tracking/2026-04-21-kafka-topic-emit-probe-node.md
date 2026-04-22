# Kafka Topic Emit Probe Node (OMN-9404)

## Overview

Node: `node_kafka_topic_emit_probe`
Purpose: Hourly synthetic event emission for Kafka topic health verification.

## Architecture

- **Type**: Compute node (`node_type: compute`)
- **Purity**: Impure (produces side-effects via event publishing on each invocation)
- **Idempotent**: No — each invocation emits new synthetic events to Kafka
- **Timeout**: 120 seconds

## Input Schema

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `topics` | `list[str]` | `[]` | Kafka topics to probe (defaults to all declared topics) |
| `probe_interval_seconds` | `int` | `3600` | Interval between probes per topic |
| `verify_consumers` | `bool` | `true` | Verify consumer group advancement |

## Output Schema

| Field | Type | Description |
|-------|------|-------------|
| `probes_emitted` | `int` | Number of synthetic events published |
| `consumers_advanced` | `int` | Number of consumer groups observed advancing |
| `failures` | `list[str]` | Topics/groups that failed verification |

## Event Flow

- **Subscribes to**: `onex.cmd.omnimarket.kafka-probe-trigger.v1`
- **Publishes to**: `onex.evt.omnimarket.kafka-probe-result.v1`
- **Terminal event**: `onex.evt.omnimarket.kafka-probe-complete.v1`

## Integration Notes

- **Dependencies**: `omnibase_core>=0.39.0` (event bus, state store)
- **Entry point**: `omnimarket.nodes.node_kafka_topic_emit_probe`
- **Runtime**: Compatible with RuntimeLocal and full runtime
- **Network**: Requires network (Kafka connectivity)

## Error Handling

- Failures are captured in `failures` output list
- Per-topic exception handling prevents cascade failures
- Timeout enforced at 120 seconds

## Testing

Unit tests provided in `tests/unit/nodes/test_node_kafka_topic_emit_probe.py` covering:
- Handler initialization
- Empty topic list (default behavior)
- Specific topic probing
