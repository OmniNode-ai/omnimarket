# LedgerReducerJudge: TLA+ model of the work ledger reducer as the single writer (OMN-21949, plan task L9)

Model-before-build for the shadow reducer, the guard equivalence replay and the flip (L10 to L13).
Design source: the work ledger bus-only cutover plan, section 4 (write, serialization, order,
guards) and task L9. Adopts OMN-20537 and OMN-19386. Base: the earlier LedgerBusAppend
model, where the judge is a file writer under an fcntl lock with a counter file; here the judge is
the reducer, and the file is gone.

## What is modelled

Writers: one per principal (a runtime tenant). A writer publishes an append command on its own
ingress topic of the reducer's bus and replays it under the same `req=` id while it has no
receipt, so a command can sit in the log more than once.

Reducer: one worker per ingress topic (per-tenant topics are separate partitions of one consumer
group), so the commands of two principals can be in flight at once. Each worker runs, with a crash
possible before every step:

    Fetch -> Lock -> Guard -> Write -> Commit -> CommitOffset

- Lock: the per-ledger advisory lock, taken inside the transaction.
- Guard: a pure function of (command, state). First the `req=` record table (a recorded req gets
  its stored answer: `duplicate` for an accepted row, `refused` for a refused one), then the rules
  over the rows the reducer holds. The one rule modelled: two rows with the same index from
  different principals conflict, and the second is refused (`ConflictOn`).
- Write: the transaction body. The ledger row with `ledger_seq = last seq + 1`, the `req=` record,
  and the outbox entries: the accepted row event, then the receipt, built from the row just written.
  A refusal and a duplicate write a receipt only.
- Commit: row, record and outbox become visible together; the lock is released. A crash before it
  drops the whole body and releases the lock. A crash after it and before CommitOffset redelivers
  the command, which the guard answers as a duplicate.

Relay: a separate process. It publishes the lowest unpublished outbox entry to the accepted-row
topic (`events`) or the receipt topic (`receipts`), then marks it published. It may crash between
publish and mark and then republishes that entry. Projection: consumes `events` in order, so it lags
the ledger.

## Properties

| Id | Property | What it says |
| -- | -- | -- |
| P1 | `NoDuplicateRow` | no row appears twice in the ledger: a replayed `req=` adds nothing |
| P2 | `SeqGapless` | the assigned `ledger_seq` values are exactly 1..n |
| P3 | `SeqMatchesEventOrder` | the ledger is in seq order, the accepted-row topic carries the events in that order, complete from 1 (a relay republish only repeats an event), each event is the row the ledger holds at that seq |
| P4 | `PrincipalInterleavingSerial` | at most one worker is inside a transaction, and a transaction in flight holds the next seq |
| P5 | `ReceiptTruth` | a published `accepted` or `duplicate` receipt has its committed row at that seq; a `refused` receipt has no row |
| P6 | `GuardSeesAllPriorRows` | while a transaction is judging, the rows its guard read are exactly the rows committed |
| P7 | `GuardEffective` | what the guard is for: two conflicting rows never both land |
| L1 | `EveryRequestAnswered` | a published request is eventually answered by a published receipt (weak fairness on writers, workers, relay, delivery; crashes are bounded) |
| L2 | `EveryCommittedRowPublished` | a committed row eventually has its event on the accepted-row topic, across relay crashes |

## Configurations

The runner runs all of them and checks each result against its expected outcome.

| Config | Knob changed from adopted | Expected |
| -- | -- | -- |
| `LedgerReducerJudge_adopted` | none: two principals, two crashes in any gap, one relay crash, one replay per row | P1-P7, L1, L2 hold |
| `LedgerReducerJudge_adopted_conflict` | `ConflictOn=TRUE`: the second of two conflicting rows is refused | P1-P7, L1, L2 hold |
| `C1_receipt_before_commit` | the receipt is published before the transaction commits | `ReceiptTruth` violated |
| `C2_seq_outside_tx` | `ledger_seq` drawn from a counter outside the transaction when the guard accepts | `SeqGapless` violated |
| `C3_guard_lagging_projection` | the guard reads the projection, not the reducer's own rows | `GuardSeesAllPriorRows` violated |
| `C3b_guard_lagging_projection_effect` | same, checking what it costs | `GuardEffective` violated |
| `C4_no_lock` | no per-ledger lock | `PrincipalInterleavingSerial` violated |
| `C5_relay_unordered` | the relay publishes any unpublished entry, not the lowest | `SeqMatchesEventOrder` violated |
| `C6_no_dedup` | the guard does not check the `req=` record table | `NoDuplicateRow` violated |
| `C7_commit_offset_first` | the offset is committed at fetch, and the writer does not replay | `EveryRequestAnswered` violated |
| `W1_both_rows_committed` | witness, no crash, replay or relay crash | `WitnessBothRowsCommitted` violated: both rows reach the ledger |
| `W2_duplicate_after_crash` | witness, no writer replay | `WitnessDuplicateReceipt` violated: a crash between commit and offset commit redelivers, and the guard answers `duplicate` |
| `W3_duplicate_after_replay` | witness, no crash | `WitnessDuplicateReceipt` violated: a writer replay is answered `duplicate` |
| `W4_refused_receipt` | witness on the conflict base, no crash or replay | `WitnessRefusedReceipt` violated: the second conflicting row is refused |
| `W5_event_republished` | witness, no worker crash or replay | `WitnessEventRepublished` violated: a relay crash between publish and mark repeats an event |

The witnesses are the positive controls of the adopted run: each shows, by a counterexample to a
`Witness...` invariant, that the adopted bounds reach the path an invariant guards, so a pass is
not a pass over a model that never gets there.

The three controls the plan names are C1, C2 and C3. C3b and C4 to C7 make the other invariants
falsifiable. Each control cfg lists the invariant it is meant to break, plus `TypeOK`, so the
counterexample cannot be another invariant's.

## Abstractions and what is out of scope

- One database transaction is one atomic `Commit` step; what happens inside the database engine
  (isolation levels, the advisory lock's own implementation) is assumed, not modelled. A crash
  loses the uncommitted body and releases the lock.
- Per-tenant ingress topics are one log and one committed offset per worker; broker partition
  assignment and consumer-group rebalances (a zombie consumer after a rebalance) are not modelled.
  The per-ledger lock is what stands in for them: `C4_no_lock` shows what it is for.
- The request id is the row id (a writer replays under the same id). A writer that re-sends the
  same row under a new `req=` is not modelled.
- The receipt reaches the writer through `receipts`; a lost receipt is the writer's replay, which
  the guard answers from the record table. Broker acknowledgements are not modelled.
- The guard model has one state rule (conflict). The real guards (M9 of the plan) are not
  enumerated; the model proves the reducer shape any pure guard over its own state needs.
- Bounds: two principals, one row each, two worker crashes, one relay crash, one replay per row.
  The adopted run is exhaustive within them. It says nothing about larger bounds.
- The stateless writer-side pre-checks (stranded clone, worktree cell), the citation table, the
  readers and the exporter are not part of this model.

## Review

The verdict (VERDICT.txt) is written by the review lane, which is neither the modelling lane nor
the 2026-10-04 reviewer. It is not part of this change.

## Run

    python3 tests/formal/ledger_reducer_judge_tlc.py     # on a lab host; needs java or docker; fetches the pinned jar

Results are written to `results/<cfg>.out` and `results/SUMMARY.txt`. The exit status is 0 only
when both adopted configs pass and every control and witness is rejected on its named property.
`tests/formal/test_ledger_reducer_judge_model.py` reads the results.
