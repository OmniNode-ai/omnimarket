------------------------- MODULE ThroughputTick -------------------------
(***************************************************************************)
(* The hourly check's send decision and its dedupe store (OMN-23732, task *)
(* H0 of the six-workflows plan). The check decides which lanes to send,   *)
(* each under one send key; it records what it sent in a store shared with *)
(* other writers; a later check reads the store and does not send a key it *)
(* finds there.                                                            *)
(*                                                                         *)
(* One key is one lane the check sends: a fill key (lab-fill.sent.json),   *)
(* a no-host relaunch (dispatch-replaced.json, the run id), a stuck-green  *)
(* head (green-unlanded.json, pull request @ head), a floor escalation     *)
(* (floor-escalation.json, the repository), a stuck-owner cause and base.  *)
(* The README maps each to the file and the key a live check writes.       *)
(*                                                                         *)
(* Actors: two checks t1 and t2 (the launchd tick and a session tick, or a *)
(* tick and the detached builder it started) and one foreign writer f1     *)
(* (any other process that replaces the whole store from a read of it).    *)
(* The store is replaced whole, from the writer's private copy of what it  *)
(* last read: that is write_sent / write_history, an atomic replace of a   *)
(* read-modify-write.                                                      *)
(*                                                                         *)
(* A check runs: Decide (read the store, list the keys not in it), Lock    *)
(* (the fill's flock), Reread (read the store again under the lock and     *)
(* drop what it holds), then per key Send (call the runner) and Record     *)
(* (write the store). It may die between any two steps (Crash); a dead     *)
(* check frees its flock. The consumer is the remote-lane runner: with     *)
(* ConsumerDedupe it starts one lane per key (the lane name is the key);   *)
(* without, it starts one per call (the lane name carries the tick's       *)
(* stamp, so a second call is a second lane).                              *)
(*                                                                         *)
(* Properties (each mutant config breaks exactly one design knob):         *)
(*   SendAttemptOnce  the runner is called at most once per key            *)
(*   StartedOnce      at most one lane is started per key                  *)
(*                                                                         *)
(* Scope of the properties: a send the runner refuses (no host free, the    *)
(* brief check refused) started no lane and is not recorded; the next      *)
(* check sends it again by design. Such a call is not a Send here: Send is *)
(* a call the runner accepted, so attempts counts accepted calls and       *)
(* StartedOnce counts lanes. The in-tick fill records once after all its   *)
(* sends and the detached worker after each send; the model records after  *)
(* each send, the narrower window: a crash between a send and its record   *)
(* reaches the same violation either way (M5), only more often in the      *)
(* wider window. Two ticks on two state directories (the 2026-10-19        *)
(* incident) share neither store nor lock, so each is blind to the other's *)
(* keys: that is the one-store requirement R2 of VERDICT.txt, not a knob.  *)
(*                                                                         *)
(* Left out, on purpose: the dedupe window (a key sent again after its     *)
(* window is a new send by design; the model is one window), more than two *)
(* checks, the content of the send, the placement result (a refused send   *)
(* is not recorded and is retried, which is a new attempt of an unsent     *)
(* key), clock skew between hosts, and a store on another host.            *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets

CONSTANTS
    Keys,             \* the send keys the checks may send
    LockedRMW,        \* TRUE: a check holds the flock from Reread to the end
    LockedWriters,    \* TRUE: the foreign writer takes the same flock
    ReReadUnderLock,  \* TRUE: a check rereads the store under the lock and drops recorded keys
    LockFailOpen,     \* TRUE: a lock file that cannot be opened lets the check go on without it
    ReadFailOpen,     \* TRUE: a store that cannot be read reads as empty (read_sent: absent or unreadable is empty)
    ConsumerDedupe,   \* TRUE: the runner starts one lane per key
    Crash             \* TRUE: a check may die between any two of its steps

Ticks  == {"t1", "t2"}
Writer == "f1"
Actors == Ticks \cup {Writer}

VARIABLES
    store,     \* the keys the shared record holds
    lock,      \* the flock holder: an actor or "none"
    pc,        \* each actor's step
    todo,      \* a check's keys still to send
    loc,       \* a check's private copy of the record (what Record writes back)
    wsnap,     \* the foreign writer's read of the record
    attempts,  \* attempts[k]: calls of the runner for k
    started,   \* started[k]: lanes the runner started for k
    sentBy     \* sentBy[t]: calls the check t made

vars == <<store, lock, pc, todo, loc, wsnap, attempts, started, sentBy>>

Init ==
    /\ store = {}
    /\ lock = "none"
    /\ pc = [a \in Actors |-> "init"]
    /\ todo = [t \in Ticks |-> {}]
    /\ loc = [t \in Ticks |-> {}]
    /\ wsnap = {}
    /\ attempts = [k \in Keys |-> 0]
    /\ started = [k \in Keys |-> 0]
    /\ sentBy = [t \in Ticks |-> 0]

\* The check reads the store outside the lock and lists the keys it does not hold.
Reads == IF ReadFailOpen THEN {store, {}} ELSE {store}

Decide(t) ==
    /\ pc[t] = "init"
    /\ \E r \in Reads :
         /\ todo' = [todo EXCEPT ![t] = Keys \ r]
         /\ loc' = [loc EXCEPT ![t] = r]
    /\ pc' = [pc EXCEPT ![t] = "decided"]
    /\ UNCHANGED <<store, lock, wsnap, attempts, started, sentBy>>

\* The flock: taken when free; with LockFailOpen the check may go on without it.
Lock(t) ==
    /\ pc[t] = "decided"
    /\ \/ /\ LockedRMW
          /\ lock = "none"
          /\ lock' = t
       \/ /\ ~LockedRMW
          /\ lock' = lock
       \/ /\ LockedRMW /\ LockFailOpen
          /\ lock = "none"      \* the lock file cannot be opened: nobody holds a flock to wait on
          /\ lock' = lock
    /\ pc' = [pc EXCEPT ![t] = "locked"]
    /\ UNCHANGED <<store, todo, loc, wsnap, attempts, started, sentBy>>

\* Under the lock the check reads the store again and drops what it holds.
Reread(t) ==
    /\ pc[t] = "locked"
    /\ IF ReReadUnderLock
          THEN \E r \in Reads :
                 /\ todo' = [todo EXCEPT ![t] = @ \ r]
                 /\ loc' = [loc EXCEPT ![t] = r]
          ELSE /\ todo' = todo
               /\ loc' = loc
    /\ pc' = [pc EXCEPT ![t] = "loop"]
    /\ UNCHANGED <<store, lock, wsnap, attempts, started, sentBy>>

\* One call of the runner for one key; the consumer starts a lane unless it dedupes.
Send(t) ==
    /\ pc[t] = "loop"
    /\ \E k \in todo[t] :
         /\ attempts' = [attempts EXCEPT ![k] = @ + 1]
         /\ started' = [started EXCEPT ![k] =
                          IF ConsumerDedupe /\ @ > 0 THEN @ ELSE @ + 1]
         /\ sentBy' = [sentBy EXCEPT ![t] = @ + 1]
         /\ todo' = [todo EXCEPT ![t] = @ \ {k}]
         /\ loc' = [loc EXCEPT ![t] = @ \cup {k}]
    /\ pc' = [pc EXCEPT ![t] = "sent"]
    /\ UNCHANGED <<store, lock, wsnap>>

\* The check writes its private copy back as the whole record.
Record(t) ==
    /\ pc[t] = "sent"
    /\ store' = loc[t]
    /\ pc' = [pc EXCEPT ![t] = "loop"]
    /\ UNCHANGED <<lock, todo, loc, wsnap, attempts, started, sentBy>>

Finish(t) ==
    /\ pc[t] = "loop"
    /\ todo[t] = {}
    /\ pc' = [pc EXCEPT ![t] = "done"]
    /\ lock' = IF lock = t THEN "none" ELSE lock
    /\ UNCHANGED <<store, todo, loc, wsnap, attempts, started, sentBy>>

\* A check dies between two steps; the operating system frees its flock.
Die(t) ==
    /\ Crash
    /\ pc[t] \in {"decided", "locked", "loop", "sent"}
    /\ pc' = [pc EXCEPT ![t] = "dead"]
    /\ lock' = IF lock = t THEN "none" ELSE lock
    /\ UNCHANGED <<store, todo, loc, wsnap, attempts, started, sentBy>>

\* The foreign writer reads the record and later replaces it whole from that read.
WRead ==
    /\ pc[Writer] = "init"
    /\ \/ /\ LockedWriters
          /\ lock = "none"
          /\ lock' = Writer
       \/ /\ ~LockedWriters
          /\ lock' = lock
    /\ wsnap' = store
    /\ pc' = [pc EXCEPT ![Writer] = "read"]
    /\ UNCHANGED <<store, todo, loc, attempts, started, sentBy>>

WWrite ==
    /\ pc[Writer] = "read"
    /\ store' = wsnap
    /\ lock' = IF lock = Writer THEN "none" ELSE lock
    /\ pc' = [pc EXCEPT ![Writer] = "done"]
    /\ UNCHANGED <<todo, loc, wsnap, attempts, started, sentBy>>

Next ==
    \/ \E t \in Ticks : Decide(t) \/ Lock(t) \/ Reread(t) \/ Send(t)
                        \/ Record(t) \/ Finish(t) \/ Die(t)
    \/ WRead \/ WWrite

Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ store \subseteq Keys
    /\ lock \in Actors \cup {"none"}
    /\ \A a \in Actors : pc[a] \in {"init", "decided", "locked", "loop", "sent", "read", "done", "dead"}
    /\ \A k \in Keys : attempts[k] \in Nat /\ started[k] \in Nat

SendAttemptOnce == \A k \in Keys : attempts[k] <= 1

StartedOnce == \A k \in Keys : started[k] <= 1

(* Witnesses: each states that a path the design relies on is reachable, so the
   passing run is not vacuous. TLC must report each as violated under its own config. *)
NotSkippedAll == ~(pc["t2"] = "done" /\ sentBy["t2"] = 0 /\ sentBy["t1"] = Cardinality(Keys))
NotRefusedRetry == ~(\E k \in Keys : attempts[k] = 2 /\ started[k] = 1)
NotWriterErased == ~(pc[Writer] = "done" /\ store = {} /\ \E t \in Ticks : sentBy[t] > 0)

=========================================================================
