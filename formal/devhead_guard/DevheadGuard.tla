----------------------------- MODULE DevheadGuard -----------------------------
(***************************************************************************
 One red episode for one (repository, branch). Init consumes the head-red
 event at time zero; GREEN and ESCALATED end the episode. Merges 1..N have
 a persistent-break boundary truth, or truth="none" for a flaky initial red.
 The reducer commands one probe at a time, records immutable evidence,
 confirms a culprit, and requests an idempotent revert PR. Opening a PR
 also enqueues it: queue scheduling is abstracted to nondeterministic Land.
 A human fix can race the revert, even before bisect completes. Check is
 required between landings and a green check closes remaining PRs and
 cancels outstanding work. Evidence is retained after episode termination.

 Constants are mutation knobs; each mutant changes exactly one design
 constant. A runner may crash without returning evidence; retry is a new
 StartProbe after slot release. Once the crash budget is exhausted, the
 crash handler may instead abandon this unreproduced bisect and escalate.
 ReleaseOnCrash governs cleanup before that exit as well as before retry.
 This explicit exhaustion policy is needed to test P4 with a crash-only
 mutation: ordinary bisect exits require a free slot and Deadline always
 releases it, so the strictly literal retry-only crash design masks M4.
 No fairness is imposed on runner responses,
 human fixes, PR landings, or checks: an external participant may stall.
 Tick and deadline expiration are fair for slot-release liveness (P4b).

 Time is discrete and independent of reducer steps. Probes may complete
 immediately or span arbitrarily many ticks up to the episode bound. The
 boundary Tick and Deadline are ONE atomic reducer transition: otherwise
 a visible open state at now=Bound would already violate the requested P5
 before a separately scheduled Deadline could execute. Ordinary Tick
 cannot cross this boundary with DeadlineOn=TRUE. This is an urgent timer
 abstraction, not a weakening of P5. MaxTime bounds exploration only.
***************************************************************************)
EXTENDS Naturals, FiniteSets, TLC

CONSTANTS N, Bound, MaxTime, MaxCrashes, MaxRedeliver,
          Flaky, ConfirmRule, IdemKey, GateOn, ReleaseOnCrash, DeadlineOn

ASSUME /\ N > 0 /\ Bound > 0 /\ MaxTime >= Bound
       /\ MaxCrashes \in Nat /\ MaxRedeliver \in Nat
       /\ ConfirmRule \in {"self_and_parent", "self_only"}
       /\ {Flaky, IdemKey, GateOn, ReleaseOnCrash, DeadlineOn} \subseteq BOOLEAN

Merges == 1..N
Kinds == {"self", "parent"}
Probes == Merges \X Kinds
NoProbe == <<0, "none">>
States == {"RED_DETECTED", "BISECTING", "CULPRIT_CONFIRMED",
           "REVERT_OPEN", "GREEN", "ESCALATED"}
Terminal == {"GREEN", "ESCALATED"}
PRs == {"fix", "revert"}

VARIABLES state, truth, now, rec, culprit,
          slotHeld, runnerAlive, probe, crashes,
          prsOpened, requests, fixOpen, revertOpen,
          landed, lastLanding, checked, checkBetween

vars == <<state, truth, now, rec, culprit,
          slotHeld, runnerAlive, probe, crashes,
          prsOpened, requests, fixOpen, revertOpen,
          landed, lastLanding, checked, checkBetween>>

Open == state \notin Terminal
\* TLC rejects direct string/integer equality; keep the requested literal
\* truth="none" and classify it through its printable representation.
NoBreak == ToString(truth) = "\"none\""
ActiveProbe == slotHeld /\ runnerAlive
Evidence(m) == rec[<<m, "self">>] = "fail"
               /\ rec[<<m, "parent">>] = "pass"
CanConfirm(m) == rec[<<m, "self">>] = "fail"
                /\ (ConfirmRule = "self_only"
                    \/ rec[<<m, "parent">>] = "pass")
Unreproduced == rec[<<N, "self">>] = "pass"
Exhausted == /\ \A p \in Probes : rec[p] # "unknown"
             /\ ~\E m \in Merges : CanConfirm(m)

TypeOK ==
    /\ state \in States
    /\ IF NoBreak THEN TRUE ELSE truth \in Merges
    /\ now \in 0..MaxTime
    /\ rec \in [Probes -> {"unknown", "pass", "fail"}]
    /\ culprit \in 0..N
    /\ slotHeld \in BOOLEAN /\ runnerAlive \in BOOLEAN
    /\ (probe = NoProbe \/ probe \in Probes)
    /\ crashes \in 0..MaxCrashes
    /\ prsOpened \in [Merges -> 0..(1 + MaxRedeliver)]
    /\ requests \in 0..(1 + MaxRedeliver)
    /\ fixOpen \in BOOLEAN /\ revertOpen \in BOOLEAN
    /\ landed \subseteq PRs
    /\ lastLanding \in PRs \cup {"none"}
    /\ checked \in BOOLEAN /\ checkBetween \in BOOLEAN

Init ==
    /\ state = "RED_DETECTED"
    /\ \E t \in 0..N : truth = IF t = 0 THEN "none" ELSE t
    /\ now = 0
    /\ rec = [p \in Probes |-> "unknown"]
    /\ culprit = 0
    /\ slotHeld = FALSE /\ runnerAlive = FALSE /\ probe = NoProbe
    /\ crashes = 0
    /\ prsOpened = [m \in Merges |-> 0] /\ requests = 0
    /\ fixOpen = FALSE /\ revertOpen = FALSE
    /\ landed = {} /\ lastLanding = "none"
    /\ checked = FALSE /\ checkBetween = FALSE

BeginBisect ==
    /\ state = "RED_DETECTED"
    /\ state' = "BISECTING"
    /\ UNCHANGED <<truth, now, rec, culprit, slotHeld, runnerAlive,
                    probe, crashes, prsOpened, requests, fixOpen, revertOpen,
                    landed, lastLanding, checked, checkBetween>>

StartProbe(m, kind) ==
    /\ state = "BISECTING" /\ ~slotHeld
    /\ rec[<<m, kind>>] = "unknown"
    /\ ~Unreproduced
    /\ slotHeld' = TRUE /\ runnerAlive' = TRUE /\ probe' = <<m, kind>>
    /\ UNCHANGED <<state, truth, now, rec, culprit, crashes,
                    prsOpened, requests, fixOpen, revertOpen,
                    landed, lastLanding, checked, checkBetween>>

ProbeResults(p) ==
    LET revision == IF p[2] = "self" THEN p[1] ELSE p[1] - 1
    IN IF revision = 0 THEN {"pass"}
       ELSE IF Flaky \/ NoBreak THEN {"pass", "fail"}
       ELSE IF revision >= truth THEN {"fail"} ELSE {"pass"}

ProbeReturn(result) ==
    /\ state = "BISECTING" /\ ActiveProbe
    /\ result \in ProbeResults(probe)
    /\ rec' = [rec EXCEPT ![probe] = result]
    /\ slotHeld' = FALSE /\ runnerAlive' = FALSE /\ probe' = NoProbe
    /\ UNCHANGED <<state, truth, now, culprit, crashes,
                    prsOpened, requests, fixOpen, revertOpen,
                    landed, lastLanding, checked, checkBetween>>

ProbeCrash(abandon) ==
    /\ state = "BISECTING" /\ ActiveProbe /\ crashes < MaxCrashes
    /\ abandon => crashes + 1 = MaxCrashes
    /\ state' = IF abandon THEN "ESCALATED" ELSE state
    /\ crashes' = crashes + 1
    /\ runnerAlive' = FALSE /\ probe' = NoProbe
    /\ slotHeld' = IF ReleaseOnCrash THEN FALSE ELSE slotHeld
    /\ UNCHANGED <<truth, now, rec, culprit,
                    prsOpened, requests, fixOpen, revertOpen,
                    landed, lastLanding, checked, checkBetween>>

Confirm(m) ==
    /\ state = "BISECTING" /\ ~slotHeld /\ ~Unreproduced
    /\ CanConfirm(m)
    /\ state' = "CULPRIT_CONFIRMED" /\ culprit' = m
    /\ UNCHANGED <<truth, now, rec, slotHeld, runnerAlive, probe, crashes,
                    prsOpened, requests, fixOpen, revertOpen,
                    landed, lastLanding, checked, checkBetween>>

EscalateBisect ==
    /\ state = "BISECTING" /\ ~slotHeld
    /\ Unreproduced \/ Exhausted
    /\ state' = "ESCALATED"
    /\ UNCHANGED <<truth, now, rec, culprit, slotHeld, runnerAlive,
                    probe, crashes, prsOpened, requests, fixOpen, revertOpen,
                    landed, lastLanding, checked, checkBetween>>

RequestRevert(m) ==
    /\ state \in {"CULPRIT_CONFIRMED", "REVERT_OPEN"}
    /\ m = culprit /\ requests < 1 + MaxRedeliver
    /\ prsOpened' = [prsOpened EXCEPT ![m] = IF IdemKey THEN 1 ELSE @ + 1]
    /\ requests' = requests + 1
    /\ revertOpen' = ("revert" \notin landed)
    /\ state' = "REVERT_OPEN"
    /\ UNCHANGED <<truth, now, rec, culprit, slotHeld, runnerAlive,
                    probe, crashes, fixOpen, landed, lastLanding,
                    checked, checkBetween>>

OpenFix ==
    /\ Open /\ ~fixOpen /\ "fix" \notin landed
    /\ fixOpen' = TRUE
    /\ UNCHANGED <<state, truth, now, rec, culprit, slotHeld, runnerAlive,
                    probe, crashes, prsOpened, requests, revertOpen,
                    landed, lastLanding, checked, checkBetween>>

Land(pr) ==
    /\ Open /\ pr \notin landed
    /\ IF pr = "fix" THEN fixOpen ELSE revertOpen
    /\ ~GateOn \/ landed = {} \/ checked
    /\ landed' = landed \cup {pr}
    /\ lastLanding' = pr
    /\ checkBetween' = IF Cardinality(landed) = 1 THEN checked ELSE checkBetween
    /\ checked' = FALSE
    /\ fixOpen' = IF pr = "fix" THEN FALSE ELSE fixOpen
    /\ revertOpen' = IF pr = "revert" THEN FALSE ELSE revertOpen
    /\ UNCHANGED <<state, truth, now, rec, culprit, slotHeld, runnerAlive,
                    probe, crashes, prsOpened, requests>>

CheckResults ==
    IF lastLanding = "fix" \/ NoBreak THEN {"green", "red"}
    ELSE IF culprit = truth THEN {"green"} ELSE {"red"}

Check(result) ==
    /\ Open /\ landed # {} /\ ~checked
    /\ result \in CheckResults
    /\ checked' = TRUE
    /\ state' = IF result = "green" THEN "GREEN" ELSE state
    /\ fixOpen' = IF result = "green" THEN FALSE ELSE fixOpen
    /\ revertOpen' = IF result = "green" THEN FALSE ELSE revertOpen
    \* Green cancels an in-flight bisect before exiting the episode.
    /\ slotHeld' = IF result = "green" THEN FALSE ELSE slotHeld
    /\ runnerAlive' = IF result = "green" THEN FALSE ELSE runnerAlive
    /\ probe' = IF result = "green" THEN NoProbe ELSE probe
    /\ UNCHANGED <<truth, now, rec, culprit, crashes, prsOpened, requests,
                    landed, lastLanding, checkBetween>>

DeadlineDue == DeadlineOn /\ Open /\ now >= Bound
Deadline ==
    /\ DeadlineDue
    /\ state' = "ESCALATED"
    /\ slotHeld' = FALSE /\ runnerAlive' = FALSE /\ probe' = NoProbe
    /\ UNCHANGED <<truth, now, rec, culprit, crashes, prsOpened, requests,
                    fixOpen, revertOpen, landed, lastLanding, checked, checkBetween>>

Tick ==
    /\ now < MaxTime /\ ~DeadlineDue
    /\ ~(DeadlineOn /\ Open /\ now + 1 >= Bound)
    /\ now' = now + 1
    /\ UNCHANGED <<state, truth, rec, culprit, slotHeld, runnerAlive,
                    probe, crashes, prsOpened, requests, fixOpen, revertOpen,
                    landed, lastLanding, checked, checkBetween>>

\* Atomic composition of a tick to Bound with Deadline at that new time.
TickAndDeadline ==
    /\ DeadlineOn /\ Open /\ now < Bound /\ now + 1 = Bound
    /\ now' = now + 1 /\ state' = "ESCALATED"
    /\ slotHeld' = FALSE /\ runnerAlive' = FALSE /\ probe' = NoProbe
    /\ UNCHANGED <<truth, rec, culprit, crashes, prsOpened, requests,
                    fixOpen, revertOpen, landed, lastLanding, checked, checkBetween>>

Next ==
    \/ BeginBisect
    \/ \E m \in Merges, kind \in Kinds : StartProbe(m, kind)
    \/ \E result \in {"pass", "fail"} : ProbeReturn(result)
    \/ \E abandon \in BOOLEAN : ProbeCrash(abandon)
    \/ \E m \in Merges : Confirm(m) \/ RequestRevert(m)
    \/ EscalateBisect \/ OpenFix
    \/ \E pr \in PRs : Land(pr)
    \/ \E result \in {"green", "red"} : Check(result)
    \/ Tick \/ TickAndDeadline \/ Deadline

Spec == Init /\ [][Next]_vars
        /\ WF_vars(Tick \/ TickAndDeadline \/ Deadline)

AtMostOneRevertPerCulprit == \A m \in Merges : prsOpened[m] <= 1
RevertOnlyForBrokenMerge == \A m \in Merges : prsOpened[m] > 0 => Evidence(m)
RevertTargetsTruth ==
    (~Flaky /\ ~NoBreak) =>
        (\A m \in Merges : prsOpened[m] > 0 => m = truth)
NoFixAndRevertBothLandWithoutCheck == landed = PRs => checkBetween
SlotReleasedOnExit ==
    state \in Terminal \cup {"CULPRIT_CONFIRMED", "REVERT_OPEN"} => ~slotHeld
SlotEventuallyReleased == slotHeld ~> ~slotHeld
RedReachesGreenOrEscalated == now >= Bound => state \in Terminal
=============================================================================
