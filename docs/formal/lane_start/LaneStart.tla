------------------------------ MODULE LaneStart ------------------------------
(* Starting one lab lane by key: the CLAIM, the engine start and the one TERMINAL.
   Task P2 (OMN-23726) of the six-workflows plan. Models the handler order of
   node_remote_lane_start_orchestrator (omnimarket, orchestration/core.py run()):
   request, placement, CLAIM, provisioning, engine start (detached), supervision,
   one closing row. Structure and naming follow RemoteLaneStop.tla (the stop side,
   whose single-closer ledger rule this model shares); the fence idea (a retry
   may not start a second engine while an earlier attempt could still be live)
   follows LanderLease.tla (one permit, one kept tree).

   One lane KEY. A start request carries an attempt number (1..MaxAttempts); the
   CLAIM request id is uuid5(lane, attempt), so the ledger refuses a second CLAIM
   for the same attempt only. Per attempt the leader walks:
     requested -> (refused | observed | claimed)
     claimed   -> StartEngine: running | failed | unresolved (the detached start did
                  not settle; the engine may or may not exist)
     running   -> the engine exits -> the leader sees it -> the leader closes
   Who may write the lane's TERMINAL (one per CLAIM; the ledger refuses a second):
     the lane   its own closeout, while or after the engine runs
     the leader the node's closing row (outcome ended | start-failed); when the
                lane already closed itself the leader writes a STATUS instead
     the stopper the stop node / reaper, for a CLAIM left open (leader dead or the
                start unresolved), only once the engine process is gone
   Retry asks for attempt a+1 after attempt a ended without engine. The orchestrator
   alone has no key fence (only the per-attempt request id); RetryGuard is the fence
   this model requires of whoever re-requests.

   Time is abstract; the engine, the leader and the stopper move at any rate.

   This model states what the lane start NEEDS (the design, Model.cfg) and, in the
   asbuilt_* configs, what the code does today, so the gap is a named list. The
   design requires seven things of the pieces around the start node (R1..R7); the
   orchestrator alone provides none of R1, R2, R3 (see the review that failed the
   first version of this model, results/review_round1.txt):
     R1  a stop never closes a CLAIM whose live leader has not started the engine
         (StopperWaitsLeader; the stop node closes an empty-status receipt today).
         The model's stopper can tell a live leader from a dead one; a real stop node
         cannot, so R1 is a fence, not a wait: the leader checks its CLAIM is still
         open (or holds a lease the stopper honours) immediately before the engine
         start, and the stopper respects the lease
     R2  something closes every CLAIM the leader left open, once no process tree
         exists for the lane: unresolved start, stop-refused, a refused or failed
         closing append, a dead leader, an orphan CLAIM whose refused append landed
         later (Reaper; no reaper exists)
     R3  a re-request of the key reads a ledger cell that says "the engine never
         started" (outcome start-failed) and no CLAIM stays open; a key whose start
         went unresolved is dead (RetryGuard = closed_unstarted_ledger). The orchestrator writes that cell only
         on the three paths before the launch (a fatal provisioning step, a worktree
         not prepared, the transport's failed result) and never for unresolved, for
         an engine that ran, or from the stopper. Today the
         TERMINAL says outcome=failed for a start failure and for an engine that
         ran and failed, and the node fences on lane+attempt only)
     R4  the ledger refuses a second TERMINAL for one CLAIM, and a second CLAIM for
         one attempt, on the leader's append path as it does on the stop node's
         (SingleCloser; UNMEASURED for the leader's external ledger writer and on
         the ledger_write_via bus path; the model has one leader per attempt, so a
         second same-attempt leader is not exercised. LeaderChecksSelfClose only
         chooses STATUS over TERMINAL and adds no safety beyond R4)
     R6  re-requests of a key are serialized: attempt a+1 is issued only after
         attempt a's leader has answered (its completed event) or its CLAIM is
         closed. A refused attempt, or a leader that died before its CLAIM, writes
         no ledger row, so a re-requester that reads only the ledger cannot tell it
         from one still placing (RetryInflight=TRUE). One serialized re-requester
         per key; neither the node nor the fill is that component today (the fill
         orchestrator lane owns the caller side, not read here)
     R7  (assumption LateStart = FALSE) a start the leader reports unresolved, or
         one interrupted by the leader's death, cannot launch after the reaper has
         seen no process tree: the reaper kills any tree for that run before it
         closes, or the transport proves it cannot land late (UNMEASURED)
     R5  the key is the lane name: the CLAIM id is uuid5(lane, attempt) and does
         not hold the key cell, so a retry under another lane name is outside this
         model and outside the dedupe; a run_id reused across attempts shares the
         host run directory and is also outside

   Properties (mutations below break the first six and the two liveness ones; the rest
   are broken by the named config or follow from others):
     StartedAtMostOnce         a lane key has at most one engine process ever
     ClaimBeforeEngine         an engine exists only for an attempt that holds a CLAIM
     NoTerminalWithoutClaim    every TERMINAL closes a CLAIM of its own attempt
     AtMostOneTerminal         at most one TERMINAL per CLAIM
     StartedTerminalPerKey     at most one TERMINAL closes a started lane of the key (a retried
                               key may also hold a start-failed TERMINAL of an engineless attempt)
     LeaderTerminalAfterExit   leader and stopper write only once the process is gone
     NoRetryAfterUnresolved    a key whose start went unresolved is never re-requested
     ObserveWritesNothing      observe mode: no CLAIM, no engine, no TERMINAL
     StartedReachesTerminal    every started lane reaches a TERMINAL (liveness)
     ClaimReachesTerminal      every accepted CLAIM reaches a TERMINAL (liveness)
     NoStartAfterTerminal      no engine appears for an attempt already closed (action)
   Mutations (cfg constants) that must break them:
     RetryInflight=TRUE          a re-request while the earlier attempt is in flight
                                 -> StartedAtMostOnce
     RetryGuard="any"            a retry never looks at the key  -> StartedAtMostOnce
     RetryGuard="no_open_claim"  retry after a closed, started lane -> StartedAtMostOnce
     ClaimFirst=FALSE            the engine starts before the CLAIM -> ClaimBeforeEngine
     SingleCloser=FALSE          the ledger takes every TERMINAL -> AtMostOneTerminal
     TerminalNeedsClaim=FALSE    a TERMINAL for an unclaimed attempt -> NoTerminalWithoutClaim
     ObserveGuard=FALSE          observe mode may claim -> ObserveWritesNothing
     Reaper=FALSE                no one closes an abandoned CLAIM -> StartedReachesTerminal
     StopperWaitsLeader=FALSE    the stopper closes a CLAIM whose live leader has not yet
                                 started the engine -> LeaderTerminalAfterExit
   Also: NoRetryAfterUnresolved is broken by RetryGuard="any" and "no_open_claim" (checked
   alone in chk_unresolved_stays_dead.cfg, which passes under the design);
   NoStartAfterTerminal by haz_late_start (and by mut_stopper_closes_prestart);
   ClaimReachesTerminal by the asbuilt_* stopper; StartedTerminalPerKey follows from
   StartedAtMostOnce and AtMostOneTerminal.
   As built (asbuilt_* cfgs, each is expected VIOLATED today):
     StopperCode=TRUE            the stop node as coded: no leader wait, refuses unresolved,
                                 stop-refused and ledger-refused receipts
                                 -> LeaderTerminalAfterExit (safety) and
                                    StartedReachesTerminal (liveness)
     CodeOutcomes=TRUE with RetryGuard="ledger_failed"
                                 the only ledger cell a re-requester can read says failed
                                 for both a failed start and a failed run -> StartedAtMostOnce
   Assumption (UNMEASURED on the running node, the hazard cfg shows it): LateStart=FALSE,
   a detached start the leader reports `unresolved`, or that died with its leader, does
   not land later. LateStart=TRUE lets it land after a stopper closed the CLAIM. *)
EXTENDS Integers, FiniteSets

CONSTANTS MaxAttempts, Mode,
          RetryGuard, ClaimFirst, SingleCloser, LeaderChecksSelfClose,
          TerminalNeedsClaim, ObserveGuard, Reaper, StopperWaitsLeader, LateStart,
          \* as-built knobs (all FALSE reproduce the design)
          RetryInflight, \* a re-request is issued while the earlier attempt has not answered (R6)
          StopperCode,   \* the stop node as handler_remote_lane_stop.py behaves: no leader wait,
                         \* refuses a receipt whose status is final-looking (start-unresolved, ledger-refused)
          CloseCanFail,  \* the leader's closing append may not land (core.py:1003-1031, durable=False)
          CodeOutcomes,  \* the TERMINAL outcome cell the code writes: failed for a start failure and
                         \* for an engine that ran and exited non-zero (handler_remote_lane_result.py:215)
          SplitClose     \* _lane_closed_itself read and the append are two steps (core.py:962 vs 996)

Attempts == 1..MaxAttempts
Writers  == {"lane", "leader", "stopper"}

VARIABLES dec,    \* per attempt, the leader's STATUS/TERMINAL decision when SplitClose
          lp,     \* per attempt, the leader's phase
          eng,    \* per attempt, the engine process: "none" | "running" | "exited"
          claims, \* attempts with an accepted CLAIM row
          terms   \* TERMINAL rows: <<attempt, writer, outcome>>

vars == <<dec, lp, eng, claims, terms>>

Started == {a \in Attempts : eng[a] # "none"}
HasT(a) == \E t \in terms : t[1] = a
OutcomeOf(a) == CHOOSE t \in terms : t[1] = a

Appended(rec) == IF SingleCloser /\ HasT(rec[1]) THEN terms ELSE terms \cup {rec}

Init ==
  /\ dec = [a \in Attempts |-> "none"]
  /\ lp  = [a \in Attempts |-> IF a = 1 THEN "requested" ELSE "none"]
  /\ eng = [a \in Attempts |-> "none"]
  /\ claims = {}
  /\ terms = {}

\* ---- the leader ----------------------------------------------------------
\* Refuse: no host, unsupported shape, pool unreadable, login refused, CLAIM refused,
\* already claimed. Nothing is written for the attempt.
Refuse(a) ==
  /\ lp[a] = "requested"
  /\ lp' = [lp EXCEPT ![a] = "refused"]
  /\ UNCHANGED <<eng, claims, terms>>

\* Observe mode: the start is placed and computed, published as one event, nothing written.
Observe(a) ==
  /\ Mode = "observe" /\ lp[a] = "requested"
  /\ lp' = [lp EXCEPT ![a] = "observed"]
  /\ UNCHANGED <<eng, claims, terms>>

Claim(a) ==
  /\ lp[a] = "requested" /\ (Mode = "act" \/ ~ObserveGuard)
  /\ claims' = claims \cup {a}
  /\ lp' = [lp EXCEPT ![a] = "claimed"]
  /\ UNCHANGED <<eng, terms>>

StartEngine(a) ==
  /\ (Mode = "act" \/ ~ObserveGuard)
  /\ lp[a] = IF ClaimFirst THEN "claimed" ELSE "requested"
  /\ \/ /\ eng' = [eng EXCEPT ![a] = "running"]
        /\ lp'  = [lp  EXCEPT ![a] = "running"]
     \/ /\ lp'  = [lp  EXCEPT ![a] = "failed_seen"]   \* the engine did not start
        /\ UNCHANGED eng
     \/ /\ lp'  = [lp  EXCEPT ![a] = "unresolved"]    \* the start did not settle
        /\ eng' \in {[eng EXCEPT ![a] = "none"], [eng EXCEPT ![a] = "running"]}
  /\ UNCHANGED <<claims, terms>>

\* An unsettled detached start lands late (only when LateStart): the process appears after
\* the leader gave up.
LateStartLands(a) ==
  /\ LateStart /\ lp[a] \in {"unresolved", "gone"} /\ a \in claims /\ eng[a] = "none"
  /\ eng' = [eng EXCEPT ![a] = "running"]
  /\ UNCHANGED <<lp, claims, terms>>

EngineExit(a) ==
  /\ eng[a] = "running"
  /\ eng' = [eng EXCEPT ![a] = "exited"]
  /\ UNCHANGED <<lp, claims, terms>>

LeaderSeesExit(a) ==
  /\ lp[a] = "running" /\ eng[a] = "exited"
  /\ lp' = [lp EXCEPT ![a] = "exited_seen"]
  /\ UNCHANGED <<eng, claims, terms>>

LeaderDies(a) ==
  /\ lp[a] \in {"claimed", "running", "exited_seen", "failed_seen"}
  /\ lp' = [lp EXCEPT ![a] = "gone"]
  /\ UNCHANGED <<eng, claims, terms>>

\* ---- the one TERMINAL, three possible writers ------------------------------
ClaimOK(a) == ~TerminalNeedsClaim \/ a \in claims

LaneClose(a) ==  \* the lane's own closeout; its engine ran
  /\ a \in claims /\ eng[a] \in {"running", "exited"}
  /\ terms' = Appended(<<a, "lane", "ended">>)
  /\ UNCHANGED <<lp, eng, claims>>

LeaderOutcomes(a) ==
  IF ~CodeOutcomes THEN {IF lp[a] = "failed_seen" THEN "start-failed" ELSE "ended"}
  ELSE IF lp[a] = "failed_seen" THEN {"failed"} ELSE {"ended", "failed"}

LeaderWrite(a, d) ==
  /\ IF d = "status"
       THEN terms' = terms                     \* a STATUS row, not a TERMINAL
       ELSE \/ \E o \in LeaderOutcomes(a) : terms' = Appended(<<a, "leader", o>>)
            \/ CloseCanFail /\ terms' = terms   \* the append did not land (not durable)
  /\ lp' = [lp EXCEPT ![a] = "done"]

LeaderClose(a) ==
  /\ lp[a] \in {"exited_seen", "failed_seen"} /\ ClaimOK(a)
  /\ IF SplitClose
       THEN IF dec[a] = "none"
              THEN /\ dec' = [dec EXCEPT ![a] = IF LeaderChecksSelfClose /\ HasT(a) THEN "status" ELSE "terminal"]
                   /\ UNCHANGED <<lp, terms>>
              ELSE /\ LeaderWrite(a, dec[a]) /\ UNCHANGED dec
       ELSE /\ LeaderWrite(a, IF LeaderChecksSelfClose /\ HasT(a) THEN "status" ELSE "terminal")
            /\ UNCHANGED dec
  /\ UNCHANGED <<eng, claims>>

StopperClose(a) ==  \* the stop node / reaper: lane found open, process tree gone
  /\ Reaper /\ ClaimOK(a)
  /\ lp[a] \in IF StopperCode
                THEN {"gone", "claimed", "running", "exited_seen", "failed_seen"}
                ELSE IF StopperWaitsLeader
                THEN {"gone", "unresolved", "exited_seen", "failed_seen", "running", "refused", "done"}
                ELSE {"gone", "unresolved", "exited_seen", "failed_seen", "running", "refused", "done",
                      "claimed", "requested"}
  /\ eng[a] # "running"
  /\ ~HasT(a)
  /\ terms' = Appended(<<a, "stopper", "stopped">>)
  /\ UNCHANGED <<lp, eng, claims>>

\* ---- a retry of the same key -----------------------------------------------
\* Attempt a+1 is requested after attempt a ended or died. RetryGuard is the fence.
RetryOK(a) ==
  CASE RetryGuard = "any" -> TRUE
    [] RetryGuard = "no_open_claim" -> \A b \in 1..a : b \in claims => HasT(b)
    [] RetryGuard = "ledger_failed" ->  \* what a re-requester can read: every CLAIM closed failed
         \A b \in 1..a : b \in claims => \E t \in terms : t[1] = b /\ t[3] \in {"start-failed", "failed"}
    [] RetryGuard = "closed_unstarted_ledger" ->  \* closed_unstarted without the omniscient eng conjunct
         \A b \in 1..a : b \in claims => \E t \in terms : t[1] = b /\ t[3] = "start-failed"
    [] OTHER ->  \* "closed_unstarted": no engine ever, every CLAIM closed start-failed
         \A b \in 1..a : /\ eng[b] = "none"
                         /\ (b \in claims => \E t \in terms : t[1] = b /\ t[3] = "start-failed")

Retry(a) ==
  /\ a < MaxAttempts /\ lp[a + 1] = "none"
  /\ lp[a] \in IF RetryInflight
                THEN {"refused", "done", "gone", "unresolved", "observed",
                      "requested", "claimed", "running", "exited_seen", "failed_seen"}
                ELSE {"refused", "done", "gone", "unresolved", "observed"}
  /\ RetryOK(a)
  /\ lp' = [lp EXCEPT ![a + 1] = "requested"]
  /\ UNCHANGED <<eng, claims, terms>>

Next == \E a \in Attempts :
          \/ /\ UNCHANGED dec
             /\ \/ Refuse(a) \/ Observe(a) \/ Claim(a) \/ StartEngine(a) \/ EngineExit(a)
                \/ LateStartLands(a) \/ LeaderSeesExit(a) \/ LeaderDies(a) \/ LaneClose(a)
                \/ StopperClose(a) \/ Retry(a)
          \/ LeaderClose(a)

U(A) == A /\ UNCHANGED dec
Fairness == \A a \in Attempts :
              /\ WF_vars(U(Claim(a))) /\ WF_vars(U(StartEngine(a))) /\ WF_vars(U(EngineExit(a)))
              /\ WF_vars(U(LeaderSeesExit(a))) /\ WF_vars(LeaderClose(a)) /\ WF_vars(U(StopperClose(a)))

Spec == Init /\ [][Next]_vars /\ Fairness

\* ---- properties --------------------------------------------------------------
TypeOK ==
  /\ dec \in [Attempts -> {"none", "status", "terminal"}]
  /\ lp \in [Attempts -> {"none", "requested", "refused", "observed", "claimed", "running",
                          "failed_seen", "unresolved", "exited_seen", "gone", "done"}]
  /\ eng \in [Attempts -> {"none", "running", "exited"}]
  /\ claims \subseteq Attempts
  /\ \A t \in terms : t[1] \in Attempts /\ t[2] \in Writers

StartedAtMostOnce == Cardinality(Started) <= 1

ClaimBeforeEngine == \A a \in Attempts : eng[a] # "none" => a \in claims

NoTerminalWithoutClaim == \A t \in terms : t[1] \in claims

AtMostOneTerminal == \A a \in Attempts : Cardinality({t \in terms : t[1] = a}) <= 1

StartedTerminalPerKey == Cardinality({t \in terms : eng[t[1]] # "none"}) <= 1

LeaderTerminalAfterExit ==
  \A t \in terms : t[2] \in {"leader", "stopper"} => eng[t[1]] # "running"

NoRetryAfterUnresolved == lp[1] = "unresolved" => lp[2] = "none"

ObserveWritesNothing == Mode = "observe" => (claims = {} /\ Started = {} /\ terms = {})

StartedReachesTerminal == \A a \in Attempts : [](eng[a] # "none" => <>HasT(a))

\* ---- witnesses: each invariant is violated, which shows the state is reachable
NeverStarted == Started = {}
NeverClosedByLane == \A t \in terms : t[2] # "lane"
NeverClosedByLeader == \A t \in terms : t[2] # "leader"
NeverClosedByStopper == \A t \in terms : t[2] # "stopper"
NeverStartFailed == \A t \in terms : t[3] # "start-failed"
NeverRetried == lp[2] = "none"
NeverRetriedAfterFailedStart == ~(lp[2] # "none" /\ \E t \in terms : t[1] = 1 /\ t[3] = "start-failed")
NeverUnresolvedRan == ~(lp[1] = "unresolved" /\ eng[1] # "none")
NeverObserved == \A a \in Attempts : lp[a] # "observed"

\* ---- claim read per CLAIM, no-start-after-close, more witnesses --------------------------------------------------------
\* every accepted CLAIM, started or not, reaches a TERMINAL (claim 2 read per CLAIM)
ClaimReachesTerminal == \A a \in Attempts : [](a \in claims => <>HasT(a))
\* no engine appears for an attempt whose CLAIM is already closed (action property)
NoStartAfterTerminal == [][\A a \in Attempts : (HasT(a) /\ eng[a] = "none") => eng'[a] = "none"]_vars
\* witnesses
NeverLeaderStatus == ~(\E a \in Attempts : lp[a] = "done" /\ \E t \in terms : t[1] = a /\ t[2] = "lane")
NeverRetryAfterRefused == ~(lp[1] = "refused" /\ lp[2] # "none")
NeverThirdStarted == eng[MaxAttempts] = "none"
NeverStopperAfterDeadLeaderRan == ~(\E a \in Attempts : lp[a] = "gone" /\ eng[a] = "exited"
                                     /\ \E t \in terms : t[1] = a /\ t[2] = "stopper")
NeverRetryAfterUnresolved == ~(lp[1] = "unresolved" /\ lp[2] # "none")
================================================================================
