# AndonStop: TLA+ model of the andon stop record and the lane lease (OMN-21442, plan task T-A1)

Model-before-build for the stop reducer (node_andon_stop_reducer) and the lane lease reducer
(node_lane_lease_reducer). Both are built after this model and cite this directory.

## What is modelled

One stop for one key, from its opening to a terminal state, and the leases of its swarm lane.

Stop machine (states and triggers are the tables StopStates, StopTriggers, StopTransitions):
open, swarming, fixed, stale, cleared, declared_false. The reopen from fixed on a mismatched
readback is modelled. Lease machine (LeaseStates, LeaseTriggers, LeaseTransitions): claimed,
spawning, uncertain, active, expired, replaced, released.

Actors: detectors open the stop; the dispatcher grants a lease (a new generation) and replaces an
expired one; a lane starts, receives its start receipt or fails, may die while spawning, finishes
or lets its lease expire; the readback reads the stop's scope; an operator extends the replacement
budget, declares the stop false, or records a hand fix; the gate answers dispatch requests; a
detector or dispatcher restarts; a contract migration happens mid-stop; an hour passes.

The stop's guard context is computed as the reducer's context builder will compute it:
lease_replacements_remaining is the budget (MaxLeaseReplacements plus the operator's extensions)
minus replacements made; fixed_readings_missing is ConsecutiveFixedReadings minus the complete
readings counted; hand_fix_allowed is a first occurrence with durable repair evidence.

Ground truth the model keeps beside what the handler counts, so a wrong guard is visible:
goodPost (readings that were PASS, proof eligible and crossed after countermeasure_at),
replTotal (replacements ever made), the lease of every generation, and the set of live processes.

## Abstractions and bounds

- Time is the hour counter age (saturating at StaleAfterHours); a lease's 90 minutes and a pending
  window are not clocks: an expiry or window elapsing is an action, and fairness says it happens.
- One stop record per run. Its occurrence number is chosen at open from Occurrences ({1, 2}); a
  recurrence is a new record and a new model run, so the model covers the first-occurrence guard
  of the hand fix without replaying a second record.
- Decision rows saturate at one: the properties read "a decision row exists".
- MaxCountermeasures = 1: a countermeasure may fail once (the cause is not fixed and the readback
  returns MISSING), and every later one fixes it. This is a stated assumption of the liveness
  checks and not a property of the stop: see F3. The safety properties are checked with the same
  bound, and mutant M34 lifts it.
- MaxGen bounds lease generations. The design bound leaves a replacement impossible only past the
  deepest reachable chain; design_live uses MaxGen = 8, Occurrences = {1} and Modes = {enforce}.
- Two pure checks are not machine state: OneStopPerKey (the open rule for any set of detectors) and
  StormIsOneStop (the storm rule for any count of keys in one tick).
- The gate is a function of the stop's state as the dispatcher last read it (cache), the class
  mode, and the request. Requests: new dispatch, the swarm lane (with a generation), a lane that
  fixes an open PR (and one that names a PR the watcher state does not show open: openpr_forged),
  a read-only lane, the merge of an armed PR, a promotion, a canary. Whether a named PR is open
  is the request's fact, read from the watcher state by the caller; the model checks that a gate
  with a dispatcher-bound exemption refuses the forged one, and does not model the watcher.

## Fairness assumption (stated, used by the liveness checks only)

Time passes: an hour ticks, an active lease expires, a claimed lease's or an uncertain lease's
pending window elapses, a launch times out. The stale-limit check and the replacement of an expired
lease run when enabled. Readbacks are fair: while a stop sits in fixed, a canary crossing is
dispatched and a PASS reading of a fixed cause is taken. Nothing is assumed about which reading a
not-yet-fixed cause returns, how often an operator acts, or whether a process of an expired lease
dies. Under this assumption, together with ReleaseGuard (F1) and finitely many failing
countermeasures (F3), every open stop eventually clears, is declared false, or reaches a decision
row (StopResolves).

## Findings of the model

F1. The stop's contract has no exit from swarming for a released lease: only lease_expired leaves
swarming. A swarm lane that ends without a merged countermeasure and releases its lease leaves the
stop in swarming with no live lane, no decision row and no way out (counterexample of
design_live before ReleaseGuard, kept as mutant M32). The model closes it with an assumption on the
lane, ReleaseGuard: a swarm lane that ends before its stop leaves swarming lets its lease expire
and does not release it. The swarm lane's deployment task (T-A7) must implement that rule, or the
stop contract must add a transition from swarming for stale_limit_reached or lane_finished; either
way the model must then be rerun. The passing results hold for the protocol with this extra rule,
and section 0d.7's lane_finished -> released transition is unconditional, so the rule has to be
carried into the canonical contract or the swarm lane's trigger requirements.
F3. The stop's contract lets a failed countermeasure reopen the stop (fixed -> open on a
mismatched readback) with no bound on the cycles: lease grants from a released lease spend no
replacement and nothing counts reopenings, so countermeasures that keep failing give an execution
that satisfies the fairness assumption (each cycle resets the hour counter) and never clears and
never reaches a decision row (mutant M34). StopResolves therefore holds under the assumption that
only finitely many countermeasures fail (MaxCountermeasures). The model does not add a bound;
the choice is the plan's owner's: a reopen count in the guard context that sends the stop to
stale after a configured number of reopenings, or an explicit amendment of the liveness claim.
F2. The contracts of both machines do not exist in the repository yet (the stop reducer's is
built by T-A2 and the lease reducer's by T-S9). tests/formal/fixtures carries the state_machine
and config fragments the plan declares, and the contract-comparison tests read the node's
contract.yaml as soon as it exists, so the later task inherits the comparison.

## Properties, the rule each checks, the mutant that breaks it

All safety properties are state or action invariants of the design config; the liveness properties
are checked by design_live. A mutant config differs from its base in exactly one knob.

P  property                                 rule                                           mutant
-  OneStopPerKey                            two detectors, one key, one stop (5.4)         M17
-  StormIsOneStop                           more than StormDistinctKeys keys: one stop     M18
-  ReplacementsBounded                      replacements at most the budget; budget grows  M1 M2 M3
                                            only by the operator's replacements_extended
-  NeverClearsWithoutReadings               a stop never clears without the consecutive    M4 M5
                                            complete readings, a hand fix included
-  UnknownNeverClears                       a reading that reads UNKNOWN holds the stop    M6
-  OnlyPostCountermeasureCounts             only crossings after countermeasure_at count   M7
-  FixedHasAnchor                           a fixed stop has countermeasure_at             M10
-  HandFixOnlyFirstWithEvidence             hand_fix_allowed: first occurrence + evidence  M8 M9
-  StaleNeverClears (action)                a stale stop never clears itself               M11
-  StaleEmitsDecisionRow                    stale writes one decision row                  M12
-  NoHumanClear                             no human clears a stop                         M13
-  DeclaredFalseIsRecorded                  declared false is recorded                     M14
-  MigrationNeverEntersTerminal             a contract migration never enters a terminal   M15
-  GateSeesOpenStop                         a restart re-reads every open stop             M16
-  StopRefusesNewDispatchOnly               open-PR fixes, read-only lanes, armed merges   M19
                                            and promotion are never refused
-  SwarmLaneNeverRefused                    the swarm lane is never refused by its stop    M20
-  OnlyBoundExemptionsAdmitted              forged, unrelated and expired exemptions are   M21
                                            refused, a forged open-PR claim included
                                            (the dispatcher binds the id)
-  NewDispatchRefusedWhileOpen              new dispatch is refused while the stop is      M22
                                            unresolved, a stale stop included
-  ObserveNeverRefuses                      a class in observe mode never refuses          M23
-  CanaryAdmittedWhileOpen                  the gate admits one canary per readback        M24
                                            interval so a class with no traffic recovers
-  AtMostOneActiveLease                     at most one active lease per key               M25
-  ActiveRequiresStartReceipt               active only on a start receipt or a found      M26
                                            process
-  StaleGenerationWriteRefused              a write carrying a generation below the        M27
                                            current one is refused
-  WriteOnlyUnderActiveLease                a write is accepted only under an active       M28
                                            lease; expiry revokes it, no replacement needed
-  StopResolves (liveness)                  every open stop clears or reaches a decision   M29 M32 M34
                                            row, including a hand-fixed stop
-  PendingLeaseResolves (liveness)          a crash leaves the lease recoverable           M30
-  ExpiredReplacementNeverWaits (liveness)  a replacement never waits on a live process    M31
-  UnreadableFixedReachesStale (liveness)   a fixed stop unreadable for the stale limit    M33
                                            with no live lane reaches stale

Witnesses (reachability, a violation is expected): W1 a hand-fixed stop reaches cleared, through
the consecutive readings; W2 a stop cleared by a countermeasure is reachable.

## Out of scope

The queue's persistence and ordering, the event chain's hashing, the class registry, WIP limits,
and the replay of the 2026-10-26 events belong to later tasks. The storm and one-stop-per-key
rules are modelled as the open rule, not as a stream of detector events. The lease's 90 minutes
are not a clock (above).

## Rerun

tests/formal/andon_stop_tlc.sh (the whole matrix; SUMMARY.txt and one .out per config) and then
tests/formal/test_andon_stop_model.py. tla2tools.jar is pinned by sha256 in the script.
