---------------------------- MODULE AndonStop ----------------------------
(***************************************************************************)
(* Model-before-build for the andon stop record and for the lane lease     *)
(* that a swarm lane shares with a bump owner. Two machines are modelled:  *)
(*                                                                         *)
(*   - the stop: open, swarming, fixed, stale, cleared, declared_false     *)
(*   - the lease: claimed, spawning, uncertain, active, expired, replaced, *)
(*     released                                                            *)
(*                                                                         *)
(* States, triggers and transitions are the tables StopTransitions and     *)
(* LeaseTransitions below; tests/formal/test_andon_stop_model.py compares  *)
(* them with the state_machine of each machine's own contract, and the     *)
(* constants of each .cfg with the contract's config block. No action      *)
(* fires a transition that is not in its table (see Step and LStep).       *)
(*                                                                         *)
(* Design knobs (CONSTANTS in lower-case-first style below) are the one    *)
(* thing a mutant .cfg changes. README.txt maps every property to the      *)
(* rule it checks and to the mutant that breaks it.                        *)
(***************************************************************************)
EXTENDS Integers, FiniteSets

CONSTANTS
  \* contract constants (config block): compared with the contract by the test
  MaxLeaseReplacements, ConsecutiveFixedReadings, StaleAfterHours,
  StormDistinctKeys, CanaryDispatchesPerReadbackInterval, RecoveryMinimumCrossings,
  \* exploration bounds (not contract values)
  Occurrences, MaxExtensions, ExtendBy, MaxGen, MaxCountermeasures,
  Modes, Detectors,
  \* design knobs
  GrantGuard, ExpiryGuard, ReopenResetsBudget, ClearGuard, CrossingsAfter,
  UnknownAs, HandFixNeedsFirst, HandFixNeedsEvidence, HandFixAnchors,
  HandFixTarget, StaleClears, StaleDecisionRow, StaleLimit, OperatorClear,
  DeclareRecorded, MigrationMap, RestartMode, DedupBy, StormCollapse,
  Exempt, ExemptionBy, GatedStates, ObserveMode, GenCheck, RevokeOn,
  ClaimGuard, ReceiptGuard, PendingWindow, ReplacementWaits, ReleaseGuard, StaleFromFixed, FailForever

StopStates == {"open", "swarming", "fixed", "stale", "cleared", "declared_false"}
Terminal == {"cleared", "declared_false"}
Unresolved == {"open", "swarming", "fixed", "stale"}
StopTriggers == {"lease_granted", "lease_expired", "stale_limit_reached",
                 "countermeasure_merged", "hand_fix_recorded", "replacements_extended",
                 "readback_fixed", "readback_unknown", "readback_mismatch",
                 "declared_false"}

\* <<from, trigger, to>> of the stop's state_machine
StopTransitions == {
  <<"open", "lease_granted", "swarming">>,
  <<"stale", "lease_granted", "swarming">>,
  <<"swarming", "lease_expired", "swarming">>,
  <<"swarming", "lease_expired", "stale">>,
  <<"open", "stale_limit_reached", "stale">>,
  <<"open", "countermeasure_merged", "fixed">>,
  <<"swarming", "countermeasure_merged", "fixed">>,
  <<"stale", "countermeasure_merged", "fixed">>,
  <<"open", "hand_fix_recorded", "fixed">>,
  <<"swarming", "hand_fix_recorded", "fixed">>,
  <<"stale", "hand_fix_recorded", "fixed">>,
  <<"stale", "replacements_extended", "stale">>,
  <<"fixed", "readback_fixed", "cleared">>,
  <<"fixed", "readback_fixed", "fixed">>,
  <<"fixed", "readback_unknown", "fixed">>,
  <<"fixed", "readback_mismatch", "open">>,
  <<"fixed", "stale_limit_reached", "stale">>,
  <<"open", "declared_false", "declared_false">>,
  <<"swarming", "declared_false", "declared_false">>,
  <<"fixed", "declared_false", "declared_false">>,
  <<"stale", "declared_false", "declared_false">> }

LeaseStates == {"claimed", "spawning", "uncertain", "active", "expired", "replaced", "released"}
LeaseTerminal == {"replaced", "released"}
LeaseTriggers == {"spawn_started", "pending_window_elapsed", "receipt_recorded",
                  "launch_failed", "launch_timed_out", "starter_died", "process_found",
                  "lease_expired", "lane_finished", "next_generation_started"}

\* <<from, trigger, to>> of the lane lease's state_machine
LeaseTransitions == {
  <<"claimed", "spawn_started", "spawning">>,
  <<"claimed", "pending_window_elapsed", "expired">>,
  <<"spawning", "receipt_recorded", "active">>,
  <<"spawning", "launch_failed", "expired">>,
  <<"spawning", "launch_timed_out", "uncertain">>,
  <<"spawning", "starter_died", "uncertain">>,
  <<"uncertain", "process_found", "active">>,
  <<"uncertain", "pending_window_elapsed", "expired">>,
  <<"active", "lease_expired", "expired">>,
  <<"active", "lane_finished", "released">>,
  <<"expired", "next_generation_started", "replaced">> }

Gens == 1..MaxGen
Live == {"claimed", "spawning", "uncertain", "active"}   \* an unexpired lease
ReqKinds == {"new", "swarm", "openpr", "readonly", "armedmerge", "promotion", "canary", "openpr_forged"}

VARIABLES
  st, cache, cons, goodPost, age, cm, occ, evid, used, replTotal, opExt,
  decisions, cross, canaryUsed, causeFixed, cmCount,
  lgen, ls, alive, boundGen,
  mode,
  histCounted, unkClears, handFixBad, handFixUsed, humanCleared, migTerm, badActive

stopVars == <<st, cache, cons, goodPost, age, cm, occ, evid, used, replTotal, opExt,
              decisions, cross, canaryUsed, causeFixed, cmCount>>
leaseVars == <<lgen, ls, alive, boundGen>>
otherVars == <<mode>>
flagVars == <<histCounted, unkClears, handFixBad, handFixUsed, humanCleared, migTerm, badActive>>
vars == <<stopVars, leaseVars, otherVars, flagVars>>

Min(a, b) == IF a < b THEN a ELSE b
Budget == MaxLeaseReplacements + opExt * ExtendBy
Remaining == Budget - used                       \* lease_replacements_remaining

\* a transition is fired only when it is in the table of its machine
Step(from, trig, to) == <<from, trig, to>> \in StopTransitions
LStep(from, trig, to) == <<from, trig, to>> \in LeaseTransitions

Init ==
  /\ st = "absent" /\ cache = "absent"
  /\ cons = 0 /\ goodPost = 0 /\ age = 0 /\ cm = FALSE /\ occ = 0 /\ evid = FALSE
  /\ used = 0 /\ replTotal = 0 /\ opExt = 0 /\ decisions = 0
  /\ cross = 0 /\ canaryUsed = 0 /\ causeFixed = FALSE /\ cmCount = 0
  /\ lgen = 0 /\ ls = [g \in Gens |-> "none"] /\ alive = {} /\ boundGen = 0
  /\ mode \in Modes
  /\ histCounted = FALSE /\ unkClears = FALSE /\ handFixBad = FALSE
  /\ handFixUsed = FALSE /\ humanCleared = FALSE /\ migTerm = FALSE /\ badActive = FALSE

TypeOK ==
  /\ st \in StopStates \cup {"absent"} /\ cache \in StopStates \cup {"absent"}
  /\ cons \in 0..(ConsecutiveFixedReadings + 1) /\ goodPost \in 0..ConsecutiveFixedReadings
  /\ age \in 0..StaleAfterHours /\ cm \in BOOLEAN /\ occ \in 0..2
  /\ decisions \in 0..1 /\ cross \in 0..RecoveryMinimumCrossings
  /\ canaryUsed \in 0..CanaryDispatchesPerReadbackInterval + 1
  /\ lgen \in 0..MaxGen /\ ls \in [Gens -> LeaseStates \cup {"none"}] /\ alive \subseteq Gens
  /\ mode \in Modes

(* ------------------------------------------------------------------ *)
(* Lease claim of the next generation: the dispatcher binds the new    *)
(* generation's id to the stop. An expired lease that is replaced      *)
(* spends one replacement (next_generation_started).                   *)
(* ------------------------------------------------------------------ *)
PrevExpired == lgen >= 1 /\ ls[lgen] = "expired"
CanClaim == lgen < MaxGen /\ (ClaimGuard => \A g \in Gens : ls[g] \notin Live)
ClaimNext ==
  /\ CanClaim
  /\ (PrevExpired => LStep("expired", "next_generation_started", "replaced"))
  /\ lgen' = lgen + 1
  /\ boundGen' = lgen + 1
  /\ ls' = IF lgen = 0 THEN [ls EXCEPT ![1] = "claimed"]
           ELSE IF PrevExpired THEN [ls EXCEPT ![lgen] = "replaced", ![lgen + 1] = "claimed"]
           ELSE [ls EXCEPT ![lgen + 1] = "claimed"]
  /\ used' = IF PrevExpired THEN used + 1 ELSE used
  /\ replTotal' = IF PrevExpired THEN replTotal + 1 ELSE replTotal
  /\ UNCHANGED alive

(* ------------------------------------------------------------------ *)
(* Detector opens a stop (a stop is one record per key; a second       *)
(* detector for an unresolved key changes nothing: see OneStopPerKey)  *)
(* ------------------------------------------------------------------ *)
DetectorFires ==
  /\ st = "absent"
  /\ st' = "open" /\ cache' = "open" /\ occ' \in Occurrences
  /\ cons' = 0 /\ goodPost' = 0 /\ age' = 0 /\ cm' = FALSE /\ evid' = FALSE
  /\ used' = 0 /\ replTotal' = 0 /\ opExt' = 0 /\ decisions' = 0
  /\ cross' = 0 /\ canaryUsed' = 0 /\ causeFixed' = FALSE /\ cmCount' = 0
  /\ UNCHANGED <<leaseVars, otherVars, flagVars>>

Tick ==                                  \* an hour passes with the stop unresolved
  /\ st \in Unresolved /\ age < StaleAfterHours
  /\ age' = age + 1
  /\ UNCHANGED <<st, cache, cons, goodPost, cm, occ, evid, used, replTotal, opExt,
                 decisions, cross, canaryUsed, causeFixed, cmCount,
                 leaseVars, otherVars, flagVars>>

RecordEvidence ==                        \* durable repair evidence on the failure record
  /\ st \in Unresolved /\ ~evid
  /\ evid' = TRUE
  /\ UNCHANGED <<st, cache, cons, goodPost, age, cm, occ, used, replTotal, opExt,
                 decisions, cross, canaryUsed, causeFixed, cmCount,
                 leaseVars, otherVars, flagVars>>

(* ---------------- stop transitions ---------------- *)
LeaseGranted ==                          \* open|stale -> swarming
  /\ st \in {"open", "stale"} /\ Step(st, "lease_granted", "swarming")
  /\ (GrantGuard => Remaining # 0)
  /\ ClaimNext
  /\ st' = "swarming" /\ cache' = "swarming" /\ age' = 0
  /\ UNCHANGED <<cons, goodPost, cm, occ, evid, opExt, decisions, cross, canaryUsed,
                 causeFixed, cmCount, otherVars, flagVars>>

LeaseExpiredTrigger ==                   \* swarming: replace once per expiry, then a decision row
  /\ st = "swarming" /\ lgen >= 1 /\ ls[lgen] = "expired"
  /\ (ReplacementWaits => lgen \notin alive)
  /\ \/ /\ (ExpiryGuard => Remaining # 0)
        /\ Step("swarming", "lease_expired", "swarming")
        /\ ClaimNext
        /\ age' = 0
        /\ UNCHANGED <<st, cache, cons, goodPost, cm, occ, evid, opExt, decisions, cross,
                       canaryUsed, causeFixed, cmCount, otherVars, flagVars>>
     \/ /\ ExpiryGuard /\ Remaining = 0
        /\ Step("swarming", "lease_expired", "stale")
        /\ st' = "stale" /\ cache' = "stale"
        /\ decisions' = IF StaleDecisionRow THEN 1 ELSE decisions
        /\ UNCHANGED <<cons, goodPost, age, cm, occ, evid, used, replTotal, opExt, cross,
                       canaryUsed, causeFixed, cmCount, leaseVars, otherVars, flagVars>>

StaleLimitReached ==                     \* open|fixed with no live swarm lane, past the stale limit
  /\ StaleLimit /\ (st = "open" \/ (StaleFromFixed /\ st = "fixed")) /\ age = StaleAfterHours
  /\ \A g \in Gens : ls[g] \notin Live
  /\ Step(st, "stale_limit_reached", "stale")
  /\ st' = "stale" /\ cache' = "stale" /\ age' = 0
  /\ decisions' = IF StaleDecisionRow THEN 1 ELSE decisions
  /\ UNCHANGED <<cons, goodPost, cm, occ, evid, used, replTotal, opExt, cross,
                 canaryUsed, causeFixed, cmCount, leaseVars, otherVars, flagVars>>

CountermeasureMerged ==                  \* open|swarming|stale -> fixed; anchors countermeasure_at
  /\ st \in {"open", "swarming", "stale"} /\ Step(st, "countermeasure_merged", "fixed")
  /\ (FailForever \/ cmCount <= MaxCountermeasures)
  /\ st' = "fixed" /\ cache' = "fixed" /\ cm' = TRUE
  /\ cons' = 0 /\ goodPost' = 0 /\ age' = 0 /\ cmCount' = Min(cmCount + 1, MaxCountermeasures + 1)
  /\ causeFixed' \in (IF cmCount >= MaxCountermeasures /\ ~FailForever THEN {TRUE} ELSE BOOLEAN)
  /\ cross' = 0 /\ canaryUsed' = 0
  /\ UNCHANGED <<occ, evid, used, replTotal, opExt, decisions, leaseVars, otherVars, flagVars>>

HandFixRecorded ==                       \* a hand fix: first occurrence with durable repair evidence
  /\ st \in {"open", "swarming", "stale"} /\ (FailForever \/ cmCount <= MaxCountermeasures)
  /\ (HandFixNeedsFirst => occ = 1) /\ (HandFixNeedsEvidence => evid)
  /\ \/ /\ HandFixTarget = "fixed" /\ Step(st, "hand_fix_recorded", "fixed")
        /\ st' = "fixed"
     \/ /\ HandFixTarget = "cleared" /\ st' = "cleared"
  /\ cache' = st'
  /\ cm' = IF HandFixAnchors THEN TRUE ELSE cm
  /\ cons' = 0 /\ goodPost' = 0 /\ age' = 0 /\ cmCount' = Min(cmCount + 1, MaxCountermeasures + 1)
  /\ causeFixed' \in (IF cmCount >= MaxCountermeasures /\ ~FailForever THEN {TRUE} ELSE BOOLEAN)
  /\ cross' = 0 /\ canaryUsed' = 0
  /\ handFixBad' = (handFixBad \/ ~(occ = 1 /\ evid)) /\ handFixUsed' = TRUE
  /\ UNCHANGED <<occ, evid, used, replTotal, opExt, decisions, leaseVars, otherVars,
                 histCounted, unkClears, humanCleared, migTerm, badActive>>

ReplacementsExtended ==                  \* the operator's record; the only way the budget grows
  /\ st = "stale" /\ opExt < MaxExtensions /\ Step("stale", "replacements_extended", "stale")
  /\ opExt' = opExt + 1 /\ decisions' = 1
  /\ UNCHANGED <<st, cache, cons, goodPost, age, cm, occ, evid, used, replTotal, cross,
                 canaryUsed, causeFixed, cmCount, leaseVars, otherVars, flagVars>>

DeclaredFalse ==                         \* the operator's only other move, recorded
  /\ st \in Unresolved /\ Step(st, "declared_false", "declared_false")
  /\ st' = "declared_false" /\ cache' = "declared_false"
  /\ decisions' = IF DeclareRecorded THEN 1 ELSE decisions
  /\ UNCHANGED <<cons, goodPost, age, cm, occ, evid, used, replTotal, opExt, cross,
                 canaryUsed, causeFixed, cmCount, leaseVars, otherVars, flagVars>>

(* ---------------- the readback ---------------- *)
\* One reading counted toward clearing. good: PASS, proof eligible, covers the scope and
\* crossed after countermeasure_at. hist: it did not cross after countermeasure_at.
\* unk: it was an unreadable reading. Only design-conformant callers pass good=TRUE.
CountReading(good, hist, unk) ==
  LET c1 == cons + 1
      clear == IF ClearGuard = "enough" THEN c1 >= ConsecutiveFixedReadings ELSE TRUE
  IN /\ \/ /\ clear /\ Step("fixed", "readback_fixed", "cleared")
           /\ st' = "cleared"
        \/ /\ ~clear /\ Step("fixed", "readback_fixed", "fixed")
           /\ st' = "fixed"
     /\ cache' = st'
     /\ cons' = Min(c1, ConsecutiveFixedReadings + 1)
     /\ goodPost' = IF good THEN Min(goodPost + 1, ConsecutiveFixedReadings) ELSE goodPost
     /\ age' = 0 /\ cross' = 0 /\ canaryUsed' = 0
     /\ histCounted' = (histCounted \/ hist)
     /\ unkClears' = (unkClears \/ (unk /\ clear))
     /\ UNCHANGED <<cm, occ, evid, used, replTotal, opExt, decisions, causeFixed, cmCount,
                    leaseVars, otherVars, handFixBad, handFixUsed, humanCleared, migTerm,
                    badActive>>

ReadbackFixed ==                         \* a PASS, proof-eligible reading of the stopped scope
  /\ st = "fixed" /\ causeFixed /\ cm /\ cross >= RecoveryMinimumCrossings
  /\ CountReading(TRUE, FALSE, FALSE)

ReadbackHistory ==                       \* a reading from before countermeasure_at
  /\ st = "fixed"
  /\ \/ /\ CrossingsAfter = "any" /\ CountReading(FALSE, TRUE, FALSE)
     \/ /\ CrossingsAfter = "countermeasure"
        /\ Step("fixed", "readback_unknown", "fixed")
        /\ canaryUsed' = 0
        /\ UNCHANGED <<st, cache, cons, goodPost, age, cm, occ, evid, used, replTotal, opExt,
                       decisions, cross, causeFixed, cmCount, leaseVars, otherVars, flagVars>>

ReadbackUnknown ==                       \* UNKNOWN holds the stop; it never clears it
  /\ st = "fixed"
  /\ \/ /\ UnknownAs = "hold" /\ Step("fixed", "readback_unknown", "fixed")
        /\ canaryUsed' = 0
        /\ UNCHANGED <<st, cache, cons, goodPost, age, cm, occ, evid, used, replTotal, opExt,
                       decisions, cross, causeFixed, cmCount, leaseVars, otherVars, flagVars>>
     \/ /\ UnknownAs = "fixed" /\ CountReading(FALSE, FALSE, TRUE)

ReadbackMismatch ==                      \* MISSING for the stopped scope reopens the stop
  /\ st = "fixed" /\ ~causeFixed /\ Step("fixed", "readback_mismatch", "open")
  /\ st' = "open" /\ cache' = "open" /\ cm' = FALSE
  /\ cons' = 0 /\ goodPost' = 0 /\ age' = 0 /\ cross' = 0 /\ canaryUsed' = 0
  /\ used' = IF ReopenResetsBudget THEN 0 ELSE used
  /\ UNCHANGED <<occ, evid, replTotal, opExt, decisions, causeFixed, cmCount,
                 leaseVars, otherVars, flagVars>>

(* ---------------- moves only a broken design allows ---------------- *)
OperatorClearsStop ==                    \* a human clearing a stop (mutant only)
  /\ OperatorClear /\ st \in Unresolved
  /\ st' = "cleared" /\ cache' = "cleared" /\ humanCleared' = TRUE
  /\ UNCHANGED <<cons, goodPost, age, cm, occ, evid, used, replTotal, opExt, decisions, cross,
                 canaryUsed, causeFixed, cmCount, leaseVars, otherVars, histCounted, unkClears,
                 handFixBad, handFixUsed, migTerm, badActive>>

StaleClearsItself ==                     \* stale -> cleared on a reading (mutant only)
  /\ StaleClears /\ st = "stale" /\ cross >= RecoveryMinimumCrossings
  /\ st' = "cleared" /\ cache' = "cleared"
  /\ UNCHANGED <<cons, goodPost, age, cm, occ, evid, used, replTotal, opExt, decisions, cross,
                 canaryUsed, causeFixed, cmCount, leaseVars, otherVars, flagVars>>

(* ---------------- a contract migration and a restart ---------------- *)
ContractMigration ==                     \* the contract changes during a stop: never a terminal state
  /\ st \in Unresolved
  /\ st' = IF MigrationMap = "identity" THEN st ELSE "cleared"
  /\ cache' = st'
  /\ migTerm' = (migTerm \/ MigrationMap # "identity")
  /\ UNCHANGED <<cons, goodPost, age, cm, occ, evid, used, replTotal, opExt, decisions, cross,
                 canaryUsed, causeFixed, cmCount, leaseVars, otherVars, histCounted, unkClears,
                 handFixBad, handFixUsed, humanCleared, badActive>>

Restart ==                               \* a detector or dispatcher restarts and re-reads the queue
  /\ cache' = IF RestartMode = "reread" THEN st ELSE "absent"
  /\ UNCHANGED <<st, cons, goodPost, age, cm, occ, evid, used, replTotal, opExt, decisions, cross,
                 canaryUsed, causeFixed, cmCount, leaseVars, otherVars, flagVars>>

(* ---------------- the gate (5.4) and proof crossings (5.5) ---------------- *)
Gated == cache \in GatedStates
\* A swarm lane's exemption is valid only when the dispatcher bound its generation to the stop
\* and its lease is unexpired and current.
Valid(g) == g >= 1 /\ g = boundGen /\ g = lgen /\ ls[g] \in Live
Decide(kind, g) ==
  IF mode = "observe" /\ ObserveMode = "admit" THEN "admit"
  ELSE IF ~Gated THEN "admit"
  ELSE IF kind = "new" THEN "refuse"
  ELSE IF kind = "swarm" THEN
         IF "swarm" \notin Exempt THEN "refuse"
         ELSE IF ExemptionBy = "brief" THEN "admit"
         ELSE IF Valid(g) THEN "admit" ELSE "refuse"
  ELSE IF kind = "canary" THEN
         IF "canary" \in Exempt /\ canaryUsed < CanaryDispatchesPerReadbackInterval
         THEN "admit" ELSE "refuse"
  ELSE IF kind = "openpr_forged" THEN   \* names a PR the watcher state does not show open
         IF ExemptionBy = "brief" /\ "openpr" \in Exempt THEN "admit" ELSE "refuse"
  ELSE IF kind \in Exempt THEN "admit" ELSE "refuse"

CanaryDispatch ==                        \* one proof crossing per readback interval
  /\ st \in Unresolved /\ Decide("canary", 0) = "admit"
  /\ canaryUsed < CanaryDispatchesPerReadbackInterval
  /\ canaryUsed' = canaryUsed + 1
  /\ cross' = IF cm THEN Min(cross + 1, RecoveryMinimumCrossings) ELSE cross
  /\ UNCHANGED <<st, cache, cons, goodPost, age, cm, occ, evid, used, replTotal, opExt,
                 decisions, causeFixed, cmCount, leaseVars, otherVars, flagVars>>

SwarmDispatch ==                         \* the swarm lane's own dispatch is a crossing too
  /\ st \in Unresolved /\ lgen >= 1 /\ ls[lgen] = "active" /\ Decide("swarm", lgen) = "admit"
  /\ cross' = IF cm THEN Min(cross + 1, RecoveryMinimumCrossings) ELSE cross
  /\ UNCHANGED <<st, cache, cons, goodPost, age, cm, occ, evid, used, replTotal, opExt,
                 decisions, canaryUsed, causeFixed, cmCount, leaseVars, otherVars, flagVars>>

(* ---------------- the lane lease (0d.7) ---------------- *)
LeaseMove(g, trig, to, newAlive, bad) ==
  /\ ls' = [ls EXCEPT ![g] = to] /\ alive' = newAlive /\ badActive' = (badActive \/ bad)
  /\ UNCHANGED <<stopVars, lgen, boundGen, otherVars, histCounted, unkClears, handFixBad,
                 handFixUsed, humanCleared, migTerm>>

SpawnStarted ==
  /\ lgen >= 1 /\ ls[lgen] = "claimed" /\ LStep("claimed", "spawn_started", "spawning")
  /\ LeaseMove(lgen, "spawn_started", "spawning", alive \cup {lgen}, FALSE)

ClaimedWindowElapsed ==
  /\ lgen >= 1 /\ ls[lgen] = "claimed" /\ LStep("claimed", "pending_window_elapsed", "expired")
  /\ LeaseMove(lgen, "pending_window_elapsed", "expired", alive, FALSE)

ReceiptRecorded ==                       \* receipt_started == True is the guard
  /\ lgen >= 1 /\ ls[lgen] = "spawning" /\ LStep("spawning", "receipt_recorded", "active")
  /\ \E started \in BOOLEAN :
        /\ (ReceiptGuard => started)
        /\ LeaseMove(lgen, "receipt_recorded", "active", alive, ~started)

LaunchFailed ==                          \* a definitive launch failure: no process exists
  /\ lgen >= 1 /\ ls[lgen] = "spawning" /\ LStep("spawning", "launch_failed", "expired")
  /\ LeaseMove(lgen, "launch_failed", "expired", alive \ {lgen}, FALSE)

LaunchTimedOut ==                        \* whether a process exists is not known
  /\ lgen >= 1 /\ ls[lgen] = "spawning" /\ LStep("spawning", "launch_timed_out", "uncertain")
  /\ \E a \in {alive, alive \ {lgen}} : LeaseMove(lgen, "launch_timed_out", "uncertain", a, FALSE)

StarterDied ==                           \* the starter died while spawning: a process may exist
  /\ lgen >= 1 /\ ls[lgen] = "spawning" /\ LStep("spawning", "starter_died", "uncertain")
  /\ \E a \in {alive, alive \ {lgen}} : LeaseMove(lgen, "starter_died", "uncertain", a, FALSE)

ProcessFound ==                          \* the pool of lane machines finds the process
  /\ lgen >= 1 /\ ls[lgen] = "uncertain" /\ lgen \in alive
  /\ LStep("uncertain", "process_found", "active")
  /\ LeaseMove(lgen, "process_found", "active", alive, FALSE)

UncertainWindowElapsed ==                \* no process found when the pending window elapses
  /\ PendingWindow /\ lgen >= 1 /\ ls[lgen] = "uncertain"
  /\ LStep("uncertain", "pending_window_elapsed", "expired")
  /\ LeaseMove(lgen, "pending_window_elapsed", "expired", alive, FALSE)

LeaseExpires ==                          \* write authority ends here, replacement or not
  /\ lgen >= 1 /\ ls[lgen] = "active" /\ LStep("active", "lease_expired", "expired")
  /\ LeaseMove(lgen, "lease_expired", "expired", alive, FALSE)

\* ReleaseGuard is an assumption on the swarm lane, not a transition of the stop: the stop's
\* contract has no way out of swarming for a released lease (only lease_expired leaves it), so
\* a swarm lane that ends before its stop leaves swarming must let its lease expire and not
\* release it. Mutant M32 drops the assumption and liveness fails.
LaneFinished ==
  /\ lgen >= 1 /\ ls[lgen] = "active" /\ LStep("active", "lane_finished", "released")
  /\ (ReleaseGuard => st # "swarming")
  /\ LeaseMove(lgen, "lane_finished", "released", alive \ {lgen}, FALSE)

(* ---------------- write authority ---------------- *)
Authority(g) == IF RevokeOn = "expiry" THEN ls[g] = "active" ELSE ls[g] \in {"active", "expired"}
\* A write by the process of generation g is accepted when it carries the current generation
\* and the lease holds authority.
Accepts(g) == IF GenCheck THEN g = lgen /\ Authority(g) ELSE lgen >= 1 /\ Authority(lgen)

Next ==
  \/ DetectorFires \/ Tick \/ RecordEvidence
  \/ LeaseGranted \/ LeaseExpiredTrigger \/ StaleLimitReached \/ CountermeasureMerged
  \/ HandFixRecorded \/ ReplacementsExtended \/ DeclaredFalse
  \/ ReadbackFixed \/ ReadbackHistory \/ ReadbackUnknown \/ ReadbackMismatch
  \/ OperatorClearsStop \/ StaleClearsItself \/ ContractMigration \/ Restart
  \/ CanaryDispatch \/ SwarmDispatch
  \/ SpawnStarted \/ ClaimedWindowElapsed \/ ReceiptRecorded \/ LaunchFailed
  \/ LaunchTimedOut \/ StarterDied \/ ProcessFound \/ UncertainWindowElapsed
  \/ LeaseExpires \/ LaneFinished

\* Fairness assumption (stated, not proved): time passes (an hour ticks, a lease expires, a
\* pending window or a launch times out), the stale-limit check and the replacement run when
\* they are enabled, and readbacks are fair: while a stop sits in fixed, a canary crossing is
\* dispatched and a PASS reading of a fixed cause is taken. Nothing is assumed about which
\* reading a not-yet-fixed cause returns or how often an operator acts. A process of an
\* expired lease is never assumed to die.
Fairness ==
  /\ WF_vars(Tick) /\ WF_vars(StaleLimitReached) /\ WF_vars(LeaseExpires)
  /\ WF_vars(LeaseExpiredTrigger) /\ WF_vars(ClaimedWindowElapsed)
  /\ WF_vars(UncertainWindowElapsed) /\ WF_vars(LaunchTimedOut)
  /\ WF_vars(CanaryDispatch) /\ WF_vars(ReadbackFixed)

Spec == Init /\ [][Next]_vars /\ Fairness

(* ====================================================================== *)
(* Properties                                                             *)
(* ====================================================================== *)

\* --- opening (5.4: one stop per key, a storm is one stop) ---------------
RecordsOpened(ds) == IF DedupBy = "key" THEN 1 ELSE Cardinality(ds)
OneStopPerKey == st \in StopStates \cup {"absent"} => \A ds \in (SUBSET Detectors) \ {{}} : RecordsOpened(ds) = 1
StormOpens(n) == IF n > StormDistinctKeys THEN (IF StormCollapse THEN 1 ELSE n) ELSE n
StormIsOneStop == st \in StopStates \cup {"absent"} => \A n \in 1..(StormDistinctKeys + 1) :
                    n > StormDistinctKeys => StormOpens(n) = 1

\* --- the lease budget (5.5 rule 3) -------------------------------------
\* Replacements ever made never exceed the budget the operator's records allow.
ReplacementsBounded == replTotal <= Budget

\* --- clearing (5.4, 5.5) ----------------------------------------------
NeverClearsWithoutReadings == st = "cleared" => goodPost >= ConsecutiveFixedReadings
UnknownNeverClears == ~unkClears
OnlyPostCountermeasureCounts == ~histCounted
FixedHasAnchor == st = "fixed" => cm
StaleNeverClears == [][~(st = "stale" /\ st' = "cleared")]_vars
NoHumanClear == ~humanCleared
MigrationNeverEntersTerminal == ~migTerm

\* --- hand fix ----------------------------------------------------------
HandFixOnlyFirstWithEvidence == ~handFixBad

\* --- decision rows -----------------------------------------------------
StaleEmitsDecisionRow == st = "stale" => decisions > 0
DeclaredFalseIsRecorded == st = "declared_false" => decisions > 0

\* --- restart -----------------------------------------------------------
GateSeesOpenStop == cache = st

\* --- the gate ----------------------------------------------------------
Enforcing == mode = "enforce" /\ st \in Unresolved
StopRefusesNewDispatchOnly ==
  \A k \in {"openpr", "readonly", "armedmerge", "promotion"} : Decide(k, 0) = "admit"
SwarmLaneNeverRefused == \A g \in Gens : Valid(g) => Decide("swarm", g) = "admit"
OnlyBoundExemptionsAdmitted ==
  Enforcing => /\ Decide("openpr_forged", 0) = "refuse"
               /\ \A g \in 0..MaxGen : ~Valid(g) => Decide("swarm", g) = "refuse"
NewDispatchRefusedWhileOpen == Enforcing => Decide("new", 0) = "refuse"
ObserveNeverRefuses ==
  mode = "observe" => \A k \in ReqKinds, g \in 0..MaxGen : Decide(k, g) = "admit"
CanaryAdmittedWhileOpen ==
  (st \in Unresolved /\ canaryUsed < CanaryDispatchesPerReadbackInterval)
    => Decide("canary", 0) = "admit"

\* --- the lease ---------------------------------------------------------
AtMostOneActiveLease == Cardinality({g \in Gens : ls[g] = "active"}) <= 1
ActiveRequiresStartReceipt == ~badActive
StaleGenerationWriteRefused == \A g \in alive : g < lgen => ~Accepts(g)
WriteOnlyUnderActiveLease == \A g \in alive : Accepts(g) => ls[g] = "active"

\* --- liveness (under Fairness) ------------------------------------------
\* Every unresolved stop eventually clears, is declared false, or reaches a decision row.
StopResolves == (st \in Unresolved) ~> (st \in Terminal \/ decisions > 0)
\* A crash at any point leaves the lease recoverable: it leaves claimed, spawning, uncertain.
PendingLeaseResolves ==
  \A g \in Gens : (ls[g] \in {"claimed", "spawning", "uncertain"})
                    ~> (ls[g] \notin {"claimed", "spawning", "uncertain"})
\* A fixed stop whose readback stays unreadable for the stale limit, with no live lane, reaches stale.
UnreadableFixedReachesStale ==
  (st = "fixed" /\ age = StaleAfterHours /\ \A g \in Gens : ls[g] \notin Live)
    ~> (st # "fixed" \/ age < StaleAfterHours)
\* A replacement never waits on a live but expired process.
ExpiredReplacementNeverWaits ==
  (st = "swarming" /\ lgen >= 1 /\ ls[lgen] = "expired")
    ~> ~(st = "swarming" /\ lgen >= 1 /\ ls[lgen] = "expired")

\* --- witnesses: reachability, expected to be violated -------------------
HandFixedNeverClears == ~(handFixUsed /\ st = "cleared")
NeverClearedByCountermeasure == ~(st = "cleared" /\ ~handFixUsed)
=============================================================================
