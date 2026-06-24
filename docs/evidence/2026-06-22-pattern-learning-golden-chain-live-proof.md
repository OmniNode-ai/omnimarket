# pattern_learning golden chain: live materialization proof

**Topic:** pattern_learning golden chain broken — no projection consumer for
`pattern-stored.v1`, `pattern_learning_artifacts` never materialized.

**Date:** 2026-06-22
**Lane:** stability-test (proof lane), runtime host — `omnibase-infra-stability-test-*`
**Verifier:** subagent verifier, distinct from the consumer build author.

---

## Summary

The 2026-06-12 diagnosis hit the "consumer already exists but is unwired / not yet built"
branch (decision-tree item 1). Between that diagnosis and this proof, **PR #1210 (merged
to dev)** built the canonical consumer and closed every structural gap. This evidence
packet is the **live, end-to-end materialization proof** on the proof lane, plus the
decision-tree reconciliation audit.

### Decision-tree outcome

1. **Consumer exists** → wiring/verification, not a new build. A canonical projection
   node `node_projection_pattern_learning` (REDUCER) already exists in omnimarket,
   subscribing `onex.evt.omniintelligence.pattern-stored.v1` and UPSERTing
   `pattern_learning_artifacts` by `pattern_id`.
   - Contract: `src/omnimarket/nodes/node_projection_pattern_learning/contract.yaml`
   - Handler: `.../handlers/handler_projection_pattern_learning.py`
     (`HandlerProjectionPatternLearning`)
   - Runtime activation: `ONEX_ACTIVE_RUNTIME_PACKAGES=omnibase_infra,omnimarket`
     (`omnibase_infra/docker/runtime-policy.env`) — the whole `omnimarket` package is
     active, so the node auto-discovers. Live consumer group is **Stable** on the lane
     (see below).
2. **`correlation_id` gap = schema-version divergence, reconciled by migration.** The
   per-node migration
   `.../node_projection_pattern_learning/migrations/0000_create_pattern_learning_artifacts.sql`
   creates the table **with `correlation_id TEXT`** and carries an idempotent
   `ALTER TABLE ... ADD COLUMN IF NOT EXISTS correlation_id TEXT` that backfills any
   pre-existing legacy (omnibase_infra 064-era) deployment that lacked the column. The
   infra-vendored copy at
   `omnibase_infra/docker/migrations/forward/nodes/node_projection_pattern_learning/0000_create_pattern_learning_artifacts.sql`
   is **byte-identical** (verified via `diff` → IDENTICAL). No separate divergent
   `pattern_learning_artifacts` migration remains in `omnibase_infra/docker/migrations`.
3. **Chain registered** in
   `src/omnimarket/nodes/node_golden_chain_sweep/golden_chains.yaml`:
   `pattern_learning` → head `onex.evt.omniintelligence.pattern-stored.v1`
   → tail `pattern_learning_artifacts`.

---

## Live proof — real `pattern-stored.v1` event materializes a row

### 1. Pre-state (proof lane)

- Table present in `omnidash_analytics` with the **correct schema including
  `correlation_id`**:
  ```
  $ docker exec omnibase-infra-stability-test-postgres psql -U postgres \
      -d omnidash_analytics -tAc "SELECT count(*) FROM information_schema.columns \
      WHERE table_name='pattern_learning_artifacts' AND column_name='correlation_id';"
  1
  ```
- Live consumer group **Stable**, subscribed to the head topic:
  ```
  stability-test.omnimarket.projection_pattern_learning.consume.1.0.0.__i.stability-test-main.__t.onex.evt.omniintelligence.pattern-stored.v1   Stable
  ```
- Row count before publish: `0`.

### 2. Publish a real event

Published a canonical `pattern-stored.v1` event to the proof-lane Redpanda topic
(`onex.evt.omniintelligence.pattern-stored.v1`) carrying the projection event fields:

```json
{"pattern_id":"11111111-1111-4111-8111-111111110001",
 "pattern_name":"omn13102-proof-pattern",
 "pattern_type":"golden_chain_proof",
 "language":"python",
 "lifecycle_state":"candidate",
 "composite_score":0.91,
 "scoring_evidence":{"source":"pattern_learning live proof"},
 "signature":{"shape":"proof"},
 "correlation_id":"22222222-2222-4222-8222-222222220001"}
```

```
Produced to partition 0 at offset 2 with timestamp 1782146076146.
```

> Note on envelope shape: the projection auto-wiring path
> (`omnibase_infra.runtime.auto_wiring.handler_wiring`) passes the message body
> **directly** to `HandlerProjectionPatternLearning` as `input_data` — the projection
> event fields must be at the top level of the produced JSON, not nested under
> `payload`. A first attempt nesting fields under `payload` was correctly **rejected**
> by the live handler with `ValidationError: pattern_id Field required`, confirming the
> consumer validates inbound events against `ModelPatternStoredEvent` rather than
> silently accepting malformed input.

### 3. Post-state — row materialized

```
$ docker exec omnibase-infra-stability-test-postgres psql -U postgres \
    -d omnidash_analytics -tAc "SELECT pattern_id, pattern_name, pattern_type, \
    composite_score, correlation_id, projected_at FROM pattern_learning_artifacts \
    WHERE pattern_id='11111111-1111-4111-8111-111111110001';"

11111111-1111-4111-8111-111111110001|omn13102-proof-pattern|golden_chain_proof|0.910000|22222222-2222-4222-8222-222222220001|2026-06-22 16:34:36.148986+00
```

Consumer group offset advanced and lag returned to zero (message consumed end-to-end):

```
TOPIC                                        PARTITION CURRENT-OFFSET LOG-END-OFFSET LAG
onex.evt.omniintelligence.pattern-stored.v1  0         3              3              0
```

---

## DoD reconciliation

| DoD item | Status | Evidence |
|---|---|---|
| `golden_chain_sweep` counts `pattern-stored.v1 → pattern_learning_artifacts` as wired | MET | Chain registered in `golden_chains.yaml`; live Stable consumer group on head topic |
| A real `pattern-stored.v1` event materializes a row on the proof lane | MET (this packet) | Row UPSERTed with `correlation_id` after publish; consumer offset advanced, lag 0 |
| Evidence file under `docs/evidence/` | MET | This file |
| Divergent infra `pattern_learning_artifacts` (missing `correlation_id`) reconciled | MET | Migration ships `correlation_id` + `ADD COLUMN IF NOT EXISTS` backfill; infra-vendored copy byte-identical; live schema has the column |

## Provenance

The omnimarket-side build (node + handler + migration + contract + `golden_chains.yaml`
entry) and the infra-vendored migration landed via **PR #1210** on dev.
This evidence file supplies the missing **live materialization proof** on the proof lane
and the decision-tree reconciliation audit. Unit suite for the node + golden-chain tests: 23 passed
(`src/omnimarket/nodes/node_projection_pattern_learning/tests/`,
`tests/test_golden_chain_pattern_learning.py`,
`tests/test_golden_chain_projection_pattern_learning.py`).
