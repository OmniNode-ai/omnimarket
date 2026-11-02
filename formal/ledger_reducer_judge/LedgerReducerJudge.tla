---------------------------- MODULE LedgerReducerJudge ----------------------------
(* OMN-21949, plan task L9 of the work ledger bus-only cutover (model before build
   for L10 to L13). Adopts OMN-20537 and OMN-19386. Base: LedgerBusAppend (OMN-20275,
   OMN-20537), where the judge is a file writer under an fcntl lock and a counter file.
   Here the judge is the reducer, the single writer.

   Writers (one per principal, that is one per runtime tenant) publish an append
   command on their own ingress topic of the reducer's bus: bus[p]. A writer replays a
   command under the same req= id while it has no receipt (MaxReplays), so the log can
   hold a command more than once. One reducer worker per ingress topic (per-tenant
   topics are separate partitions of one consumer group), so two workers can hold
   commands of two principals at the same time. Each worker runs, with a crash possible
   in every gap:

     Fetch -> Lock -> Guard -> Write -> Commit -> CommitOffset

   Lock    takes the per-ledger advisory lock inside the transaction (PerLedgerLock).
   Guard   is a pure function of (command, state): the req= record table first (a req
           already recorded is a duplicate and gets its stored answer), then the rules
           over the rows the reducer holds (here: two rows with the same index from
           different principals conflict, the second is refused). The state is read
           under the lock (adopted), or from the projection that consumes the accepted
           row topic (control GuardFromProjection).
   Write   builds the transaction body: the ledger row with ledger_seq = last seq + 1
           (adopted; control SeqInTx = FALSE draws it from a counter outside the
           transaction when the guard accepts), the req= record, and the outbox entries
           (the accepted row event, then the receipt, built from the row it just wrote;
           control ReceiptBeforeCommit publishes the receipt before the commit instead).
   Commit  makes all of it visible at once and releases the lock. A crash before it
           loses the whole body and releases the lock (the connection drops).
   CommitOffset advances the ingress offset. A crash before it redelivers the command.

   The relay is a separate process. It publishes outbox entries in order to the accepted
   row topic (events) and the receipt topic (receipts), then marks the entry published.
   It may crash between publish and mark and then republishes that entry. A projection
   consumes events in order and lags the ledger.

   Invariants (all hold in the adopted configurations):
     NoDuplicateRow, SeqGapless, SeqMatchesEventOrder, PrincipalInterleavingSerial,
     ReceiptTruth, GuardSeesAllPriorRows, and GuardEffective (no two conflicting rows).
   Liveness:
     EveryRequestAnswered, EveryCommittedRowPublished.

   Controls (one knob each; the expected violation is named in each cfg header):
     receipt_before_commit        ReceiptTruth
     seq_outside_tx               SeqGapless
     guard_lagging_projection     GuardSeesAllPriorRows
     guard_lagging_projection_effect GuardEffective
     no_lock                      PrincipalInterleavingSerial
     relay_unordered              SeqMatchesEventOrder
     no_dedup                     NoDuplicateRow
     commit_offset_first          EveryRequestAnswered

   Witnesses (W1 to W5, expected violated): the adopted bounds reach two committed rows,
   a duplicate receipt after a crash redelivery, a duplicate receipt after a writer
   replay, a refused receipt, and a republished event after a relay crash.

   Abstractions are listed in README.txt.
*)
EXTENDS Naturals, Sequences, FiniteSets, TLC

CONSTANTS
  Principals,
  RowsPer,              \* rows per principal; row = <<principal, k>>, req id = row
  ConflictOn,           \* rows with the same k from different principals conflict
  MaxCrashes,           \* reducer worker crashes
  MaxRelayCrashes,      \* relay crashes (between publish and mark)
  MaxReplays,           \* writer replays per row
  DedupCheck,           \* the guard checks the req= record table first
  PerLedgerLock,        \* the transaction takes the per-ledger advisory lock
  SeqInTx,              \* ledger_seq = last seq + 1 inside the transaction
  ReceiptBeforeCommit,  \* control: the receipt is published before the commit
  GuardFromProjection,  \* control: the guard reads the projection, not the reducer's rows
  CommitOffsetFirst,    \* control: the offset is committed at fetch
  RelayInOrder          \* the relay publishes the lowest unpublished outbox entry

Rows == Principals \X (1..RowsPer)
Owner(r) == r[1]
Conflicts(a, b) == ConflictOn /\ a # b /\ a[2] = b[2]
None == "none"
NoRow == <<None, 0>>

VARIABLES
  bus, offset, sent, replays, rcvd,                \* writers and the ingress topics
  ledger, reqlog, outbox, marked, rpend,           \* reducer database and relay
  events, receipts, proj, pj,                      \* published topics, projection
  pc, cmd, dec, gview, txn, wseq,                  \* per worker
  holder, counter, crashes, rcrashes

vars == <<bus, offset, sent, replays, rcvd, ledger, reqlog, outbox, marked, rpend,
          events, receipts, proj, pj, pc, cmd, dec, gview, txn, wseq,
          holder, counter, crashes, rcrashes>>

EmptyTxn == [led |-> << >>, log |-> {}, out |-> << >>]
Evt(r, s) == [kind |-> "evt", row |-> r, status |-> "-", seq |-> s]
Rcpt(r, st, s) == [kind |-> "rcpt", row |-> r, status |-> st, seq |-> s]

LedgerRows == {ledger[k].row : k \in 1..Len(ledger)}
RowInLedger(r) == r \in LedgerRows
LastSeq == IF Len(ledger) = 0 THEN 0 ELSE ledger[Len(ledger)].seq
InLog(r) == \E x \in reqlog : x.row = r
LogOf(r) == CHOOSE x \in reqlog : x.row = r
Answered(r) == \E x \in receipts : x.row = r
Unmarked == {i \in 1..Len(outbox) : i \notin marked}
InTx == {p \in Principals : pc[p] \in {"locked", "decided", "written", "early"}}

Init ==
  /\ bus = [p \in Principals |-> << >>] /\ offset = [p \in Principals |-> 0]
  /\ sent = {} /\ replays = [r \in Rows |-> 0] /\ rcvd = {}
  /\ ledger = << >> /\ reqlog = {} /\ outbox = << >> /\ marked = {} /\ rpend = 0
  /\ events = << >> /\ receipts = {} /\ proj = {} /\ pj = 0
  /\ pc = [p \in Principals |-> "idle"] /\ cmd = [p \in Principals |-> None]
  /\ dec = [p \in Principals |-> None] /\ gview = [p \in Principals |-> {}]
  /\ txn = [p \in Principals |-> EmptyTxn] /\ wseq = [p \in Principals |-> 0]
  /\ holder = None /\ counter = 0 /\ crashes = 0 /\ rcrashes = 0

writerVars == <<sent, replays, rcvd>>
workerVars == <<pc, cmd, dec, gview, txn, wseq>>
relayVars == <<marked, rpend>>

(* ---------------------------------------------------------------- writers *)

Publish(r) ==
  /\ r \notin sent
  /\ bus' = [bus EXCEPT ![Owner(r)] = Append(@, r)]
  /\ sent' = sent \cup {r}
  /\ UNCHANGED <<offset, replays, rcvd, ledger, reqlog, outbox, events, receipts,
                 proj, pj, holder, counter, crashes, rcrashes>>
  /\ UNCHANGED workerVars /\ UNCHANGED relayVars

\* Same req= id, while the writer has no receipt.
Replay(r) ==
  /\ r \in sent /\ r \notin rcvd
  /\ replays[r] < MaxReplays
  /\ replays' = [replays EXCEPT ![r] = @ + 1]
  /\ bus' = [bus EXCEPT ![Owner(r)] = Append(@, r)]
  /\ UNCHANGED <<offset, sent, rcvd, ledger, reqlog, outbox, events, receipts,
                 proj, pj, holder, counter, crashes, rcrashes>>
  /\ UNCHANGED workerVars /\ UNCHANGED relayVars

Deliver(r) ==
  /\ Answered(r) /\ r \notin rcvd
  /\ rcvd' = rcvd \cup {r}
  /\ UNCHANGED <<bus, offset, sent, replays, ledger, reqlog, outbox, events, receipts,
                 proj, pj, holder, counter, crashes, rcrashes>>
  /\ UNCHANGED workerVars /\ UNCHANGED relayVars

(* ---------------------------------------------------------------- reducer *)

Fetch(p) ==
  /\ pc[p] = "idle"
  /\ offset[p] < Len(bus[p])
  /\ pc' = [pc EXCEPT ![p] = "fetched"]
  /\ cmd' = [cmd EXCEPT ![p] = bus[p][offset[p] + 1]]
  /\ offset' = IF CommitOffsetFirst THEN [offset EXCEPT ![p] = @ + 1] ELSE offset
  /\ UNCHANGED <<bus, writerVars, ledger, reqlog, outbox, events, receipts, proj, pj,
                 dec, gview, txn, wseq, holder, counter, crashes, rcrashes>>
  /\ UNCHANGED relayVars

Lock(p) ==
  /\ pc[p] = "fetched"
  /\ IF PerLedgerLock
       THEN holder = None /\ holder' = p
       ELSE UNCHANGED holder
  /\ pc' = [pc EXCEPT ![p] = "locked"]
  /\ UNCHANGED <<bus, offset, writerVars, ledger, reqlog, outbox, events, receipts,
                 proj, pj, cmd, dec, gview, txn, wseq, counter, crashes, rcrashes>>
  /\ UNCHANGED relayVars

\* A pure function of (command, state). The state is the reducer's own rows, or the
\* lagging projection in the control.
Guard(p) ==
  /\ pc[p] = "locked"
  /\ LET r == cmd[p]
         view == IF GuardFromProjection THEN proj ELSE LedgerRows
         d == IF DedupCheck /\ InLog(r) THEN "duplicate"
              ELSE IF \E q \in view : Conflicts(q, r) THEN "refused"
              ELSE "accepted"
     IN /\ dec' = [dec EXCEPT ![p] = d]
        /\ gview' = [gview EXCEPT ![p] = view]
        /\ IF ~SeqInTx /\ d = "accepted"
             THEN /\ wseq' = [wseq EXCEPT ![p] = counter + 1]
                  /\ counter' = counter + 1
             ELSE UNCHANGED <<wseq, counter>>
  /\ pc' = [pc EXCEPT ![p] = "decided"]
  /\ UNCHANGED <<bus, offset, writerVars, ledger, reqlog, outbox, events, receipts,
                 proj, pj, cmd, txn, holder, crashes, rcrashes>>
  /\ UNCHANGED relayVars

Write(p) ==
  /\ pc[p] = "decided"
  /\ LET r == cmd[p]
         s == IF SeqInTx THEN LastSeq + 1 ELSE wseq[p]
         stored == LogOf(r)
     IN txn' = [txn EXCEPT ![p] =
          IF dec[p] = "duplicate"
            THEN [led |-> << >>, log |-> {},
                  out |-> <<Rcpt(r, IF stored.status = "refused" THEN "refused" ELSE "duplicate",
                                 stored.seq)>>]
          ELSE IF dec[p] = "refused"
            THEN [led |-> << >>, log |-> {[row |-> r, status |-> "refused", seq |-> 0]},
                  out |-> <<Rcpt(r, "refused", 0)>>]
          ELSE [led |-> <<[row |-> r, seq |-> s]>>,
                log |-> {[row |-> r, status |-> "accepted", seq |-> s]},
                out |-> IF ReceiptBeforeCommit THEN <<Evt(r, s)>>
                        ELSE <<Evt(r, s), Rcpt(r, "accepted", s)>>]]
  /\ pc' = [pc EXCEPT ![p] = "written"]
  /\ UNCHANGED <<bus, offset, writerVars, ledger, reqlog, outbox, events, receipts,
                 proj, pj, cmd, dec, gview, wseq, holder, counter, crashes, rcrashes>>
  /\ UNCHANGED relayVars

\* Control: the receipt leaves before the transaction commits.
EarlyReceipt(p) ==
  /\ ReceiptBeforeCommit
  /\ pc[p] = "written" /\ txn[p].led # << >>
  /\ receipts' = receipts \cup {[row |-> txn[p].led[1].row, status |-> "accepted",
                                 seq |-> txn[p].led[1].seq]}
  /\ pc' = [pc EXCEPT ![p] = "early"]
  /\ UNCHANGED <<bus, offset, writerVars, ledger, reqlog, outbox, events, proj, pj,
                 cmd, dec, gview, txn, wseq, holder, counter, crashes, rcrashes>>
  /\ UNCHANGED relayVars

\* The one commit: row, req= record and outbox entries become visible together.
Commit(p) ==
  /\ pc[p] \in {"written", "early"}
  /\ ledger' = ledger \o txn[p].led
  /\ reqlog' = reqlog \cup txn[p].log
  /\ outbox' = outbox \o txn[p].out
  /\ txn' = [txn EXCEPT ![p] = EmptyTxn]
  /\ holder' = IF holder = p THEN None ELSE holder
  /\ pc' = [pc EXCEPT ![p] = "committed"]
  /\ UNCHANGED <<bus, offset, writerVars, events, receipts, proj, pj, cmd, dec, gview,
                 wseq, counter, crashes, rcrashes>>
  /\ UNCHANGED relayVars

CommitOffset(p) ==
  /\ pc[p] = "committed"
  /\ offset' = IF CommitOffsetFirst THEN offset ELSE [offset EXCEPT ![p] = @ + 1]
  /\ pc' = [pc EXCEPT ![p] = "idle"]
  /\ gview' = [gview EXCEPT ![p] = {}]
  /\ UNCHANGED <<bus, writerVars, ledger, reqlog, outbox, events, receipts, proj, pj,
                 cmd, dec, txn, wseq, holder, counter, crashes, rcrashes>>
  /\ UNCHANGED relayVars

\* A worker crash drops the transaction body, releases the lock, keeps the counter.
WorkerCrash(p) ==
  /\ pc[p] # "idle"
  /\ crashes < MaxCrashes
  /\ crashes' = crashes + 1
  /\ pc' = [pc EXCEPT ![p] = "idle"]
  /\ txn' = [txn EXCEPT ![p] = EmptyTxn]
  /\ gview' = [gview EXCEPT ![p] = {}]
  /\ wseq' = [wseq EXCEPT ![p] = 0]
  /\ holder' = IF holder = p THEN None ELSE holder
  /\ UNCHANGED <<bus, offset, writerVars, ledger, reqlog, outbox, events, receipts,
                 proj, pj, cmd, dec, counter, rcrashes>>
  /\ UNCHANGED relayVars

(* ------------------------------------------------------------------ relay *)

RelayPublish ==
  /\ rpend = 0
  /\ \E i \in Unmarked :
       /\ RelayInOrder => \A j \in Unmarked : i <= j
       /\ LET e == outbox[i] IN
          IF e.kind = "evt"
            THEN /\ events' = Append(events, [row |-> e.row, seq |-> e.seq])
                 /\ UNCHANGED receipts
            ELSE /\ receipts' = receipts \cup {[row |-> e.row, status |-> e.status, seq |-> e.seq]}
                 /\ UNCHANGED events
       /\ rpend' = i
  /\ UNCHANGED <<bus, offset, writerVars, ledger, reqlog, outbox, proj, pj, marked,
                 holder, counter, crashes, rcrashes>>
  /\ UNCHANGED workerVars

RelayMark ==
  /\ rpend # 0
  /\ marked' = marked \cup {rpend}
  /\ rpend' = 0
  /\ UNCHANGED <<bus, offset, writerVars, ledger, reqlog, outbox, events, receipts,
                 proj, pj, holder, counter, crashes, rcrashes>>
  /\ UNCHANGED workerVars

\* Between publish and mark: the entry is republished after the restart.
RelayCrash ==
  /\ rpend # 0
  /\ rcrashes < MaxRelayCrashes
  /\ rcrashes' = rcrashes + 1
  /\ rpend' = 0
  /\ UNCHANGED <<bus, offset, writerVars, ledger, reqlog, outbox, events, receipts,
                 proj, pj, holder, counter, crashes, marked>>
  /\ UNCHANGED workerVars

\* The projection consumes the accepted row topic in order, behind the ledger.
ProjStep ==
  /\ pj < Len(events)
  /\ proj' = proj \cup {events[pj + 1].row}
  /\ pj' = pj + 1
  /\ UNCHANGED <<bus, offset, writerVars, ledger, reqlog, outbox, events, receipts,
                 holder, counter, crashes, rcrashes>>
  /\ UNCHANGED workerVars /\ UNCHANGED relayVars

(* ------------------------------------------------------------------- spec *)

WorkerStep(p) == Fetch(p) \/ Lock(p) \/ Guard(p) \/ Write(p) \/ EarlyReceipt(p)
                 \/ Commit(p) \/ CommitOffset(p)
RelayStep == RelayPublish \/ RelayMark

Next ==
  \/ \E r \in Rows : Publish(r) \/ Replay(r) \/ Deliver(r)
  \/ \E p \in Principals : WorkerStep(p) \/ WorkerCrash(p)
  \/ RelayStep \/ RelayCrash \/ ProjStep

Spec ==
  /\ Init /\ [][Next]_vars
  /\ WF_vars(\E r \in Rows : Publish(r))
  /\ WF_vars(\E r \in Rows : Deliver(r))
  /\ \A p \in Principals : WF_vars(WorkerStep(p))
  /\ WF_vars(RelayStep)
  /\ WF_vars(ProjStep)

(* ------------------------------------------------------------- properties *)

TypeOK ==
  /\ \A p \in Principals : pc[p] \in {"idle", "fetched", "locked", "decided", "written",
                                      "early", "committed"}
  /\ \A p \in Principals : offset[p] \in 0..Len(bus[p])
  /\ holder \in {None} \cup Principals
  /\ rpend \in 0..Len(outbox)

\* A replayed req= adds nothing: no row appears twice in the ledger.
NoDuplicateRow ==
  \A i, j \in 1..Len(ledger) : i # j => ledger[i].row # ledger[j].row

\* The assigned numbers are exactly 1..n.
SeqGapless == {ledger[k].seq : k \in 1..Len(ledger)} = 1..Len(ledger)

\* The ledger is in seq order, and the accepted row topic carries each row's event in
\* that order (a relay republish only repeats an event), complete from 1 up, and
\* each event is the row the ledger holds at that seq.
SeqMatchesEventOrder ==
  /\ \A k \in 1..Len(ledger) : ledger[k].seq = k
  /\ \A i, j \in 1..Len(events) : i < j => events[i].seq <= events[j].seq
  /\ {events[i].seq : i \in 1..Len(events)} = 1..Cardinality({events[i].seq : i \in 1..Len(events)})
  /\ \A i \in 1..Len(events) :
       events[i].seq <= Len(ledger) /\ ledger[events[i].seq].row = events[i].row

\* Commands of two principals are judged one at a time, and a transaction in flight
\* is the next row in sequence (judgement order is commit order is seq order).
PrincipalInterleavingSerial ==
  /\ Cardinality(InTx) <= 1
  /\ \A p \in Principals :
       (pc[p] \in {"written", "early"} /\ txn[p].led # << >>)
         => txn[p].led[1].seq = Len(ledger) + 1

\* No accepted or duplicate receipt without its committed row at that seq; no refused
\* receipt for a row that is in the ledger.
ReceiptTruth ==
  \A x \in receipts :
    /\ x.status \in {"accepted", "duplicate"} =>
         \E k \in 1..Len(ledger) : ledger[k].row = x.row /\ ledger[k].seq = x.seq
    /\ x.status = "refused" => ~RowInLedger(x.row)

\* While a transaction is judging, its guard read every row committed so far.
GuardSeesAllPriorRows ==
  \A p \in Principals :
    pc[p] \in {"decided", "written", "early"} => gview[p] = LedgerRows

\* What the guard is for: two conflicting rows never both land.
GuardEffective ==
  \A i, j \in 1..Len(ledger) : i # j => ~Conflicts(ledger[i].row, ledger[j].row)

(* Reachability witnesses: each is expected to be violated, which proves the adopted
   bounds reach the paths the invariants guard (a zero without a positive control). *)
WitnessBothRowsCommitted == Len(ledger) # Cardinality(Rows)
WitnessDuplicateReceipt == ~(\E x \in receipts : x.status = "duplicate")
WitnessRefusedReceipt == ~(\E x \in receipts : x.status = "refused")
WitnessEventRepublished ==
  ~(\E i, j \in 1..Len(events) : i < j /\ events[i] = events[j])

EveryRequestAnswered == \A r \in Rows : (r \in sent) ~> Answered(r)

EveryCommittedRowPublished ==
  \A r \in Rows : RowInLedger(r) ~> (\E i \in 1..Len(events) : events[i].row = r)
=============================================================================
