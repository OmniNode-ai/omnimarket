---------------------------- MODULE CiRedTriage ----------------------------
(***************************************************************************)
(* Red CI triage over the bus with a polling backstop (OMN-20730).         *)
(*                                                                         *)
(* A red CI run is one canonical identity                                  *)
(*   (repo, workflow_run_id, run_attempt, check_name).                     *)
(* The detector publishes a ci-run-failed event for it; the publish can be *)
(* lost and can be repeated (a detector restart re-derives every red it    *)
(* still sees). The triage consumer reads the topic from its committed     *)
(* offset, writes the triage record through the one writer, then commits.  *)
(* A write can fail; the event is then delivered again, and once its own   *)
(* retries are spent the consumer dead-letters it. The poll backstop       *)
(* scans CI state for visible red identities with no triage record and     *)
(* writes the missing ones through the same writer. A record carries its   *)
(* owner start as a durable intent; a dispatcher sends the start, and the  *)
(* start's receiver deduplicates by identity. The receiver is assumed to   *)
(* apply a start and record its identity in one durable step; its own      *)
(* crash and recovery are outside this model.                              *)
(*                                                                         *)
(* Runs: a1 and a2 are two attempts of one workflow run at one PR head (a  *)
(* rerun that went red again).                                             *)
(*                                                                         *)
(* Each design knob below is one decision of the design note; every mutant *)
(* config flips exactly one of them and must violate the property it       *)
(* protects.                                                               *)
(***************************************************************************)
EXTENDS Naturals, Sequences, FiniteSets

CONSTANTS
    KeyMode,           \* "canonical" | "head" | "delivery": the record key
    WriteMode,         \* "insert_if_absent" | "blind_insert" | "upsert_update"
    DurableKeys,       \* the key check reads the durable store, not process memory
    AckBeforeWrite,    \* the consumer commits the offset before the write
    DeadLetterDurable, \* a dead-lettered event is recorded before its offset is committed
    PollOn,            \* the poll backstop runs
    PollViaWriter,     \* the poll writes through the shared writer
    OutboxStart,       \* the start is dispatched from the record's durable intent
    ReceiverDedup,     \* the start's receiver applies one start per identity
    MaxDup,            \* extra publishes of one red (detector restarts)
    MaxDrop,           \* lost publishes (bus down, emitter crash before send)
    MaxCrash,          \* crashes of the consumer, the poll or the dispatcher
    MaxFail,           \* writer failures in one behaviour (exploration bound)
    RetryLimit         \* failures of one event before it is dead-lettered

Runs == {"a1", "a2"}
HeadOf == [r \in Runs |-> "A"]
PollSrc == 99                     \* the poll's delivery tag in "delivery" mode
None == "none"

\* The writer's key for identity r arriving from source src (a log position,
\* or PollSrc). Every key is a pair so that TLC compares like with like.
KeyOf(r, src) ==
    CASE KeyMode = "canonical" -> <<r, 0>>
      [] KeyMode = "head"      -> <<HeadOf[r], 0>>
      [] KeyMode = "delivery"  -> <<r, src>>

VARIABLES
    red,       \* identities whose CI run went red
    gone,      \* red identities no longer visible in CI state (superseded)
    emits,     \* publish attempts per identity (published or lost)
    log,       \* the ci-run-failed topic: a sequence of identities
    off,       \* the consumer's committed offset
    cur,       \* the log position the consumer holds in flight
    cst,       \* consumer: "idle" | "read" | "written" | "acked" | "down"
    fails,     \* writer failures so far, over the whole behaviour
    retries,   \* failures of the event at the next uncommitted position
    dlq,       \* log positions recorded as dead-lettered
    keys,      \* keys present in the writer's dedup store
    triaged,   \* identities that hold a triage record
    nwrites,   \* triage records inserted per identity
    srcOf,     \* the path that inserted a record: None | "event" | "poll"
    st,        \* a record's start status: None | "pending" | "sent" (dispatcher-owned once set)
    rewrote,   \* a triage write changed a column of an existing record
    pollDup,   \* the poll wrote a record for an identity already triaged
    mem,       \* starts held only in process memory (OutboxStart = FALSE)
    inflight,  \* the start the dispatcher has sent but not yet marked sent
    applied,   \* identities whose start the receiver applied and recorded
    started,   \* starts the receiver applied, per identity
    snap,      \* the poll's snapshot of untriaged visible red identities
    pst,       \* poll: "idle" | "scanned"
    drops,
    crashes

ci == <<red, gone, emits, log, drops>>
consumer == <<off, cur, cst, fails, retries, dlq>>
store == <<keys, triaged, nwrites, srcOf, st, rewrote, pollDup, mem>>
starts == <<inflight, applied, started>>
poll == <<snap, pst>>
vars == <<ci, consumer, store, starts, poll, crashes>>

TypeOK ==
    /\ red \subseteq Runs
    /\ gone \subseteq red
    /\ emits \in [Runs -> 0..(1 + MaxDup)]
    /\ log \in Seq(Runs)
    /\ off \in 0..Len(log)
    /\ cur \in 0..Len(log)
    /\ cst \in {"idle", "read", "written", "acked", "down"}
    /\ fails \in 0..MaxFail
    /\ retries \in 0..MaxFail
    /\ dlq \subseteq 1..Len(log)
    /\ triaged \subseteq Runs
    /\ nwrites \in [Runs -> Nat]
    /\ srcOf \in [Runs -> {None, "event", "poll"}]
    /\ st \in [Runs -> {None, "pending", "sent"}]
    /\ rewrote \in BOOLEAN
    /\ pollDup \in BOOLEAN
    /\ mem \subseteq Runs
    /\ inflight \in Runs \cup {None}
    /\ applied \subseteq Runs
    /\ started \in [Runs -> Nat]
    /\ snap \subseteq Runs
    /\ pst \in {"idle", "scanned"}
    /\ drops \in 0..MaxDrop
    /\ crashes \in 0..MaxCrash

Init ==
    /\ red = {}
    /\ gone = {}
    /\ emits = [r \in Runs |-> 0]
    /\ log = <<>>
    /\ off = 0
    /\ cur = 0
    /\ cst = "idle"
    /\ fails = 0
    /\ retries = 0
    /\ dlq = {}
    /\ keys = {}
    /\ triaged = {}
    /\ nwrites = [r \in Runs |-> 0]
    /\ srcOf = [r \in Runs |-> None]
    /\ st = [r \in Runs |-> None]
    /\ rewrote = FALSE
    /\ pollDup = FALSE
    /\ mem = {}
    /\ inflight = None
    /\ applied = {}
    /\ started = [r \in Runs |-> 0]
    /\ snap = {}
    /\ pst = "idle"
    /\ drops = 0
    /\ crashes = 0

(***************************************************************************)
(* The writer. Insert creates a record whose triage columns (source, start *)
(* intent) are set by the inserting path; after that only the dispatcher   *)
(* changes the start status (pending to sent). Update is an UPSERT that    *)
(* updates on conflict: a triage write that rewrites an existing record.   *)
(***************************************************************************)
Insert(r, k, via) ==
    /\ keys' = keys \cup {k}
    /\ triaged' = triaged \cup {r}
    /\ nwrites' = [nwrites EXCEPT ![r] = @ + 1]
    /\ srcOf' = [srcOf EXCEPT ![r] = via]
    /\ st' = [st EXCEPT ![r] = "pending"]
    /\ rewrote' = rewrote
    /\ pollDup' = (pollDup \/ (via = "poll" /\ r \in triaged))
    /\ mem' = IF OutboxStart THEN mem ELSE mem \cup {r}

Update(r, via) ==
    /\ srcOf' = [srcOf EXCEPT ![r] = via]
    /\ st' = [st EXCEPT ![r] = "pending"]
    /\ rewrote' = (rewrote \/ srcOf[r] # via \/ st[r] # "pending")
    /\ pollDup' = (pollDup \/ via = "poll")
    /\ mem' = IF OutboxStart THEN mem ELSE mem \cup {r}
    /\ UNCHANGED <<keys, triaged, nwrites>>

Write(r, k, via) ==
    CASE WriteMode = "insert_if_absent" ->
            IF k \in keys THEN UNCHANGED store ELSE Insert(r, k, via)
      [] WriteMode = "blind_insert" ->
            Insert(r, k, via)
      [] WriteMode = "upsert_update" ->
            IF k \in keys THEN Update(r, via) ELSE Insert(r, k, via)

(***************************************************************************)
(* CI and the detector. The detector's first publish of a visible red is   *)
(* fair (the watcher re-derives every red each tick); that publish can be  *)
(* lost, and the detector's in-memory index then holds it as emitted, so   *)
(* a later publish happens only after a detector restart (adversarial).    *)
(***************************************************************************)
GoRed(r) ==
    /\ r \notin red
    /\ red' = red \cup {r}
    /\ UNCHANGED <<gone, emits, log, drops, consumer, store, starts, poll, crashes>>

\* A red stops being visible: a rerun went green, the head moved, the PR closed.
Supersede(r) ==
    /\ r \in red \ gone
    /\ gone' = gone \cup {r}
    /\ UNCHANGED <<red, emits, log, drops, consumer, store, starts, poll, crashes>>

Publish(r) ==
    /\ r \in red \ gone
    /\ emits[r] < 1 + MaxDup
    /\ emits' = [emits EXCEPT ![r] = @ + 1]
    /\ log' = Append(log, r)
    /\ UNCHANGED <<red, gone, drops, consumer, store, starts, poll, crashes>>

Drop(r) ==
    /\ r \in red \ gone
    /\ emits[r] < 1 + MaxDup
    /\ drops < MaxDrop
    /\ emits' = [emits EXCEPT ![r] = @ + 1]
    /\ drops' = drops + 1
    /\ UNCHANGED <<red, gone, log, consumer, store, starts, poll, crashes>>

FirstEmit(r) == emits[r] = 0 /\ (Publish(r) \/ Drop(r))
ReEmit(r) == emits[r] > 0 /\ Publish(r)

(***************************************************************************)
(* The triage consumer. Design order: read, write, commit. With            *)
(* AckBeforeWrite the commit precedes the write.                           *)
(***************************************************************************)
Read ==
    /\ cst = "idle"
    /\ off < Len(log)
    /\ cur' = off + 1
    /\ cst' = "read"
    /\ UNCHANGED <<ci, off, fails, retries, dlq, store, starts, poll, crashes>>

Process ==
    /\ \/ cst = "read" /\ ~AckBeforeWrite /\ cst' = "written"
       \/ cst = "acked" /\ AckBeforeWrite /\ cst' = "idle"
    /\ Write(log[cur], KeyOf(log[cur], cur), "event")
    /\ UNCHANGED <<ci, off, cur, fails, retries, dlq, starts, poll, crashes>>

Ack ==
    /\ \/ cst = "written" /\ ~AckBeforeWrite /\ cst' = "idle"
       \/ cst = "read" /\ AckBeforeWrite /\ cst' = "acked"
    /\ off' = cur
    /\ retries' = 0
    /\ UNCHANGED <<ci, cur, fails, dlq, store, starts, poll, crashes>>

\* The write fails; the offset stays where it was, so the event is redelivered
\* and its retry count grows. (After an early commit the event is already
\* behind the offset; the failure loses it.)
WriteFail ==
    /\ cst \in {"read", "acked"}
    /\ fails < MaxFail
    /\ fails' = fails + 1
    /\ retries' = IF cst = "read" THEN retries + 1 ELSE retries
    /\ cst' = "idle"
    /\ UNCHANGED <<ci, off, cur, dlq, store, starts, poll, crashes>>

\* This event's retries are spent: the consumer dead-letters it and moves on.
DeadLetter ==
    /\ cst = "read"
    /\ ~AckBeforeWrite
    /\ retries >= RetryLimit
    /\ dlq' = IF DeadLetterDurable THEN dlq \cup {cur} ELSE dlq
    /\ off' = cur
    /\ retries' = 0
    /\ cst' = "idle"
    /\ UNCHANGED <<ci, cur, fails, store, starts, poll, crashes>>

Crash ==
    /\ cst # "down"
    /\ crashes < MaxCrash
    /\ crashes' = crashes + 1
    /\ cst' = "down"
    /\ UNCHANGED <<ci, off, cur, fails, retries, dlq, store, starts, poll>>

\* A restart resumes from the committed offset. Dedup state that lives only
\* in process memory is gone after it.
Restart ==
    /\ cst = "down"
    /\ cst' = "idle"
    /\ keys' = IF DurableKeys THEN keys ELSE {}
    /\ UNCHANGED <<ci, off, cur, fails, retries, dlq, triaged, nwrites, srcOf, st,
                   rewrote, pollDup, mem, starts, poll, crashes>>

(***************************************************************************)
(* The start dispatcher. It sends a pending start, then marks it sent; a   *)
(* crash between the two leaves it pending, so it is sent again. The send  *)
(* and the receiver's apply-and-record are one step (the receiver          *)
(* assumption above).                                                      *)
(***************************************************************************)
Dispatch(r) ==
    /\ inflight = None
    /\ st[r] = "pending"
    /\ OutboxStart \/ r \in mem
    /\ inflight' = r
    /\ mem' = mem \ {r}
    /\ applied' = applied \cup {r}
    /\ started' = IF ReceiverDedup /\ r \in applied
                  THEN started
                  ELSE [started EXCEPT ![r] = @ + 1]
    /\ UNCHANGED <<ci, consumer, keys, triaged, nwrites, srcOf, st, rewrote,
                   pollDup, poll, crashes>>

MarkSent ==
    /\ inflight # None
    /\ st' = [st EXCEPT ![inflight] = "sent"]
    /\ inflight' = None
    /\ UNCHANGED <<ci, consumer, keys, triaged, nwrites, srcOf, rewrote, pollDup,
                   mem, applied, started, poll, crashes>>

DispatcherCrash ==
    /\ inflight # None \/ mem # {}
    /\ crashes < MaxCrash
    /\ crashes' = crashes + 1
    /\ inflight' = None
    /\ mem' = {}
    /\ UNCHANGED <<ci, consumer, keys, triaged, nwrites, srcOf, st, rewrote,
                   pollDup, applied, started, poll>>

(***************************************************************************)
(* The poll backstop. It sees only visible reds. The scan and the writes   *)
(* are separate steps, so the event path can write between them; the       *)
(* snapshot is then stale.                                                 *)
(***************************************************************************)
PollScan ==
    /\ PollOn
    /\ pst = "idle"
    /\ snap' = {r \in red \ gone : KeyOf(r, PollSrc) \notin keys}
    /\ pst' = "scanned"
    /\ UNCHANGED <<ci, consumer, store, starts, crashes>>

PollWrite(r) ==
    /\ pst = "scanned"
    /\ r \in snap
    /\ IF PollViaWriter
       THEN Write(r, KeyOf(r, PollSrc), "poll")
       ELSE Insert(r, KeyOf(r, PollSrc), "poll")
    /\ snap' = snap \ {r}
    /\ UNCHANGED <<ci, consumer, starts, pst, crashes>>

PollDone ==
    /\ pst = "scanned"
    /\ snap = {}
    /\ pst' = "idle"
    /\ UNCHANGED <<ci, consumer, store, starts, snap, crashes>>

PollCrash ==
    /\ pst = "scanned"
    /\ crashes < MaxCrash
    /\ crashes' = crashes + 1
    /\ pst' = "idle"
    /\ snap' = {}
    /\ UNCHANGED <<ci, consumer, store, starts>>

Next ==
    \/ \E r \in Runs : GoRed(r) \/ Supersede(r) \/ FirstEmit(r) \/ ReEmit(r)
    \/ Read \/ Process \/ Ack \/ WriteFail \/ DeadLetter \/ Crash \/ Restart
    \/ (\E r \in Runs : Dispatch(r)) \/ MarkSent \/ DispatcherCrash
    \/ PollScan \/ (\E r \in Runs : PollWrite(r)) \/ PollDone \/ PollCrash

\* CI turning red, superseding, re-publishes, lost publishes, write failures
\* and crashes are adversarial: never assumed to happen. The detector's first
\* publish of a visible red, the consumer, the dispatcher and the poll keep
\* running. Write failures are bounded by MaxFail, so a write eventually
\* succeeds or its event, once its own retries reach RetryLimit, is
\* dead-lettered.
Fairness ==
    /\ WF_vars(\E r \in Runs : FirstEmit(r))
    /\ WF_vars(Read)
    /\ WF_vars(Process)
    /\ WF_vars(Ack)
    /\ WF_vars(Restart)
    /\ WF_vars(\E r \in Runs : Dispatch(r))
    /\ WF_vars(MarkSent)
    /\ WF_vars(PollScan)
    /\ WF_vars(\E r \in Runs : PollWrite(r))
    /\ WF_vars(PollDone)

Spec == Init /\ [][Next]_vars /\ Fairness

(***************************************************************************)
(* Properties.                                                             *)
(***************************************************************************)
\* P1: one triage record per canonical run identity.
OneTriagePerIdentity == \A r \in Runs : nwrites[r] <= 1

\* P1s: one applied owner start per canonical run identity.
OneStartPerIdentity == \A r \in Runs : started[r] <= 1

\* P2: no lost red. Every red identity is eventually triaged, unless it stops
\* being visible first (a superseded red needs no triage).
NoLostRed == \A r \in Runs : (r \in red) ~> (r \in triaged \/ r \in gone)

\* P2s: every triage record's start is eventually applied by its receiver.
NoLostStart == \A r \in Runs : (r \in triaged) ~> (r \in applied)

\* P3: the poll backstop never writes a record for an identity that already
\* holds one.
PollNeverDoubleWrites == ~pollDup

\* P4: a committed offset is a promise that the write behind it is durable:
\* the event's record exists, or the event is recorded as dead-lettered.
AckedIsDurable == \A i \in 1..off : log[i] \in triaged \/ i \in dlq

\* P5: column ownership. The path that inserted a record set its triage
\* columns, and only the dispatcher moves its start status; no later triage
\* write, from either path, changes a column of an existing record.
ColumnOwnership == ~rewrote

=============================================================================
