---------------------- MODULE DelegationShrinkRetry ----------------------
(* The delegation ladder on a rubric failure (OMN-20172, plan 13.6 and 13.10).

   One request fails the class rubric on its first try. The ladder then goes:
     1. config swap  once, on the same model and backend, with the next-best
                     MET configuration (skipped when the model has no second
                     MET configuration for the class and band),
     2. shrink       split the request, run the units on the same model and
                     backend, recombine, and check the WHOLE with the class
                     rubric (skipped when the class is not decomposable),
     3. escalate     to the next responder in the class chain.
   A unit that fails may split once more, to depth MaxDepth. A unit that fails
   and cannot split further escalates alone (plan 13.6 step 3): its answer
   comes from another responder and the units are then recombined and the
   WHOLE is checked like any other. Unit attempts stop at UnitCap = twice the
   unit count, counted for each split level. A recombined whole is accepted
   only after it passes the class rubric; a whole that fails escalates. The
   request holds exactly one terminal record; a redelivery of the terminal
   event adds none.

   Whether a given attempt, unit run or whole check passes is the environment's
   choice, so every outcome is explored.

   Abstractions, stated so the review can judge them: the terminal modelled is
   the hand-off of this request's ladder (accepted, or escalated to the next
   responder); the next responder's own ladder is a separate instance of this
   model. Unit leases, the token-spend cap, the marginal-cost gate and the
   band-must-shrink check are not modelled; the last two are guards on the
   split rung that the handler tests cover. Only the terminal event is
   redelivered; the dedup mechanism (an event id) is the implementation's
   and DedupOn stands for it.

   Properties:
     AcceptedImpliesWholePassed  an accepted answer passed the class rubric,
                                 and a shrunk answer passed the WHOLE check
     SwapsAtMostCap              configuration swaps stay within the one-swap cap
     DepthAtMostMax              splitting stops at depth MaxDepth
     UnitAttemptsAtMostCap       unit attempts stop at UnitCap at each split
     AtMostOneTerminal           redelivery never adds a second terminal
     SwapBeforeShrink            a split happens only after the swap rung was
                                 used, or when it was not available
     EscalateOnlyWhenExhausted   a request escalates only after the swap rung
                                 (when available) and the shrink rung (when
                                 the class is decomposable) were tried
     Terminates                  every request ends with a terminal (liveness)
   Mutations (cfg constants) that must break them:
     DepthCapOn=FALSE    unbounded split                       -> DepthAtMostMax
     WholeCheckOn=FALSE  accept without the whole check        -> AcceptedImpliesWholePassed
     DedupOn=FALSE       a second terminal on redelivery       -> AtMostOneTerminal
     SwapCapOn=FALSE     swap configurations without a cap     -> SwapsAtMostCap
     SwapFirst=FALSE     shrink before the swap rung           -> SwapBeforeShrink
     UnitCapOn=FALSE     unit attempts without a cap           -> UnitAttemptsAtMostCap
     LadderOn=FALSE      escalate without trying the rungs     -> EscalateOnlyWhenExhausted *)
EXTENDS Integers

CONSTANTS MaxDepth, SwapCap, UnitCount, UnitCap, Horizon,
          Decomposable, HasSecondMet,
          DepthCapOn, UnitCapOn, SwapCapOn, WholeCheckOn, DedupOn, SwapFirst,
          LadderOn

VARIABLES phase, swaps, depth, unitAtt, shrank, accepted, wholeOK,
          wholeChecked, terminals, kind, deliveries

vars == <<phase, swaps, depth, unitAtt, shrank, accepted, wholeOK,
          wholeChecked, terminals, kind, deliveries>>

SwapLimit == IF SwapCapOn THEN SwapCap ELSE Horizon
DepthLimit == IF DepthCapOn THEN MaxDepth ELSE Horizon
UnitLimit == IF UnitCapOn THEN UnitCap ELSE UnitCap + Horizon

SwapAvail == HasSecondMet /\ swaps < SwapLimit
SplitAvail == Decomposable /\ depth < DepthLimit
                           /\ (~SwapFirst \/ ~HasSecondMet \/ swaps >= 1)
ResplitAvail == depth < DepthLimit

Init == /\ phase = "first" /\ swaps = 0 /\ depth = 0 /\ unitAtt = 0
        /\ shrank = FALSE /\ accepted = FALSE /\ wholeOK = FALSE
        /\ wholeChecked = FALSE
        /\ terminals = 0 /\ kind = "none" /\ deliveries = 0

Finish(k, ok) == /\ phase' = "terminal" /\ kind' = k
                 /\ accepted' = (k = "accepted") /\ wholeOK' = ok
                 /\ terminals' = terminals + 1

FirstTry == /\ phase = "first"
            /\ \/ /\ Finish("accepted", TRUE)
                  /\ UNCHANGED <<swaps, depth, unitAtt, shrank, wholeChecked,
                                 deliveries>>
               \/ /\ phase' = "failed"
                  /\ UNCHANGED <<swaps, depth, unitAtt, shrank, accepted,
                                 wholeOK, wholeChecked, terminals, kind,
                                 deliveries>>

Swap == /\ phase = "failed" /\ SwapAvail
        /\ swaps' = swaps + 1
        /\ \/ /\ Finish("accepted", TRUE)
           \/ /\ phase' = "failed"
              /\ UNCHANGED <<accepted, wholeOK, terminals, kind>>
        /\ UNCHANGED <<depth, unitAtt, shrank, wholeChecked, deliveries>>

Split == /\ phase = "failed" /\ SplitAvail
         /\ shrank' = TRUE /\ depth' = depth + 1 /\ unitAtt' = 0
         /\ phase' = "units"
         /\ UNCHANGED <<swaps, accepted, wholeOK, wholeChecked, terminals,
                        kind, deliveries>>

RunUnits == /\ phase = "units"
            /\ \E n \in UnitCount..(UnitLimit - unitAtt) :
                 /\ unitAtt' = unitAtt + n
                 /\ \/ phase' = "recombined" \/ phase' = "unitfail"
            /\ UNCHANGED <<swaps, depth, shrank, accepted, wholeOK,
                           wholeChecked, terminals, kind, deliveries>>

Resplit == /\ phase = "unitfail" /\ ResplitAvail
           /\ depth' = depth + 1 /\ unitAtt' = 0 /\ phase' = "units"
           /\ UNCHANGED <<swaps, shrank, accepted, wholeOK, wholeChecked,
                          terminals, kind, deliveries>>

UnitEscalateAlone == /\ phase = "unitfail" /\ ~ResplitAvail
                     /\ phase' = "recombined"
                     /\ UNCHANGED <<swaps, depth, unitAtt, shrank, accepted,
                                    wholeOK, wholeChecked, terminals, kind,
                                    deliveries>>

WholeCheck == /\ phase = "recombined"
              /\ wholeChecked' = WholeCheckOn
              /\ \/ Finish("accepted", TRUE)
                 \/ Finish(IF WholeCheckOn THEN "escalated" ELSE "accepted", FALSE)
              /\ UNCHANGED <<swaps, depth, unitAtt, shrank, deliveries>>

Escalate == /\ phase = "failed"
            /\ (~LadderOn \/ (~SwapAvail /\ ~SplitAvail))
            /\ Finish("escalated", FALSE)
            /\ UNCHANGED <<swaps, depth, unitAtt, shrank, wholeChecked,
                           deliveries>>

Redeliver == /\ phase = "terminal" /\ deliveries < 1
             /\ deliveries' = deliveries + 1
             /\ terminals' = IF DedupOn THEN terminals ELSE terminals + 1
             /\ UNCHANGED <<phase, swaps, depth, unitAtt, shrank, accepted,
                            wholeOK, wholeChecked, kind>>

Progress == FirstTry \/ Swap \/ Split \/ RunUnits \/ Resplit
            \/ UnitEscalateAlone \/ WholeCheck \/ Escalate

Next == Progress \/ Redeliver

Spec == Init /\ [][Next]_vars /\ WF_vars(Progress)

AcceptedImpliesWholePassed == accepted => (wholeOK /\ (shrank => wholeChecked))
SwapsAtMostCap == swaps <= SwapCap
DepthAtMostMax == depth <= MaxDepth
UnitAttemptsAtMostCap == unitAtt <= UnitCap
AtMostOneTerminal == terminals <= 1
SwapBeforeShrink == shrank => (~HasSecondMet \/ swaps >= 1)
EscalateOnlyWhenExhausted ==
    (kind = "escalated") =>
        ((~HasSecondMet \/ swaps >= 1) /\ (~Decomposable \/ shrank))
Terminates == <>(terminals >= 1)
=============================================================================
