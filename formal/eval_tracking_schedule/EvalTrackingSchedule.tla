---------------------------- MODULE EvalTrackingSchedule ----------------------------
(***************************************************************************)
(* The window trigger of the weekly coding test (OMN-24177, plan task T13). *)
(*                                                                         *)
(* Models: node_eval_tracking_schedule_compute (a stateless tick that names *)
(* the current window's run of one model) and the trial driver's run-id    *)
(* no-op (nodes/node_code_eval_trial_orchestrator, leg.py: a request for a *)
(* run id it holds is dropped). Two tracked models run on their own       *)
(* cadence (stand-ins for 7 and 28 days: a window is 1 day for "a" and 4   *)
(* days for "b"). The clock moves one or two days at a time, so a window   *)
(* can be jumped over.                                                     *)
(*                                                                         *)
(* A message in transit may be lost (the trigger crashed between the send  *)
(* and the driver's record, or the bus dropped it); the next tick sends    *)
(* the same run id again. The driver's start is not idempotent by itself:  *)
(* "starts" counts every real start of a run, and only the no-op on a      *)
(* recorded run id keeps it at one.                                        *)
(*                                                                         *)
(* Knobs (each constant is one design decision a mutant turns off):        *)
(*   FreshIds    the run id carries a fresh nonce per send, not the        *)
(*               window                                                    *)
(*   DriverNoop  the driver drops a request for a recorded run id          *)
(*   SplitStart  the driver starts the run and records it in two steps,    *)
(*               and may crash between them                                *)
(*   RoundUp     a tick reads the window it is nearest, not the one it is  *)
(*               in                                                        *)
(*   MakeUp      a tick also names every earlier window                    *)
(*   SharedDays  every model uses the shortest cadence                     *)
(*                                                                         *)
(* Left out: what a run does after it starts (T6's close and void rules,   *)
(* the trial driver's own model, formal/code_eval_trial); the bus's        *)
(* ordering; a real clock (days are integers).                             *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets

CONSTANTS MaxDay, MaxSends, MaxMsgs,
          FreshIds, DriverNoop, SplitStart, RoundUp, MakeUp, SharedDays

Models == {"a", "b"}
DaysOf == [m \in Models |-> IF m = "a" THEN 1 ELSE 4]
MinDays == 1
Nonces == 0..(MaxSends - 1)
Ids == Models \X (0..MaxDay) \X Nonces

\* The window a model is in on day d, by its own cadence (the truth the invariants read).
Win(m, d) == d \div DaysOf[m]
\* The window the trigger under test names on day d.
CadenceUsed(m) == IF SharedDays THEN MinDays ELSE DaysOf[m]
SendWin(m, d) == IF RoundUp THEN (d + CadenceUsed(m) - 1) \div CadenceUsed(m)
                 ELSE d \div CadenceUsed(m)

VARIABLES day,      \* the clock, in days
          ticked,   \* the (model, window) pairs a tick has been in
          bus,      \* messages in transit per run id
          rows,     \* run ids the driver has recorded
          pending,  \* run ids started and not yet recorded (SplitStart)
          starts,   \* real starts per run id
          sends,    \* sends so far (bounds the search, names the nonce)
          lostIds,  \* run ids whose message was lost once
          noops     \* requests the driver dropped for a recorded run id

vars == <<day, ticked, bus, rows, pending, starts, sends, lostIds, noops>>

Init == /\ day = 0
        /\ ticked = {}
        /\ bus = [i \in Ids |-> 0]
        /\ rows = {}
        /\ pending = {}
        /\ starts = [i \in Ids |-> 0]
        /\ sends = 0
        /\ lostIds = {}
        /\ noops = 0

Advance == /\ day < MaxDay
           /\ \E d \in (day + 1)..(IF day + 2 < MaxDay THEN day + 2 ELSE MaxDay) :
                 day' = d
           /\ UNCHANGED <<ticked, bus, rows, pending, starts, sends, lostIds, noops>>

Tick(m) ==
  /\ sends < MaxSends
  /\ \E w \in (IF MakeUp THEN 0..SendWin(m, day) ELSE {SendWin(m, day)}) :
       LET id == <<m, w, IF FreshIds THEN sends ELSE 0>> IN
         /\ w <= MaxDay
         /\ bus[id] < MaxMsgs
         /\ bus' = [bus EXCEPT ![id] = @ + 1]
  /\ ticked' = ticked \cup {<<m, Win(m, day)>>}
  /\ sends' = sends + 1
  /\ UNCHANGED <<day, rows, pending, starts, lostIds, noops>>

Lose(id) ==
  /\ bus[id] > 0
  /\ bus' = [bus EXCEPT ![id] = @ - 1]
  /\ lostIds' = lostIds \cup {id}
  /\ UNCHANGED <<day, ticked, rows, pending, starts, sends, noops>>

Deliver(id) ==
  /\ bus[id] > 0
  /\ bus' = [bus EXCEPT ![id] = @ - 1]
  /\ IF DriverNoop /\ id \in rows
        THEN /\ noops' = noops + 1
             /\ UNCHANGED <<day, ticked, rows, pending, starts, sends, lostIds>>
        ELSE /\ starts' = [starts EXCEPT ![id] = @ + 1]
             /\ IF SplitStart
                   THEN /\ pending' = pending \cup {id}
                        /\ rows' = rows
                   ELSE /\ rows' = rows \cup {id}
                        /\ pending' = pending
             /\ UNCHANGED <<day, ticked, sends, lostIds, noops>>

Record(id) ==
  /\ SplitStart
  /\ id \in pending
  /\ rows' = rows \cup {id}
  /\ pending' = pending \ {id}
  /\ UNCHANGED <<day, ticked, bus, starts, sends, lostIds, noops>>

DriverCrash ==
  /\ SplitStart
  /\ pending /= {}
  /\ pending' = {}
  /\ UNCHANGED <<day, ticked, bus, rows, starts, sends, lostIds, noops>>

Next == \/ Advance
        \/ \E m \in Models : Tick(m)
        \/ \E id \in Ids : Lose(id) \/ Deliver(id) \/ Record(id)
        \/ DriverCrash

Spec == Init /\ [][Next]_vars

TypeOK ==
  /\ day \in 0..MaxDay
  /\ ticked \subseteq (Models \X (0..MaxDay))
  /\ bus \in [Ids -> 0..MaxMsgs]
  /\ rows \subseteq Ids
  /\ pending \subseteq Ids
  /\ starts \in [Ids -> Nat]
  /\ sends \in 0..MaxSends

\* A run id starts at most once, across a crash between send and record.
StartedOnce == \A id \in Ids : starts[id] <= 1

\* A model and window have at most one run, whatever the run ids.
OneRunPerWindow ==
  \A m \in Models, w \in 0..MaxDay :
    Cardinality({id \in Ids : id[1] = m /\ id[2] = w /\ starts[id] > 0}) <= 1

\* A run never starts before the clock is in its window, by the model's own cadence.
NeverEarly ==
  \A id \in Ids : starts[id] > 0 => id[2] <= Win(id[1], day)

\* A window no tick was in is never run: a missed window is not made up.
NoMakeUp ==
  \A id \in Ids : starts[id] > 0 => <<id[1], id[2]>> \in ticked

\* Witnesses: each is meant to be VIOLATED, which shows the path it names is reachable.
NotNoop == noops = 0
NotLostThenStarted == \A id \in lostIds : starts[id] = 0
NotWindowSkipped ==
  \A m \in Models, w \in 0..MaxDay :
    (w < Win(m, day)) => <<m, w>> \in ticked
=============================================================================
