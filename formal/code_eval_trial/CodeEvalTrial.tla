---------------------------- MODULE CodeEvalTrial ----------------------------
(* OMN-23627 (E1, AC10) model before build.
   The state_io row of node_code_eval_trial_orchestrator: one row per run, keyed by the
   run's correlation id, moved by a pure reducer. Plan: the local-model code eval plan,
   revision 44, task E1 (State, Message flow, Legs, Close rule) and rules 2, 3 and 7.

   Facts modelled:
   * Each task has a control slot. The control is one score request with no model and no
     unit; its verdict is held (pass), notheld (fail) or unrun (unscorable or expired:
     could not be run). Only a held control gives its task's sequences tries.
   * Each (task, model) pair is a sequence with one slot: none (control unread), pending,
     try (unit in flight), scoreq (scorable end, score queued), score (score in flight),
     done, excluded (control did not hold, no tries).
   * A verdict is pass or nopass. E2's fail and unscorable and the harness-unscorable ends
     (a unit that cannot be scored, a nudge expiry) all give nopass: rule 7 treats them
     alike for the sequence (an unscorable try takes its ordinal and the sequence goes on).
     The difference between them matters only to the per-budget counts (rule 7, AC13), which
     are a function of the closed row and are tested in the reducer, not modelled here.
   * A sequence holds up to Bound tries. A try's verdict ends the sequence on pass or at
     the bound, else queues the next try. An unscorable try takes its ordinal and the
     sequence goes on (rule 2, rule 7).
   * At most one score is in flight in the run (one slot for the control or a try).
   * A try is placed only on a free host (hostsUp, an environment flag) and only after its
     task's control reads pass.
   * The nudge (a tick per open run) expires a try or score in flight past its deadline
     (timeout plus slack) as unscorable(harness); an expired control is unrun. The tick
     route may lose the run (nudging goes FALSE once: a restart).
   * A message names a unit or score identity. It changes the row only if that identity is
     in flight; any other message is late or unknown and changes nothing. Messages may be
     delivered any number of times (at-least-once).
   * The run closes once, on the leg where every task has its control verdict and every
     sequence has ended. The contract's completion_bound ends a stalled row (no write for
     CompletionBound ticks) with one abandoned close, without the close rule.

   Two modes (constant Timed). Timed = FALSE leaves time out: a nudge may expire any item in
   flight, the completion bound may end the run in any open state, and the age counters
   stay 0 (this keeps the state space small for the 2 task, 2 model, bound 3 safety run).
   Timed = TRUE adds the clock: since counts ticks since the last row write (a leg that changes nothing writes
   nothing); tryAge and scoreAge count ticks since send. All saturate.

   Mutations (each must violate an invariant or property, see M*.cfg):
   * CloseWithTryPending: the close rule allows a pending or in-flight try.
   * LateAnswerChangesRow: a late answer is applied to the row.
   * PlaceBeforeControl: a try is placed without its control passing.
   * ConcurrentScores: a second score is sent while one is in flight.
   * NoNudgeExpiry: the nudge never expires an item in flight.
   * BoundBelowTimeout: the completion bound is not above the try timeout (cfg only). *)
(* Seam: the model's actions against the plan's message-flow rows (plan revision 44, E1).
     Run       Init (the run leg's result: every control queued). Not expressed: reading the overlay,
               pinning the suite, admission and exclusion counts, size bands, the configuration
               error and dead letter (an ASSUME), publish_cells, the tick route's own state.
     Try       Place (a free host: hostsUp; the control read pass). Unit id and host are not named.
     Unit ans. DeliverUnit(s, o, k): scorable (E2 scores the diff, including timed_out) or
               unscorable (refused, infra_error, failure terminal, draft_not_recoverable, the
               loop could not run: not sent to E2). Rule 2's five rows are these two classes.
     Score     SendControl, SendTryScore (one in flight) and DeliverScore(id, v).
     Row       Record, the verdict write. Publication of the context-ROI row is the same leg and
               is not a separate step here; redelivery idempotence of that row is not modelled.
     Capacity  hostsUp only (the readings and max_hosts are not modelled).
     Tick      Tick and EnvNudging (the tick route loses the run).
     Nudge     ExpireTry, ExpireScore (processed before time moves on), and Abandon's time.
     Close     Close, Abandon.
     Cells, Paired   Not expressible: both are the stateless close route after the closed row.
     Budgets 1, 3, 5   Not expressible: the per-budget reading is a function of the closed row;
               the model's Bound is the largest budget. *)
EXTENDS Naturals, FiniteSets, TLC

CONSTANTS Nil, Tasks, Models, Bound,
          TryDeadline, ScoreDeadline, CompletionBound,
          CloseWithTryPending, LateAnswerChangesRow, PlaceBeforeControl,
          ConcurrentScores, NoNudgeExpiry, BoundBelowTimeout, Timed, HostsFlap, NudgeLoss,
          UnknownAnswerChangesRow, DropOrdinalGuard, ContinueAfterPass

\* The run leg raises a configuration error (dead letter, no row) when the timeouts plus
\* slack do not fit under the completion bound, so a row exists only under this assumption.
ASSUME BoundBelowTimeout \/ (CompletionBound > TryDeadline /\ CompletionBound > ScoreDeadline)

VARIABLES ctrl,         \* task -> control slot
          slot,         \* sequence -> slot
          ord,          \* sequence -> current try ordinal
          vd,           \* sequence -> ordinal -> verdict recorded in the row
          scoreFlight,  \* the one score in flight (an id) or Nil
          since, tryAge, scoreAge,
          hostsUp, nudging,
          np,           \* sequence -> tries placed so far
          closed, abandoned, closeCount,
          lastLate      \* ghost: the last step delivered a late or unknown message

S == Tasks \X Models
TryIds == Tasks \X Models \X (1..Bound)
CtlIds == Tasks \X {"ctl"} \X {0}
Ids == TryIds \cup CtlIds

rowVars == <<ctrl, slot, ord, vd, scoreFlight, np, closed, abandoned, closeCount>>
envVars == <<hostsUp, nudging>>
timeVars == <<since, tryAge, scoreAge>>
vars == <<ctrl, slot, ord, vd, scoreFlight, np, since, tryAge, scoreAge,
          hostsUp, nudging, closed, abandoned, closeCount, lastLate>>

\* Try o of sequence s was placed (its unit was sent), derived from the row.
Sent(s, o) == o < ord[s] \/ (o = ord[s] /\ slot[s] \in {"try", "scoreq", "score", "done"})
\* A score for try o of sequence s was sent (a superset: it holds from the send on).
ScoreSent(s, o) == o < ord[s] \/ (o = ord[s] /\ slot[s] \in {"score", "done"})
Min(a, b) == IF a < b THEN a ELSE b
MaxDeadline == IF TryDeadline > ScoreDeadline THEN TryDeadline ELSE ScoreDeadline
CtrlFinal(t) == ctrl[t] \in {"held", "notheld", "unrun"}
SeqEnded(s) == slot[s] \in {"done", "excluded"}
CloseRule == (\A t \in Tasks : CtrlFinal(t)) /\ (\A s \in S : SeqEnded(s))
CloseRuleMutated == (\A t \in Tasks : CtrlFinal(t)) /\ (\A s \in S : slot[s] \in {"done", "excluded", "pending", "try"})
CanClose == IF CloseWithTryPending THEN CloseRuleMutated ELSE CloseRule
CtlOf(v) == IF v = "pass" THEN "held" ELSE IF v = "fail" THEN "notheld" ELSE "unrun"
VdOf(v) == IF v = "pass" THEN "pass" ELSE "nopass"
AnyExpired == (\E s \in S : slot[s] = "try" /\ tryAge[s] >= TryDeadline)
              \/ (scoreFlight # Nil /\ scoreAge >= ScoreDeadline)

Init == /\ ctrl = [t \in Tasks |-> "queued"]
        /\ slot = [s \in S |-> IF PlaceBeforeControl THEN "pending" ELSE "none"]
        /\ ord = [s \in S |-> 1]
        /\ vd = [s \in S |-> [o \in 1..Bound |-> "none"]]
        /\ scoreFlight = Nil
        /\ np = [s \in S |-> 0]
        /\ since = 0 /\ tryAge = [s \in S |-> 0] /\ scoreAge = 0
        /\ hostsUp = TRUE /\ nudging = TRUE
        /\ closed = FALSE /\ abandoned = FALSE /\ closeCount = 0
        /\ lastLate = FALSE

\* A verdict v for try ordinal o of sequence s: record it, end the sequence on pass or at the
\* bound, else queue the next try.
Record(s, o, v) ==
  /\ vd' = [vd EXCEPT ![s][o] = v]
  /\ ctrl' = ctrl
  /\ IF (v = "pass" /\ ~ContinueAfterPass) \/ o = Bound
       THEN /\ slot' = [slot EXCEPT ![s] = "done"] /\ ord' = ord
       ELSE /\ slot' = [slot EXCEPT ![s] = "pending"] /\ ord' = [ord EXCEPT ![s] = o + 1]

\* The control of task t gets verdict c: its sequences get tries (held) or end excluded.
CtlResult(t, c) ==
  /\ ctrl' = [ctrl EXCEPT ![t] = c]
  /\ slot' = [s \in S |-> IF s[1] = t /\ slot[s] = "none"
                          THEN (IF c = "held" THEN "pending" ELSE "excluded") ELSE slot[s]]
  /\ UNCHANGED <<ord, vd>>

(* ---- reducer legs ---- *)

SendControl(t) ==
  /\ ~closed /\ ctrl[t] = "queued"
  /\ ConcurrentScores \/ scoreFlight = Nil
  /\ ctrl' = [ctrl EXCEPT ![t] = "inflight"]
  /\ scoreFlight' = <<t, "ctl", 0>>
  /\ since' = 0 /\ scoreAge' = 0 /\ lastLate' = FALSE
  /\ UNCHANGED <<slot, ord, vd, tryAge, hostsUp, nudging, np, closed, abandoned, closeCount>>

SendTryScore(s) ==
  /\ ~closed /\ slot[s] = "scoreq"
  /\ ConcurrentScores \/ scoreFlight = Nil
  /\ slot' = [slot EXCEPT ![s] = "score"]
  /\ scoreFlight' = <<s[1], s[2], ord[s]>>
  /\ since' = 0 /\ scoreAge' = 0 /\ lastLate' = FALSE
  /\ UNCHANGED <<ctrl, ord, vd, tryAge, hostsUp, nudging, np, closed, abandoned, closeCount>>

Place(s) ==
  /\ ~closed /\ slot[s] = "pending" /\ hostsUp
  /\ PlaceBeforeControl \/ ctrl[s[1]] = "held"
  /\ slot' = [slot EXCEPT ![s] = "try"]
  /\ np' = [np EXCEPT ![s] = np[s] + 1]
  /\ tryAge' = [tryAge EXCEPT ![s] = 0]
  /\ since' = 0 /\ lastLate' = FALSE
  /\ UNCHANGED <<ctrl, ord, vd, scoreFlight, scoreAge, hostsUp, nudging, closed, abandoned, closeCount>>

\* The host answers (or re-delivers an answer for) the unit of try o of sequence s, content k.
\* The host may never answer (lost); an answer for a try not in flight, or never sent, is late or unknown.
DeliverUnit(s, o, k) ==
  /\ IF ~closed /\ slot[s] = "try" /\ (ord[s] = o \/ (DropOrdinalGuard /\ o < ord[s]))
       THEN /\ since' = 0 /\ lastLate' = FALSE
            /\ tryAge' = [tryAge EXCEPT ![s] = 0]
            /\ IF k = "scorable"
                 THEN /\ slot' = [slot EXCEPT ![s] = "scoreq"] /\ UNCHANGED <<ctrl, ord, vd>>
                 ELSE Record(s, o, "nopass")
            /\ UNCHANGED <<scoreFlight, scoreAge, hostsUp, nudging, np, closed, abandoned, closeCount>>
       ELSE \* late or unknown: changes nothing
            /\ lastLate' = TRUE
            /\ IF ((LateAnswerChangesRow /\ Sent(s, o)) \/ (UnknownAnswerChangesRow /\ ~Sent(s, o))) /\ k = "unscorable"
                 THEN vd' = [vd EXCEPT ![s][o] = "nopass"] ELSE UNCHANGED vd
            /\ UNCHANGED <<ctrl, slot, ord, scoreFlight, timeVars, hostsUp, nudging, np, closed, abandoned, closeCount>>

\* E2 answers (or re-delivers an answer for) score id with verdict v.
DeliverScore(id, v) ==
  /\ IF ~closed /\ scoreFlight = id
        /\ (id[2] = "ctl" \/ (slot[<<id[1], id[2]>>] = "score" /\ ord[<<id[1], id[2]>>] = id[3]))
       THEN /\ since' = 0 /\ lastLate' = FALSE /\ scoreFlight' = Nil /\ scoreAge' = 0
            /\ IF id[2] = "ctl"
                 THEN CtlResult(id[1], CtlOf(v))
                 ELSE Record(<<id[1], id[2]>>, id[3], VdOf(v))
            /\ UNCHANGED <<tryAge, hostsUp, nudging, np, closed, abandoned, closeCount>>
       ELSE \* late or unknown: changes nothing
            /\ lastLate' = TRUE
            /\ IF (LateAnswerChangesRow /\ (IF id[2] = "ctl" THEN ctrl[id[1]] # "queued" ELSE ScoreSent(<<id[1], id[2]>>, id[3])))
                  \/ (UnknownAnswerChangesRow /\ (IF id[2] = "ctl" THEN ctrl[id[1]] = "queued" ELSE ~ScoreSent(<<id[1], id[2]>>, id[3])))
                 THEN IF id[2] = "ctl"
                        THEN /\ ctrl' = [ctrl EXCEPT ![id[1]] = CtlOf(v)] /\ UNCHANGED vd
                        ELSE /\ vd' = [vd EXCEPT ![<<id[1], id[2]>>][id[3]] = VdOf(v)] /\ UNCHANGED ctrl
                 ELSE UNCHANGED <<ctrl, vd>>
            /\ UNCHANGED <<slot, ord, scoreFlight, timeVars, hostsUp, nudging, np, closed, abandoned, closeCount>>

\* The nudge (tick) is processed before time moves on: expiry is enabled past a deadline.
ExpireTry(s) ==
  /\ ~NoNudgeExpiry /\ nudging /\ ~closed
  /\ slot[s] = "try" /\ (~Timed \/ tryAge[s] >= TryDeadline)
  /\ Record(s, ord[s], "nopass")
  /\ tryAge' = [tryAge EXCEPT ![s] = 0]
  /\ since' = 0 /\ lastLate' = FALSE
  /\ UNCHANGED <<scoreFlight, scoreAge, hostsUp, nudging, np, closed, abandoned, closeCount>>

ExpireScore ==
  /\ ~NoNudgeExpiry /\ nudging /\ ~closed
  /\ scoreFlight # Nil /\ (~Timed \/ scoreAge >= ScoreDeadline)
  /\ IF scoreFlight[2] = "ctl"
       THEN CtlResult(scoreFlight[1], "unrun")
       ELSE Record(<<scoreFlight[1], scoreFlight[2]>>, scoreFlight[3], "nopass")
  /\ scoreFlight' = Nil /\ scoreAge' = 0
  /\ since' = 0 /\ lastLate' = FALSE
  /\ UNCHANGED <<tryAge, hostsUp, nudging, np, closed, abandoned, closeCount>>

Close ==
  /\ ~closed /\ CanClose
  /\ closed' = TRUE /\ closeCount' = closeCount + 1
  /\ since' = 0 /\ lastLate' = FALSE
  /\ UNCHANGED <<ctrl, slot, ord, vd, scoreFlight, np, tryAge, scoreAge, hostsUp, nudging, abandoned>>

\* The contract's completion_bound: a row with no write for CompletionBound ticks.
Abandon ==
  /\ ~closed /\ (~Timed \/ since > CompletionBound)
  /\ closed' = TRUE /\ abandoned' = TRUE /\ closeCount' = closeCount + 1
  /\ since' = 0 /\ lastLate' = FALSE
  /\ UNCHANGED <<ctrl, slot, ord, vd, scoreFlight, np, tryAge, scoreAge, hostsUp, nudging>>

\* Work a leg dispatches before time moves on: a score to send, a try to place on a free host,
\* a close the rule allows.
Dispatchable ==
  \/ (scoreFlight = Nil /\ ((\E t \in Tasks : ctrl[t] = "queued") \/ (\E s \in S : slot[s] = "scoreq")))
  \/ (hostsUp /\ \E s \in S : slot[s] = "pending" /\ ctrl[s[1]] = "held")
  \/ CanClose

Tick ==
  /\ Timed
  /\ closed \/ ((~nudging \/ NoNudgeExpiry \/ ~AnyExpired) /\ ~Dispatchable)
  /\ since' = Min(since + 1, CompletionBound + 1)
  /\ tryAge' = [s \in S |-> IF slot[s] = "try" THEN Min(tryAge[s] + 1, TryDeadline) ELSE 0]
  /\ scoreAge' = IF scoreFlight # Nil THEN Min(scoreAge + 1, ScoreDeadline) ELSE 0
  /\ lastLate' = FALSE
  /\ UNCHANGED <<rowVars, envVars>>

(* ---- the world: the host, E2, hosts, the tick route (no fairness) ---- *)

EnvHosts ==
  /\ HostsFlap
  /\ hostsUp' = ~hostsUp /\ lastLate' = FALSE
  /\ UNCHANGED <<rowVars, timeVars, nudging>>

EnvNudging ==
  /\ NudgeLoss
  /\ nudging /\ nudging' = FALSE /\ lastLate' = FALSE
  /\ UNCHANGED <<rowVars, timeVars, hostsUp>>

Terminated == closed /\ UNCHANGED vars

Next == \/ \E t \in Tasks : SendControl(t)
        \/ \E s \in S : SendTryScore(s)
        \/ \E s \in S : Place(s)
        \/ \E s \in S, o \in 1..Bound, k \in {"scorable", "unscorable"} : DeliverUnit(s, o, k)
        \/ \E i \in Ids, v \in {"pass", "fail", "unscorable"} : DeliverScore(i, v)
        \/ \E s \in S : ExpireTry(s)
        \/ ExpireScore
        \/ Close
        \/ Abandon
        \/ Tick
        \/ EnvHosts
        \/ EnvNudging
        \/ Terminated

Fair == /\ \A t \in Tasks : WF_vars(SendControl(t))
        /\ \A s \in S : WF_vars(SendTryScore(s))
        /\ \A s \in S : WF_vars(Place(s))
        /\ \A s \in S : WF_vars(ExpireTry(s))
        /\ WF_vars(ExpireScore)
        /\ WF_vars(Close)
        /\ WF_vars(Abandon)
        /\ SF_vars(Tick)  \* time always passes, even while hosts flap

Spec == Init /\ [][Next]_vars
LiveSpec == Spec /\ Fair

Sym == Permutations(Tasks) \cup Permutations(Models)

(* ---- invariants ---- *)

TypeOK ==
  /\ ctrl \in [Tasks -> {"queued", "inflight", "held", "notheld", "unrun"}]
  /\ slot \in [S -> {"none", "pending", "try", "scoreq", "score", "done", "excluded"}]
  /\ ord \in [S -> 1..Bound]
  /\ vd \in [S -> [1..Bound -> {"none", "pass", "nopass"}]]
  /\ scoreFlight \in Ids \cup {Nil}
  /\ since \in 0..(CompletionBound + 1)
  /\ tryAge \in [S -> 0..TryDeadline]
  /\ np \in [S -> 0..(Bound + 1)]
  /\ scoreAge \in 0..ScoreDeadline
  /\ hostsUp \in BOOLEAN /\ nudging \in BOOLEAN
  /\ closed \in BOOLEAN /\ abandoned \in BOOLEAN /\ closeCount \in 0..2 /\ lastLate \in BOOLEAN

\* The run closes only under the close rule, unless the completion bound abandoned it.
CloseOnlyUnderRule == closed /\ ~abandoned => CloseRule

\* Exactly one close event is published, ever.
ExactlyOneClose == /\ closeCount <= 1
                   /\ (closed <=> closeCount = 1)

\* No try before its task's control reads pass.
NoTryBeforeControl ==
  \A s \in S : slot[s] \in {"try", "scoreq", "score", "done"} => ctrl[s[1]] = "held"

\* At most one score in flight: the control slot and the sequence slots together.
AtMostOneScore ==
  Cardinality({t \in Tasks : ctrl[t] = "inflight"}) + Cardinality({s \in S : slot[s] = "score"}) <= 1

\* A sequence never holds more verdicts than the bound, and a verdict sits only at an ordinal reached.
VerdictsReached ==
  \A s \in S, o \in 1..Bound : vd[s][o] # "none" => o <= ord[s]

\* A sequence that ended on pass has no later try; a sequence done by the bound has Bound verdicts.
DoneIsTerminal ==
  \A s \in S : slot[s] = "done" =>
    (\E o \in 1..Bound : vd[s][o] = "pass" /\ \A p \in (o + 1)..Bound : vd[s][p] = "none")
    \/ (\A o \in 1..Bound : vd[s][o] # "none")

\* A sequence places at most Bound tries (a stale answer never moves its ordinal back).
AtMostBoundTries == \A s \in S : np[s] <= Bound

\* Stop at the first pass: no verdict is recorded after a pass.
StopAtFirstPass ==
  \A s \in S, o \in 1..Bound : vd[s][o] = "pass" => \A p \in (o + 1)..Bound : vd[s][p] = "none"

(* ---- action properties (checked on every step) ---- *)

\* A recorded verdict or control verdict never changes: a late answer changes nothing.
VerdictsStable ==
  [][ /\ \A s \in S, o \in 1..Bound : vd[s][o] # "none" => vd'[s][o] = vd[s][o]
      /\ \A t \in Tasks : CtrlFinal(t) => ctrl'[t] = ctrl[t] ]_vars

\* A closed row is frozen.
ClosedRowFrozen == [][ closed => UNCHANGED rowVars ]_vars

\* A late or unknown message changes no part of the row.
LateNoEffect == [][ lastLate' => UNCHANGED rowVars ]_vars

\* The completion bound abandons a run only when nothing in flight is inside its deadline.
AbandonOnlyPastDeadline ==
  [][ (abandoned' /\ ~abandoned) =>
        /\ \A s \in S : slot[s] = "try" => tryAge[s] >= TryDeadline
        /\ scoreFlight # Nil => scoreAge >= ScoreDeadline ]_vars

\* While nudges arrive, nothing in flight past its deadline is left to the completion bound.
NudgeExpiresBeforeAbandon ==
  [][ (abandoned' /\ ~abandoned /\ nudging) => ~AnyExpired ]_vars

(* ---- liveness (LiveSpec) ---- *)

EverySequenceEnds == \A s \in S : <>(SeqEnded(s) \/ abandoned)
RunCloses == <>closed
\* With the tick route and hosts alive, the run closes by the close rule, never abandoned, even
\* when every answer is lost (the nudge expires it).
NormalClose == <>(closed /\ ~abandoned)
EverySequenceEndsNormally == \A s \in S : <>SeqEnded(s)
=============================================================================
