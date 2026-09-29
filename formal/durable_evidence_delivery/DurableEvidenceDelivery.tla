--------------------- MODULE DurableEvidenceDelivery ---------------------
(***************************************************************************
 OR.3 preparation model.  It is deliberately abstract: it models the
 admission boundary around one execution, rather than selecting a storage
 backend or changing the verifier.  The service owner, Jonah/operator, owns
 the later retention/deletion/access/restore policy; that policy is not
 invented here.

 A result is eligible only after an off-host intent exists, a PASS has a
 durable confirmation, and the attempt still names the current contract
 revision.  A lost result is retained as UNRESOLVED.  Confirmation delivery
 may be redelivered but it never creates a second logical attempt.  Cleanup
 may occur, but cannot discard a recorded result or its durable confirmation.
***************************************************************************)
EXTENDS Naturals, TLC

CONSTANTS MaxRevision, MaxAttempt, RequireIntent, RequireExactIntentBinding,
          RequireSubjectBinding,
          RequireConfirmation, IdempotentConfirmation, RequireCurrentRevision,
          RequireHighestAllocated, PreserveUnconfirmedLoss, RetainHistory

Subject == "candidate"
ForeignSubject == "foreign-candidate"
Revisions == 1..MaxRevision
Attempts == 1..MaxAttempt
AttemptStates == {"NOT_STARTED", "RUNNING", "RESULT_RECORDED",
                  "CONFIRMED", "UNRESOLVED", "CANCELLED"}
Results == {"NONE", "PASS", "LOST"}
Artifacts == {"NONE", "LOCAL", "PERSISTED"}

VARIABLES currentRevision, nextSequence, intentRevision, intentSubject,
          attemptState, result, artifact, dispatchWasStale, logicalDeliveries,
          lostBeforeConfirmation, admitted, selectedAttempt, historyRetained,
          cleanupCount

vars == <<currentRevision, nextSequence, intentRevision, intentSubject,
          attemptState, result, artifact, dispatchWasStale, logicalDeliveries,
          lostBeforeConfirmation, admitted, selectedAttempt, historyRetained,
          cleanupCount>>

TypeOK ==
    /\ currentRevision \in Revisions
    /\ nextSequence \in 1..(MaxAttempt + 1)
    /\ intentRevision \in [Attempts -> ({0} \cup Revisions)]
    /\ intentSubject \in [Attempts -> ({"NONE", Subject, ForeignSubject})]
    /\ attemptState \in [Attempts -> AttemptStates]
    /\ result \in [Attempts -> Results]
    /\ artifact \in [Attempts -> Artifacts]
    /\ dispatchWasStale \in [Attempts -> BOOLEAN]
    /\ logicalDeliveries \in [Attempts -> 0..2]
    /\ lostBeforeConfirmation \in [Attempts -> BOOLEAN]
    /\ admitted \in BOOLEAN
    /\ selectedAttempt \in {0} \cup Attempts
    /\ historyRetained \in [Attempts -> BOOLEAN]
    /\ cleanupCount \in [Attempts -> 0..1]

Init ==
    /\ currentRevision = 1
    /\ nextSequence = 1
    /\ intentRevision = [i \in Attempts |-> 0]
    /\ intentSubject = [i \in Attempts |-> "NONE"]
    /\ attemptState = [i \in Attempts |-> "NOT_STARTED"]
    /\ result = [i \in Attempts |-> "NONE"]
    /\ artifact = [i \in Attempts |-> "NONE"]
    /\ dispatchWasStale = [i \in Attempts |-> FALSE]
    /\ logicalDeliveries = [i \in Attempts |-> 0]
    /\ lostBeforeConfirmation = [i \in Attempts |-> FALSE]
    /\ admitted = FALSE
    /\ selectedAttempt = 0
    /\ historyRetained = [i \in Attempts |-> TRUE]
    /\ cleanupCount = [i \in Attempts |-> 0]

RecordIntent ==
    /\ ~admitted
    /\ nextSequence <= MaxAttempt
    /\ intentRevision' = [intentRevision EXCEPT ![nextSequence] = currentRevision]
    /\ intentSubject' = [intentSubject EXCEPT ![nextSequence] = Subject]
    /\ nextSequence' = nextSequence + 1
    /\ UNCHANGED <<currentRevision, attemptState, result, artifact,
                   dispatchWasStale, logicalDeliveries, admitted,
                   lostBeforeConfirmation, selectedAttempt, historyRetained,
                   cleanupCount>>

RecordForeignIntent ==
    /\ ~admitted
    /\ nextSequence <= MaxAttempt
    /\ intentRevision' = [intentRevision EXCEPT ![nextSequence] = currentRevision]
    /\ intentSubject' = [intentSubject EXCEPT ![nextSequence] = ForeignSubject]
    /\ nextSequence' = nextSequence + 1
    /\ UNCHANGED <<currentRevision, attemptState, result, artifact,
                   dispatchWasStale, logicalDeliveries, admitted,
                   lostBeforeConfirmation, selectedAttempt, historyRetained,
                   cleanupCount>>

Dispatch(i) ==
    /\ i \in Attempts
    /\ attemptState[i] = "NOT_STARTED"
    /\ (~RequireIntent \/ intentRevision[i] # 0)
    /\ (~RequireExactIntentBinding \/ intentRevision[i] = 0 \/
        intentRevision[i] = currentRevision)
    /\ (~RequireSubjectBinding \/ intentRevision[i] = 0 \/
        intentSubject[i] = Subject)
    /\ attemptState' = [attemptState EXCEPT ![i] = "RUNNING"]
    /\ dispatchWasStale' = [dispatchWasStale EXCEPT ![i] =
          ~(intentSubject[i] = Subject /\ intentRevision[i] = currentRevision)]
    /\ logicalDeliveries' = [logicalDeliveries EXCEPT ![i] = 1]
    /\ UNCHANGED <<currentRevision, nextSequence, intentRevision, intentSubject,
                   result, artifact, admitted, selectedAttempt,
                   lostBeforeConfirmation, historyRetained, cleanupCount>>

RecordPassLocally(i) ==
    /\ i \in Attempts
    /\ attemptState[i] = "RUNNING"
    /\ result' = [result EXCEPT ![i] = "PASS"]
    /\ artifact' = [artifact EXCEPT ![i] = "LOCAL"]
    /\ attemptState' = [attemptState EXCEPT ![i] = "RESULT_RECORDED"]
    /\ UNCHANGED <<currentRevision, nextSequence, intentRevision, intentSubject,
                   dispatchWasStale, logicalDeliveries, admitted, selectedAttempt,
                   lostBeforeConfirmation, historyRetained, cleanupCount>>

RecordLostResult(i) ==
    /\ i \in Attempts
    /\ attemptState[i] = "RUNNING"
    /\ result' = [result EXCEPT ![i] = "LOST"]
    /\ attemptState' = [attemptState EXCEPT ![i] = "UNRESOLVED"]
    /\ UNCHANGED <<currentRevision, nextSequence, intentRevision, intentSubject,
                   artifact, dispatchWasStale, logicalDeliveries, admitted,
                   lostBeforeConfirmation, selectedAttempt, historyRetained,
                   cleanupCount>>

LoseUnconfirmedPass(i) ==
    /\ i \in Attempts
    /\ attemptState[i] = "RESULT_RECORDED"
    /\ result[i] = "PASS"
    /\ artifact[i] = "LOCAL"
    /\ result' = [result EXCEPT ![i] =
          IF PreserveUnconfirmedLoss THEN "LOST" ELSE "PASS"]
    /\ attemptState' = [attemptState EXCEPT ![i] =
          IF PreserveUnconfirmedLoss THEN "UNRESOLVED" ELSE "RESULT_RECORDED"]
    /\ lostBeforeConfirmation' = [lostBeforeConfirmation EXCEPT ![i] = TRUE]
    /\ UNCHANGED <<currentRevision, nextSequence, intentRevision, intentSubject,
                   artifact, dispatchWasStale, logicalDeliveries, admitted,
                   selectedAttempt, historyRetained, cleanupCount>>

CancelAttempt(i) ==
    /\ i \in Attempts
    /\ attemptState[i] = "RUNNING"
    /\ attemptState' = [attemptState EXCEPT ![i] = "CANCELLED"]
    /\ UNCHANGED <<currentRevision, nextSequence, intentRevision, intentSubject,
                   result, artifact, dispatchWasStale, logicalDeliveries, admitted,
                   lostBeforeConfirmation, selectedAttempt, historyRetained,
                   cleanupCount>>

ConfirmDurably(i) ==
    /\ i \in Attempts
    /\ attemptState[i] = "RESULT_RECORDED"
    /\ result[i] = "PASS"
    /\ artifact[i] = "LOCAL"
    /\ artifact' = [artifact EXCEPT ![i] = "PERSISTED"]
    /\ attemptState' = [attemptState EXCEPT ![i] = "CONFIRMED"]
    /\ UNCHANGED <<currentRevision, nextSequence, intentRevision, intentSubject,
                   result, dispatchWasStale, logicalDeliveries, admitted,
                   lostBeforeConfirmation, selectedAttempt, historyRetained,
                   cleanupCount>>

RedeliverConfirmation(i) ==
    /\ i \in Attempts
    /\ attemptState[i] = "CONFIRMED"
    /\ result[i] = "PASS"
    /\ artifact[i] = "PERSISTED"
    /\ logicalDeliveries' = [logicalDeliveries EXCEPT ![i] =
          IF IdempotentConfirmation THEN @ ELSE 2]
    /\ UNCHANGED <<currentRevision, nextSequence, intentRevision, intentSubject,
                   attemptState, result, artifact, dispatchWasStale, admitted,
                   lostBeforeConfirmation, selectedAttempt, historyRetained,
                   cleanupCount>>

ReviseContract ==
    /\ ~admitted
    /\ currentRevision < MaxRevision
    /\ currentRevision' = currentRevision + 1
    /\ UNCHANGED <<nextSequence, intentRevision, intentSubject, attemptState,
                   result, artifact, dispatchWasStale, logicalDeliveries,
                   lostBeforeConfirmation, admitted, selectedAttempt,
                   historyRetained, cleanupCount>>

Admit(i) ==
    /\ i \in Attempts
    /\ ~admitted
    /\ result[i] = "PASS"
    /\ (~RequireConfirmation \/ artifact[i] = "PERSISTED")
    /\ (~RequireCurrentRevision \/ intentRevision[i] = currentRevision)
    /\ (~RequireHighestAllocated \/ i = nextSequence - 1)
    /\ admitted' = TRUE
    /\ selectedAttempt' = i
    /\ UNCHANGED <<currentRevision, nextSequence, intentRevision, intentSubject,
                   attemptState, result, artifact, dispatchWasStale,
                   logicalDeliveries, lostBeforeConfirmation, historyRetained,
                   cleanupCount>>

Cleanup(i) ==
    /\ i \in Attempts
    /\ result[i] # "NONE"
    /\ cleanupCount[i] = 0
    /\ cleanupCount' = [cleanupCount EXCEPT ![i] = 1]
    /\ historyRetained' = [historyRetained EXCEPT ![i] = RetainHistory]
    /\ UNCHANGED <<currentRevision, nextSequence, intentRevision, intentSubject,
                   attemptState, result, artifact, dispatchWasStale,
                   logicalDeliveries, lostBeforeConfirmation, admitted,
                   selectedAttempt>>

Next == RecordIntent \/ RecordForeignIntent \/ (\E i \in Attempts: Dispatch(i)) \/
        (\E i \in Attempts: RecordPassLocally(i)) \/
        (\E i \in Attempts: RecordLostResult(i)) \/
        (\E i \in Attempts: LoseUnconfirmedPass(i)) \/
        (\E i \in Attempts: CancelAttempt(i)) \/
        (\E i \in Attempts: ConfirmDurably(i)) \/
        (\E i \in Attempts: RedeliverConfirmation(i)) \/ ReviseContract \/
        (\E i \in Attempts: Admit(i)) \/ (\E i \in Attempts: Cleanup(i))

Spec == Init /\ [][Next]_vars

DispatchUsesRecordedIntent ==
    \A i \in Attempts: attemptState[i] # "NOT_STARTED" => intentRevision[i] # 0
DispatchUsesExactCurrentIntent ==
    \A i \in Attempts: attemptState[i] # "NOT_STARTED" => ~dispatchWasStale[i]
DispatchUsesExactSubjectIntent ==
    \A i \in Attempts: attemptState[i] # "NOT_STARTED" =>
        intentSubject[i] = Subject
NoAdmissionWithoutConfirmation ==
    admitted => artifact[selectedAttempt] = "PERSISTED"
LostIsExplicitlyUnresolved ==
    \A i \in Attempts: result[i] = "LOST" => attemptState[i] = "UNRESOLVED"
LostUnconfirmedPassIsUnresolved ==
    \A i \in Attempts: lostBeforeConfirmation[i] =>
        /\ result[i] = "LOST"
        /\ attemptState[i] = "UNRESOLVED"
        /\ artifact[i] = "LOCAL"
NoStaleRevisionAdmission ==
    admitted => intentRevision[selectedAttempt] = currentRevision
HighestAllocatedAttemptSelected ==
    admitted => selectedAttempt = nextSequence - 1
OneLogicalAttemptPerExecution ==
    \A i \in Attempts: logicalDeliveries[i] <= 1
RecordedEvidenceIsRetained ==
    \A i \in Attempts: result[i] # "NONE" => historyRetained[i]
=============================================================================
