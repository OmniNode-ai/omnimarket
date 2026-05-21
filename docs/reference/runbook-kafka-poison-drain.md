# Runbook: Kafka Poison Record Drain — node_pr_lifecycle_orchestrator

**Topic:** `onex.cmd.omnimarket.pr-lifecycle-orchestrator-start.v1`
**Ticket:** OMN-9216
**Guard:** `omniclaude/plugins/onex/hooks/scripts/post_tool_use_kafka_poison_message_guard.sh`

---

## Symptom

The PostToolUse crash-escalation guard (OMN-9085) fires on `rpk group describe`
or similar Bash output, recording a friction YAML at
`$ONEX_STATE_DIR/friction/kafka_poison/`. The pattern `unicode_decode_consumer_groups`
matches because the prod consumer group name embeds `describe_consumer_groups`
and the MEMBER-ID field contains `aiokafka`.

**This may be a false positive.** Verify the broker state before any drain operation.

---

## Diagnosis

```bash
# 1. Confirm broker state (run on 192.168.86.201)
PROD_GROUP="prod.omnimarket.pr_lifecycle_orchestrator.consume.1.0.0.__i.prod-effects.__t.onex.cmd.omnimarket.pr-lifecycle-orchestrator-start.v1"
docker exec omnibase-infra-prod-redpanda rpk group describe "$PROD_GROUP"
```

Interpret the output:

| Condition | Meaning |
|-----------|---------|
| `LOG-END-OFFSET=0` on all partitions | Topic empty — no poison record on broker |
| `PRIOR-OFFSET=-1` on all partitions | Consumer has never committed — aiokafka `auto_offset_reset=latest` |
| `LAG > 0` on any partition | Unprocessed records exist; may include a poison record |
| `UnicodeDecodeError` in runtime container logs | Real decode crash — proceed to drain |

If LOG-END-OFFSET=0 and PRIOR-OFFSET=-1 across all partitions, the topic is clean.
The guard fired as a false positive. No drain is needed. Document the false positive
and move to the classifier fix (see OMN-9216).

---

## Drain Procedure (real poison record present)

### Option A: Seek to end (requires empty group — no active consumers)

```bash
# Stop the runtime-effects container to release the consumer group
docker stop omninode-prod-runtime-effects

# Wait for group to report no members
docker exec omnibase-infra-prod-redpanda rpk group describe "$PROD_GROUP"
# Confirm MEMBERS=0

# Seek all partitions to end
docker exec omnibase-infra-prod-redpanda \
  rpk group seek "$PROD_GROUP" --to end

# Verify: CURRENT-OFFSET should match LOG-END-OFFSET, LAG=0
docker exec omnibase-infra-prod-redpanda rpk group describe "$PROD_GROUP"

# Restart the container (RestartPolicy=unless-stopped handles auto-restart)
docker start omninode-prod-runtime-effects
```

### Option B: Seek past the specific poison offset (active group blocked on one offset)

```bash
# Find the stalled partition and offset from rpk group describe output
# Then write an offset file advancing past the bad record:
cat > /tmp/seek_offsets.txt <<EOF
onex.cmd.omnimarket.pr-lifecycle-orchestrator-start.v1 <PARTITION> <POISON_OFFSET+1>
EOF

# Stop the consumer, seek, restart (same as Option A flow)
docker stop omninode-prod-runtime-effects
docker exec omnibase-infra-prod-redpanda \
  rpk group seek "$PROD_GROUP" --to-file /tmp/seek_offsets.txt
docker start omninode-prod-runtime-effects
```

### Option C: Delete and recreate topic (idempotent command stream)

The pr-lifecycle-orchestrator-start topic carries **idempotent commands** — each
sweep run generates a fresh `correlation_id`. Replaying or dropping old commands
has no state side effects beyond missing a sweep run.

```bash
# Stop consumer
docker stop omninode-prod-runtime-effects

# Delete topic (Redpanda will honour retention.ms=604800000 otherwise)
docker exec omnibase-infra-prod-redpanda \
  rpk topic delete onex.cmd.omnimarket.pr-lifecycle-orchestrator-start.v1

# Recreate with same partition count and replication factor
docker exec omnibase-infra-prod-redpanda \
  rpk topic create onex.cmd.omnimarket.pr-lifecycle-orchestrator-start.v1 \
    --partitions 6 --replicas 1

# Restart consumer
docker start omninode-prod-runtime-effects
```

---

## Verification

After any drain operation:

```bash
# Group should show LAG=0 (or LAG='-' on empty topic) and no error column entries
docker exec omnibase-infra-prod-redpanda rpk group describe "$PROD_GROUP"

# Runtime-effects container should be healthy
docker ps --filter name=omninode-prod-runtime-effects --format "{{.Status}}"

# Guard should not fire on subsequent rpk group describe calls
# Run describe and confirm no friction file is created in:
ls $ONEX_STATE_DIR/friction/kafka_poison/
```

---

## False Positive: Guard Fires on Clean Topic

The `unicode_decode_consumer_groups` classifier pattern matches when Bash tool
output contains both `aiokafka` and `describe_consumer_groups` within 400
characters. This occurs legitimately on:

- `rpk group describe` output (MEMBER-ID contains `aiokafka`, group name contains
  the topic path which includes `describe_consumer_groups` as a substring of the
  group convention)
- `git show` of commit `e089337b8` (OMN-9085 commit message body)

These are false positives. The friction file records them but no consumer action
is required. Tracked for classifier narrowing in OMN-9216.

---

## Consumer Configuration

`consumer.py` key settings:

```python
auto_offset_reset="latest"   # joins at end of topic on first poll
enable_auto_commit=False      # commits only after successful message processing
```

A consumer with `auto_offset_reset=latest` and no prior committed offset starts
at the current log end. If the topic is empty when it starts, it will poll
indefinitely with no records delivered — no crash-loop is possible in this state.
