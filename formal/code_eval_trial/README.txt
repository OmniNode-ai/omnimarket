Code eval trial model (OMN-23627 AC10: model before build)

What it models: the state_io row of node_code_eval_trial_orchestrator for one run
(the local-model code eval plan, revision 44, task E1: State,
Message flow, Legs, Close rule; rules 2, 3 and 7). Exemplar: formal/delegation_eval_run.
The seam to the plan's message-flow rows is the comment block at the top of CodeEvalTrial.tla.

Run it: java -XX:+UseParallelGC -Xmx2g -cp tla2tools.jar tlc2.TLC -config <cfg>.cfg -workers 2 CodeEvalTrial.tla
for each config (pinned tla2tools.jar: TLC 2.19, release v1.7.4, sha256
936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88; needs a Java 11+ runtime),
and save each output as results/<cfg>.out.
The design configs must end clean and every M* config must end in a TLC violation;
tests/formal/test_code_eval_trial_model.py reads the checked-in results and asserts both.

Configs
  CodeEvalTrial_design    2 tasks, 2 models, bound 3, untimed (a nudge may expire any item in
                          flight; the completion bound may end the run in any open state).
                          Invariants: TypeOK CloseOnlyUnderRule ExactlyOneClose NoTryBeforeControl
                          AtMostOneScore VerdictsReached DoneIsTerminal AtMostBoundTries
                          StopAtFirstPass VoidOnlyOnClose. Action properties: VerdictsStable
                          ClosedRowFrozen LateNoEffect VoidFrozen.
  CodeEvalTrial_timed     1 task, 2 models, bound 3, with the clock, hosts that come and go and a
                          tick route that can lose the run. Adds AbandonOnlyPastDeadline and
                          NudgeExpiresBeforeAbandon.
  CodeEvalTrial_liveness  the timed config under weak fairness of the reducer legs and strong
                          fairness of the tick (answers may be lost): EverySequenceEnds (every
                          sequence ends, or the run was abandoned) and RunCloses.
  CodeEvalTrial_liveness_normal  the clock, hosts up and the tick route alive: NormalClose (the run
                          closes under the close rule and is not abandoned, even when every answer
                          is lost) and EverySequenceEndsNormally.
Mutants (each must violate the property named; results/M*.out)
  M1 close with a try pending            CloseOnlyUnderRule
  M2 a late answer changes the row       VerdictsStable (a second answer for a sent control, no longer
                                         in flight, overwrites its verdict)
  M2b an unknown message changes the row LateNoEffect (an identity never sent)
  M3 place before the control passes     NoTryBeforeControl
  M4 two scores in flight                AtMostOneScore
  M5 the nudge never expires an item     NudgeExpiresBeforeAbandon (timed)
  M6 completion bound below the timeout  AbandonOnlyPastDeadline (timed; bound 1, deadline 3)
  M6b bound + 1 equals the deadline      NudgeExpiresBeforeAbandon (timed; bound 1, deadline 2)
  M7 liveness without fairness           Temporal properties (a positive control: the liveness
                                         check can fail)
  M8 drop the ordinal guard on a unit answer  AtMostBoundTries (a stale answer, for an ordinal
                                         below the current one, re-queues a try past the bound)
  M9 keep going after a pass             StopAtFirstPass
  M10 the nudge never expires, liveness  NormalClose
  M11 a closed run closes again (OMN-24173)  ExactlyOneClose (the close step without its closed guard,
                                         so a void close or any close can be published twice)
  M12 a closed row's void label is rewritten (OMN-24173)  VoidFrozen (the void label is set in the one
                                         close and never after)

Results (TLC 2.19, JRE 17, 2 workers, lab host h202; re-run for OMN-24173 after the void-close extension)
  CodeEvalTrial_design: clean; 3,945,519 generated, 57,788 distinct
  CodeEvalTrial_timed: clean; 265,787 generated, 7,340 distinct
  CodeEvalTrial_liveness: clean; 494,897 generated, 13,676 distinct
  CodeEvalTrial_liveness_normal: clean; 56,911 generated, 1,656 distinct
  M1_close_with_try_pending: Invariant CloseOnlyUnderRule; 1,782 generated, 58 distinct
  M2_late_answer_changes_row: Action property VerdictsStable; 367 generated, 16 distinct
  M2b_unknown_message_changes_row: Action property LateNoEffect; 29 generated, 6 distinct
  M3_place_before_control: Invariant NoTryBeforeControl; 5 generated, 4 distinct
  M4_concurrent_scores: Invariant AtMostOneScore; 73 generated, 5 distinct
  M5_no_nudge_expiry: Action property NudgeExpiresBeforeAbandon; 5,578 generated, 264 distinct
  M6_bound_below_timeout: Action property AbandonOnlyPastDeadline; 6,120 generated, 286 distinct
  M6b_bound_equals_deadline_minus_one: Action property NudgeExpiresBeforeAbandon; 6,087 generated, 286 distinct
  M7_no_fairness: Temporal properties were violated; 494,897 generated, 13,676 distinct
  M8_drop_ordinal_guard: Invariant AtMostBoundTries; 26,796 generated, 679 distinct
  M9_continue_after_pass: Invariant StopAtFirstPass; 23,399 generated, 589 distinct
  M10_no_nudge_expiry_liveness: Temporal properties were violated; 108,095 generated, 3,094 distinct
  M11_close_twice: Invariant ExactlyOneClose; 4,873 generated, 134 distinct
  M12_void_changes_after_close: Action property VoidFrozen; 615 generated, 23 distinct

Abstractions (what the model does not carry)
  A void close (OMN-24173) is the one close step with a void label: any value, set with the close
  (the label is a function of the closed row: a try still infrastructure-unscorable after its
  retries, more than a tenth of first tries unscorable for infrastructure, or a revision change
  mid-run) and frozen with it. An infrastructure retry (the same ordinal, the try or its score sent
  again, up to the overlay's count) is not a transition of the model: the reducer tests carry it.
  A verdict is pass or nopass: E2's fail and unscorable and the harness-unscorable ends all let
  the sequence go on (rule 7); the per-budget n and pass counts are a function of the closed row
  and belong to the reducer tests (AC13). Hosts are one flag; answers arrive with any content, any
  number of times, for any identity (a superset of the real at-least-once bus). Work a leg can
  dispatch (a score to send, a try to place on a free host, an allowed close) is done before time
  moves on in the timed configs, as the plan's Place-on-every-leg does. Answers may be lost (no
  fairness on delivery), so the nudge expiry is what ends a lost unit. The queued score is picked
  in any order, not oldest first. Close and Abandon are atomic and guarded by the closed flag, so
  ExactlyOneClose holds by construction; the build must carry the same guard as a compare-and-set
  between the reducer's close and the runtime's abandoned-row sweep (plan line 138 and 162).

Where the plan (revision 44) is ambiguous or contradicts the model
  1. Plan line 140 (State) names four slot values (pending, try in flight, score in flight, done);
     line 160 (Legs) queues a scorable end's score and sends "the oldest queued score ... when none
     is in flight". That needs a fifth slot (score queued) and a queued control. The model has them.
  2. "Row" is two things: the state_io row (line 140; AC10's "a late answer changes the row") and
     the published context-ROI row (line 150, the Row step; line 160 "published as its row"). The
     model's row is the state_io row; publication is part of the verdict write.
  3. Line 162: completion_bound must exceed the try timeout and E2's timeout plus slack "so a run
     waiting only on a unit or a score is not abandoned". That holds only because the sweep
     measures the time since the last write (omnibase_infra state_store_adapter.py:396-399, "A row
     still being advanced by a live leg pushes updated_at forward"); line 140 relies on it ("writes
     nothing, so updated_at stands") but line 162 does not say so. On a whole-run wall clock a
     three-try sequence would be abandoned. The model uses the since-last-write reading.
  4. Line 160 says the run leg raises a configuration error when the timeouts plus slack "do not
     fit under" the bound. The model shows the edge matters: with bound + 1 equal to the deadline
     (M6b) the abandon and the expiry race and a run is abandoned with an expired item still in
     flight; with a bound further below (M6) it is abandoned with a unit inside its deadline. The
     model assumes the bound is strictly above each deadline (the ASSUME in CodeEvalTrial.tla).
  5. Rule 2 (line 110): E2's failed event is not read, so a lost or failed score holds the single
     score slot until the nudge expires it. Every other score and every later control waits for it.
     Not a contradiction; it serialises the whole run on one slow score.
  6. Line 162 defines an ended sequence by its verdicts; a task whose control did not hold has no
     sequence (line 121). The model gives it the slot excluded and counts it ended. The plan should
     say so. Line 160 sends the oldest queued score; the model picks any (a superset, so a
     starvation of one score would hide behind the completion bound). Line 121 separates
     unscorable(falsifier_not_held) from other unscorable (could not be run): the model gives both
     the same effect on the sequences and keeps only the difference between held and not held.
  7. Line 160 and 162 compare the bound with the timeouts plus slack in seconds. The model's tick is
     the nudge: the sweep and the nudge each act once per tick, so the bound must exceed each
     deadline by at least one tick interval, or the abandon races the expiry (M6b). The plan's
     check should add the nudge interval to the sum it compares.
  Not expressible (see the header of CodeEvalTrial.tla): the run leg's reads (overlay, pin,
  admission and exclusion counts, bands, the dead letter), capacity readings, the cells and paired
  steps (the stateless close route), and the budgets 1, 3 and 5.
