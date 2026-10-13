CiRedTriage: design note and TLA+ model for red CI triage over the bus (OMN-20730)
==================================================================================

Model-before-build for the durable red CI triage contract. A red CI run emits a
bus event; the sweep orchestrator triages it on receipt; polling stays as the
backstop. This note fixes the scope, the record identity, the one writer and the
checkpoints. CiRedTriage.tla checks them. The node changes that implement them are
a later deliverable and are not part of this directory.

Scope
-----

* One concept per topic: onex.evt.omnimarket.ci-run-failed.v1 carries red CI runs
  only. Classification, cause clustering and owner leases stay where they are
  (node_pr_lifecycle_triage_compute, node_pr_lifecycle_orchestrator). This
  contract is about delivery: each visible red run is triaged exactly once, its
  owner start is applied exactly once, and none is lost.
* In scope: the detector's publish (watcher stream and check-run webhook stream),
  the triage consumer and its retries, the writer of the triage record, the start
  dispatcher, the poll backstop, and crash, restart and replay of each of them.
* The start's receiver is in scope only through one assumption: it applies a
  start and records the start's identity in one durable step, and a start whose
  identity it has recorded is a no-op. Its own crash and recovery are not
  modelled; the later deliverable provides that step and tests it.
* Visibility: a red stops needing triage when it stops being visible in CI state
  (a rerun went green, the head moved, the PR closed). "No lost red" is promised
  for every red until it is triaged or stops being visible. The poll reads the
  current CI state only, so it cannot recover a red that is no longer visible.
* Out of scope: what a triage decides, the owner-claim TTL, and a red check that
  no Actions workflow run carries (another app's check, run_id 0). That check has
  no canonical identity under this note; the later deliverable names its key
  before it handles one.

Record identity
---------------

The canonical identity of a red is

    (repo, workflow_run_id, run_attempt, check_name)

* A rerun is a new run_attempt and so a new identity. A rerun that goes red again
  is triaged again; it is not absorbed by the earlier attempt's record.
* A repeated publish of the same red (a detector restart re-derives every red it
  still sees; the watcher re-observes every tick) is the same identity.
* The event's own id, the observation time and the set of other failing checks
  are not part of the identity.

The one writer, and column ownership
------------------------------------

* One triage record per identity, written only by the projection's writer
  (node_pr_lifecycle_state_reducer) as an insert-if-absent on the identity: an
  UPSERT that does nothing on conflict.
* Column ownership: the inserting path sets the record's triage columns (its
  source and its start intent) once. After that only the dispatcher writes the
  record, and only its start status (pending to sent). A later triage write for
  the same identity, from either path, changes nothing. An UPSERT that updates on
  conflict would let one path rewrite a record the other path made, and reset a
  sent start to pending.
* The dedup authority is the durable record. In-process caches (the detector's
  CiRedIndex.emitted, the triage handler's seen-event and decided caches) only
  save work; losing them on restart must change nothing.

The owner start
---------------

* The record carries its owner start as a durable intent (an outbox column),
  written in the same insert. A start follows from the insert that created the
  record, never from a read that found no record.
* A dispatcher sends each pending start and then marks it sent. A crash between
  the two leaves it pending, so it is sent again: delivery of a start is at least
  once.
* The receiver applies one start per identity, keyed by the start's correlation
  id, which is derived from the identity. That makes the second send a no-op.
* A start held only in process memory between the insert and the send is lost
  with the process.

Checkpoints (the durable-queue contract)
----------------------------------------

* Acknowledgement: the triage consumer commits its topic offset only after the
  writer's insert-or-skip for that event is durable. A committed offset is a
  promise that the record behind it exists, or that the event is recorded as
  dead-lettered.
* Retry: a writer failure leaves the offset uncommitted, so the event is
  delivered again. Delivery is at least once; the writer makes it exactly once
  in effect.
* Retry exhaustion: each event has its own retry count. Once that event's count
  reaches the limit, the consumer may record it as dead-lettered, then commit past
  it; an event that has not failed is never dead-lettered. The model also lets a
  later retry succeed after the limit; it does not require dead-lettering at the
  limit. The poll recovers the red behind a dead-lettered event.
* Restart: the consumer resumes from its committed offset and replays every
  event after it. A replayed event whose record exists is a no-op.
* Poll backstop: on each tick it reads the current CI state for visible red
  identities with no triage record and writes each missing one through the same
  writer, with the same key. Its snapshot may be stale by the time it writes;
  that is safe only because the writer re-checks the key at write time. The poll
  never starts an owner by itself; its insert carries the start intent like any
  other. The poll is the only guaranteed recovery for a publish that never
  reached the topic and for a dead-lettered event; a detector restart may publish
  the red again, but nothing guarantees it.

Properties
----------

| Id  | Property              | Kind      | Mutant (one design knob)                     | Config                     |
| --- | --------------------- | --------- | -------------------------------------------- | -------------------------- |
| P1  | OneTriagePerIdentity  | invariant | KeyMode="delivery": a key per delivery       | M2_key_delivery            |
| P1  | OneTriagePerIdentity  | invariant | WriteMode="blind_insert"                     | M3_blind_insert            |
| P1  | OneTriagePerIdentity  | invariant | DurableKeys=FALSE: dedup in process memory   | M4_volatile_dedup          |
| P1s | OneStartPerIdentity   | invariant | ReceiverDedup=FALSE                          | M10_receiver_no_dedup      |
| P2  | NoLostRed             | liveness  | KeyMode="head": a key without the attempt    | M1_key_head                |
| P2  | NoLostRed             | liveness  | PollOn=FALSE: no backstop                    | M7_no_poll                 |
| P2s | NoLostStart           | liveness  | OutboxStart=FALSE: start held in memory      | M9_start_from_memory       |
| P3  | PollNeverDoubleWrites | invariant | PollViaWriter=FALSE: the poll's own insert   | M6_poll_own_writer         |
| P4  | AckedIsDurable        | invariant | AckBeforeWrite=TRUE                          | M5_ack_before_write        |
| P4  | AckedIsDurable        | invariant | DeadLetterDurable=FALSE                      | M11_dead_letter_unrecorded |
| P5  | ColumnOwnership       | invariant | WriteMode="upsert_update"                    | M8_upsert_update           |

* P1, one triage per canonical run identity: no identity ever holds more than one
  inserted record.
* P1s, one applied owner start per identity, at the start's receiver.
* P2, no lost red: every red identity is eventually triaged or stops being
  visible.
* P2s, no lost start: every triaged identity's start is eventually applied by
  the receiver.
* P3, the poll backstop never double-writes: the poll never writes a record for
  an identity that already holds one.
* P4, a committed offset is durable: every event at or below the committed offset
  has its record or is recorded as dead-lettered.
* P5, column ownership: no triage write, from either path, changes a column of
  an existing record; only the dispatcher moves its start status.

CiRedTriage_design checks all of them and must pass. Each mutant config flips
exactly one constant of the design config, checks only the property it targets,
and must produce a counterexample to it.

What the recorded counterexamples show
--------------------------------------

* M1_key_head: one attempt is triaged by the poll under the head's key. The other
  attempt of the same run goes red, its publish is lost, and the poll finds the
  head's key present and skips it. That attempt stays visible and is never
  triaged.
* M2_key_delivery and M3_blind_insert: the event path and the poll each insert a
  record for one red.
* M4_volatile_dedup: the consumer writes, crashes before its commit, restarts
  with an empty dedup set and replays the event into a second record.
* M5_ack_before_write: the offset passes an event whose record does not exist.
* M6_poll_own_writer: the poll scans, the event path writes the record, the poll
  then inserts from its stale snapshot.
* M7_no_poll: the event for one red fails once, is dead-lettered and committed
  past; that red stays visible and is never triaged. A lost publish reaches the
  same end.
* M8_upsert_update: one path inserts a record; the other path's write for the
  same identity then rewrites its source column.
* M9_start_from_memory: the record is inserted with its start held in memory;
  the dispatcher crashes; the start never reaches the receiver.
* M10_receiver_no_dedup: the dispatcher sends a start and crashes before marking
  it sent; it sends it again and the receiver applies both.
* M11_dead_letter_unrecorded: a failed event is committed past without a
  dead-letter record.

The present code against this note
----------------------------------

Read from omnimarket dev at 69fcd9778. The later deliverable closes each gap.

* Identity: the decision key is repo#pr@head:check
  (handler_ci_red_triage.py), which has no workflow_run_id and no run_attempt.
  That is the M1_key_head shape: a rerun that goes red again at the same head is
  not triaged again. The red-CI facts carry run_id but no run_attempt today.
* Writer and start: the triage handler awaits the decision and owner claim reads
  from the projection (handler_ci_red_triage.py, the _was_decided call in handle
  and the _owner_run call in _decide). It then builds the decision and start
  events, fills its in-process caches (owners, decided, seen events) and returns
  the events for publication. The reducer writes the projection later, from the
  published decision. Two deliveries handled concurrently, in one process or in
  two, can both read no claim and both start. That is the read-then-write shape
  that M3, M6 and M10 refute; the start must follow from the writer's insert, and
  its receiver must deduplicate.
* The caches are filled before the events are published. If publication then
  fails, a redelivery to the same process is answered from the seen-event cache
  with no events, while no decision is durable. That is the M4 shape: process
  memory acting as the dedup authority.
* Detector dedup is in process memory and empty after a restart. That is safe
  only while the writer, not the cache, is the dedup authority (M4).
* Acknowledgement and retry: when the runtime's consumer commits the offset
  relative to the handler's write, and what it does when a handler keeps failing,
  were not read in this lane. The later deliverable tests both against P4.
* Poll backstop: the watcher stream (pr-state-observed) already reaches the same
  topic, consumer and writer. Any backstop that starts owners outside this writer
  is a second writer (M6).

Modelling notes
---------------

* Identities: a1 and a2 are two attempts of one workflow run at one head (a rerun
  that went red again). Bounds: one extra publish per identity (MaxDup), one lost
  publish (MaxDrop), one crash of the consumer, the poll or the dispatcher
  (MaxCrash), one writer failure in a behaviour (MaxFail), and a retry limit of
  one failure per event (RetryLimit). The design config explores 1260354
  distinct states.
* Adversarial, never assumed to happen: CI turning red, a red stopping being
  visible, a re-publish, a lost publish, a writer failure, a dead-letter, and a
  crash. Weakly fair: the detector's first publish attempt of a visible red (it
  re-derives every red on each watcher tick), the consumer's read, write and
  commit, its restart, the dispatcher's send and mark, and the poll's scan and
  writes.
* Fairness of the write assumes the writer eventually succeeds. Writer failures
  are bounded; an outage that never ends is out of scope, and the poll, which
  writes through the same writer, would not recover from it either.
* The poll's scan and its writes are separate steps, so the event path can write
  between them.
* Not modelled: classification, cause clustering, owner leases and the content of
  a decision. Partition order across identities is not assumed.

Run
---

tests/formal/ci_red_triage_tlc.sh runs the matrix and writes results/: each
config's raw TLC output and SUMMARY.txt, which records the sha256 of the model,
of each config and of each config's output beside its result. It uses
TLA2TOOLS_JAR when set, or fetches the pinned tla2tools.jar (TLC 2.19, release
v1.7.4); the sha256 is checked either way. Java comes from JAVA, PATH, or the
eclipse-temurin:17-jre-alpine image.

tests/formal/test_ci_red_triage_model.py fails when the model, a config or an
output changes without a re-run, when a recorded result or a mutant's own output
does not show the expected result, when a mutant flips more than one knob, and
when VERDICT.txt does not hold exactly one verdict line approving the committed
model's digest.
