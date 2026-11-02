---------------------------- MODULE LandingStall ----------------------------
(***************************************************************************)
(* The repository-wide stall rule of the landing controller (OMN-21880).   *)
(*                                                                         *)
(* A stall opens when one required context is red on at least Pct percent  *)
(* of at least MinPrs open pull requests. It is judged per context, never  *)
(* per pull request or per snapshot. At ExcAfter ticks of an open stall    *)
(* the stall writes one exception row, sends one cause lane, and the       *)
(* controller sends no further repair workers at that context. At QAfter   *)
(* ticks, with no eligible repair, one plain question goes to the          *)
(* operator. A stall closes when the context has been clear for            *)
(* ClearReads consecutive snapshots; closing lifts the block.              *)
(*                                                                         *)
(* A tick is one coarse unit of time (the real rule counts 60 and 120      *)
(* minutes; the model counts 2 and 4 ticks, so the ratio is the same).     *)
(* Elapsed time is kept per open stall and saturates at Cap, which is      *)
(* above every threshold, so the state space is finite.                    *)
(*                                                                         *)
(* The snapshot is the only reader of the world. The environment changes   *)
(* the red and open counts of any context, and whether an eligible repair  *)
(* exists, at any step; a change makes the context's last snapshot stale.  *)
(*                                                                         *)
(* Scope: this models the rule the pure compute node evaluates and the     *)
(* controller's refusal to dispatch. It does not model the controller's    *)
(* other holds, a cause lane's own success, or delivery of the row, the    *)
(* lane or the question.                                                   *)
(*                                                                         *)
(* Each design knob below is one decision of the design note; every mutant *)
(* config flips exactly one of them and must violate the property it       *)
(* protects.                                                               *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets

CONSTANTS
    Ctx,               \* the required contexts the controller judges
    Active,            \* the contexts whose counts change (the rest stay calm)
    Pct,               \* red share that opens a stall, in percent
    MinPrs,            \* fewest open pull requests that can open a stall
    MaxPR,             \* most open pull requests the model explores
    PctCmp,            \* "ge" | "gt": the share test
    MinCmp,            \* "ge" | "gt": the open-count test
    MinGuard,          \* the open-count test is applied
    StallKey,          \* "context" | "snapshot": what one stall is keyed by
    ExcAfter,          \* ticks of an open stall before the exception row
    ExcEnabled,        \* the exception row is ever written
    ExcRows,           \* "once" | "every": the row per episode
    LaneWithRow,       \* the exception row sends its cause lane
    QAfter,            \* ticks of an open stall before the question
    QRows,             \* "once" | "every": the question per episode
    AskWhenRepair,     \* ask even when an eligible repair exists
    BlockOn,           \* "exception" | "open" | "never": when the controller stops
    BlockScope,        \* "context" | "all": which contexts the block refuses
    ClearBlockOnClose, \* closing the stall lifts the block
    ClearReads,        \* consecutive clear snapshots that close a stall
    CloseEnabled,      \* a stall ever closes
    ClockResets,       \* a reading that still holds restarts the stall's clock
    MaxAttempts        \* dispatch attempts in one behaviour (exploration bound)

\* The design's values, which the invariants state independently of the knobs.
DesignExc == 2
DesignQ == 4
DesignClear == 2
Cap == 5

VARIABLES
    n,          \* open pull requests per context
    r,          \* of those, red on the context
    rep,        \* an eligible repair exists for the context
    fresh,      \* the context's last snapshot reflects the world
    stall,      \* "none" | "open" | "excepted" | "asked"
    el,         \* ticks of the open stall, saturating at Cap
    reads,      \* consecutive clear snapshots, saturating at Cap
    excRows,    \* exception rows written in the current episode
    lanes,      \* cause lanes sent in the current episode
    qs,         \* questions asked in the current episode
    blocked,    \* the controller sends no repair workers at the context
    wAfter,     \* workers sent at the context while it was excepted or asked
    nopen,      \* open stall records for the context
    openedOK,   \* the share and count held, by the design test, when it opened
    excEl,      \* the stall's ticks when the exception row was written
    askEl,      \* the stall's ticks when the question was asked
    askRep,     \* an eligible repair existed when the question was asked
    lastClose,  \* clear snapshots behind the last close (99 before any)
    attempts,   \* dispatch attempts so far
    wrong       \* a dispatch was refused where the rule allows it

vars == <<n, r, rep, fresh, stall, el, reads, excRows, lanes, qs, blocked, wAfter,
          nopen, openedOK, excEl, askEl, askRep, lastClose, attempts, wrong>>

Min(a, b) == IF a < b THEN a ELSE b

\* The share and count test as the design states it.
TrueCond(k) == r[k] * 100 >= Pct * n[k] /\ n[k] >= MinPrs

\* The test as the configured knobs apply it.
Cond(k) ==
    /\ IF PctCmp = "ge" THEN r[k] * 100 >= Pct * n[k] ELSE r[k] * 100 > Pct * n[k]
    /\ MinGuard => (IF MinCmp = "ge" THEN n[k] >= MinPrs ELSE n[k] > MinPrs)

Allowed(k) ==
    IF BlockScope = "all" THEN \A j \in Ctx : ~blocked[j] ELSE ~blocked[k]

Init ==
    /\ n = [k \in Ctx |-> 0]
    /\ r = [k \in Ctx |-> 0]
    /\ rep = [k \in Ctx |-> FALSE]
    /\ fresh = [k \in Ctx |-> TRUE]
    /\ stall = [k \in Ctx |-> "none"]
    /\ el = [k \in Ctx |-> 0]
    /\ reads = [k \in Ctx |-> 0]
    /\ excRows = [k \in Ctx |-> 0]
    /\ lanes = [k \in Ctx |-> 0]
    /\ qs = [k \in Ctx |-> 0]
    /\ blocked = [k \in Ctx |-> FALSE]
    /\ wAfter = [k \in Ctx |-> 0]
    /\ nopen = [k \in Ctx |-> 0]
    /\ openedOK = [k \in Ctx |-> TRUE]
    /\ excEl = [k \in Ctx |-> DesignExc]
    /\ askEl = [k \in Ctx |-> DesignQ]
    /\ askRep = [k \in Ctx |-> FALSE]
    /\ lastClose = [k \in Ctx |-> 99]
    /\ attempts = 0
    /\ wrong = FALSE

\* The environment changes one context's counts.
Env(k) ==
    /\ k \in Active
    /\ \E nn \in 0..MaxPR, rr \in 0..MaxPR :
         /\ rr <= nn
         /\ n' = [n EXCEPT ![k] = nn]
         /\ r' = [r EXCEPT ![k] = rr]
    /\ fresh' = [fresh EXCEPT ![k] = FALSE]
    /\ UNCHANGED <<rep, stall, el, reads, excRows, lanes, qs, blocked, wAfter, nopen,
                   openedOK, excEl, askEl, askRep, lastClose, attempts, wrong>>

\* An eligible repair appears or goes away.
RepFlip(k) ==
    /\ k \in Active
    /\ rep' = [rep EXCEPT ![k] = ~rep[k]]
    /\ fresh' = [fresh EXCEPT ![k] = FALSE]
    /\ UNCHANGED <<n, r, stall, el, reads, excRows, lanes, qs, blocked, wAfter, nopen,
                   openedOK, excEl, askEl, askRep, lastClose, attempts, wrong>>

\* One unit of time passes: every open stall ages, and every context is due a new
\* snapshot. Snapshots arrive on the controller's cadence, with or without a change.
Tick ==
    /\ el' = [k \in Ctx |-> IF stall[k] # "none" THEN Min(el[k] + 1, Cap) ELSE el[k]]
    /\ fresh' = [k \in Ctx |-> FALSE]
    /\ UNCHANGED <<n, r, rep, stall, reads, excRows, lanes, qs, blocked, wAfter, nopen,
                   openedOK, excEl, askEl, askRep, lastClose, attempts, wrong>>

\* A snapshot of the context's red share: the one place a stall changes.
Snapshot(k) ==
    /\ ~fresh[k]
    /\ fresh' = [fresh EXCEPT ![k] = TRUE]
    /\ UNCHANGED <<n, r, rep, attempts, wrong, wAfter>>
    /\ IF stall[k] = "none" THEN
         IF Cond(k) THEN
            \* Open: one record, the clock starts, the episode's rows are zero.
            /\ stall' = [stall EXCEPT ![k] = "open"]
            /\ el' = [el EXCEPT ![k] = 0]
            /\ reads' = [reads EXCEPT ![k] = 0]
            /\ excRows' = [excRows EXCEPT ![k] = 0]
            /\ lanes' = [lanes EXCEPT ![k] = 0]
            /\ qs' = [qs EXCEPT ![k] = 0]
            /\ nopen' = [nopen EXCEPT ![k] = 1]
            /\ openedOK' = [openedOK EXCEPT ![k] = TrueCond(k)]
            /\ blocked' = [blocked EXCEPT ![k] = (BlockOn = "open")]
            /\ UNCHANGED <<excEl, askEl, askRep, lastClose>>
         ELSE
            UNCHANGED <<stall, el, reads, excRows, lanes, qs, nopen, openedOK,
                        blocked, excEl, askEl, askRep, lastClose>>
       ELSE
         IF Cond(k) THEN
            LET newExc == stall[k] = "open" /\ ExcEnabled /\ el[k] >= ExcAfter
                repExc == ExcRows = "every" /\ stall[k] \in {"excepted", "asked"}
                newQ == stall[k] = "excepted" /\ el[k] >= QAfter
                        /\ (AskWhenRepair \/ ~rep[k])
                repQ == QRows = "every" /\ stall[k] = "asked"
            IN
            /\ reads' = [reads EXCEPT ![k] = 0]
            /\ el' = [el EXCEPT ![k] = IF ClockResets THEN 0 ELSE el[k]]
            /\ stall' = [stall EXCEPT ![k] =
                            IF newExc THEN "excepted"
                            ELSE IF newQ THEN "asked" ELSE stall[k]]
            /\ excRows' = [excRows EXCEPT ![k] =
                              IF newExc \/ repExc THEN excRows[k] + 1 ELSE excRows[k]]
            /\ lanes' = [lanes EXCEPT ![k] =
                            IF newExc /\ LaneWithRow THEN lanes[k] + 1 ELSE lanes[k]]
            /\ qs' = [qs EXCEPT ![k] = IF newQ \/ repQ THEN qs[k] + 1 ELSE qs[k]]
            /\ blocked' = [blocked EXCEPT ![k] =
                              IF newExc /\ BlockOn = "exception" THEN TRUE ELSE blocked[k]]
            /\ excEl' = [excEl EXCEPT ![k] = IF newExc THEN el[k] ELSE excEl[k]]
            /\ askEl' = [askEl EXCEPT ![k] = IF newQ THEN el[k] ELSE askEl[k]]
            /\ askRep' = [askRep EXCEPT ![k] = IF newQ THEN rep[k] ELSE askRep[k]]
            /\ nopen' = [nopen EXCEPT ![k] =
                            IF StallKey = "snapshot" THEN nopen[k] + 1 ELSE nopen[k]]
            /\ UNCHANGED <<openedOK, lastClose>>
         ELSE
            /\ reads' = [reads EXCEPT ![k] = Min(reads[k] + 1, Cap)]
            /\ IF CloseEnabled /\ reads[k] + 1 >= ClearReads THEN
                  \* Close: the stall ends and the block lifts.
                  /\ stall' = [stall EXCEPT ![k] = "none"]
                  /\ el' = [el EXCEPT ![k] = 0]
                  /\ nopen' = [nopen EXCEPT ![k] = 0]
                  /\ lastClose' = [lastClose EXCEPT ![k] = Min(reads[k] + 1, Cap)]
                  /\ blocked' = [blocked EXCEPT ![k] =
                                    IF ClearBlockOnClose THEN FALSE ELSE blocked[k]]
               ELSE
                  /\ UNCHANGED <<stall, el, nopen, lastClose, blocked>>
            /\ UNCHANGED <<excRows, lanes, qs, openedOK, excEl, askEl, askRep>>

\* The controller tries to send a repair worker at the context.
Attempt(k) ==
    /\ attempts < MaxAttempts
    /\ attempts' = attempts + 1
    /\ IF Allowed(k) THEN
          /\ wAfter' = [wAfter EXCEPT ![k] =
                           IF stall[k] \in {"excepted", "asked"} THEN wAfter[k] + 1
                           ELSE wAfter[k]]
          /\ UNCHANGED wrong
       ELSE
          /\ wrong' = (wrong \/ stall[k] \in {"none", "open"})
          /\ UNCHANGED wAfter
    /\ UNCHANGED <<n, r, rep, fresh, stall, el, reads, excRows, lanes, qs, blocked,
                   nopen, openedOK, excEl, askEl, askRep, lastClose>>

Next == \E k \in Ctx : Env(k) \/ RepFlip(k) \/ Snapshot(k) \/ Attempt(k) \/ Tick

Spec == Init /\ [][Next]_vars /\ WF_vars(Tick) /\ \A k \in Ctx : WF_vars(Snapshot(k))

TypeOK ==
    /\ \A k \in Ctx :
         /\ n[k] \in 0..MaxPR /\ r[k] \in 0..MaxPR /\ r[k] <= n[k]
         /\ stall[k] \in {"none", "open", "excepted", "asked"}
         /\ el[k] \in 0..Cap /\ reads[k] \in 0..Cap
         /\ rep[k] \in BOOLEAN /\ fresh[k] \in BOOLEAN /\ blocked[k] \in BOOLEAN
    /\ attempts \in 0..MaxAttempts /\ wrong \in BOOLEAN

\* A stall opens only on the share and count the design states.
OpensOnlyOnThreshold == \A k \in Ctx : stall[k] # "none" => openedOK[k]

\* A snapshot that reads the share and count holding leaves the context stalled,
\* at the boundary included: exactly Pct percent, exactly MinPrs pull requests.
ThresholdBoundaryOpens == \A k \in Ctx : (fresh[k] /\ TrueCond(k)) => stall[k] # "none"

\* One stall per context, however many snapshots read the same red.
OneStallPerContext == \A k \in Ctx : nopen[k] <= 1

\* The exception row waits the design's time and is written once.
ExceptionNotEarly ==
    \A k \in Ctx : stall[k] \in {"excepted", "asked"} => excEl[k] >= DesignExc

\* One exception row, one question, one cause lane per episode.
OneRowPerEpisode ==
    \A k \in Ctx : excRows[k] <= 1 /\ lanes[k] <= 1 /\ qs[k] <= 1

\* The exception row and the cause lane go out together.
CauseLaneWithException ==
    \A k \in Ctx : stall[k] \in {"excepted", "asked"} => excRows[k] = 1 /\ lanes[k] = 1

\* The question waits its time, and is not asked while a repair is eligible.
QuestionNotEarly == \A k \in Ctx : stall[k] = "asked" => askEl[k] >= DesignQ
QuestionOnlyUnrepaired == \A k \in Ctx : stall[k] = "asked" => ~askRep[k]

\* No repair worker is sent at a context once its stall has gone to exception.
NoWorkerAfterException == \A k \in Ctx : wAfter[k] = 0

\* The controller refuses only at an excepted or asked stall, and only there.
NoWrongRefusal == ~wrong

\* A stall closes only on consecutive clear snapshots.
ClosedOnlyAfterReads == \A k \in Ctx : lastClose[k] >= DesignClear

\* An open stall goes on to exception or closes.
OpenStallEscalatesOrCloses ==
    \A k \in Ctx : [](stall[k] = "open" => <>(stall[k] # "open"))

\* A stall whose context stays clear closes.
ClearedStallCloses ==
    \A k \in Ctx : [](stall[k] # "none" => <>(stall[k] = "none" \/ Cond(k)))

\* Witnesses: the states the invariants talk about are reachable.
NeverExcepted == \A k \in Ctx : stall[k] # "excepted"
NeverAsked == \A k \in Ctx : stall[k] # "asked"
NeverClosedAfterExcept == \A k \in Ctx : lastClose[k] = 99 \/ excRows[k] = 0
=============================================================================
