CreateTicketDraft: the ticket node's draft create, a retry after a failure, duplicate delivery
================================================================================================

Ticket OMN-23760 (plan S3 of the standard-tickets plan). Model before build: this directory holds the TLA+ model of the
create-then-write path of node_create_ticket when it is given a draft, its TLC results and the design-review verdict. No node
code is changed here.

Files
-----
CreateTicketDraft.tla   the model; its header says what it models, the design (D1 to D8) and what it leaves out
Model.cfg               the design, one draft, the full bound (an original command and one duplicate, 2 crashes, 2 Linear
                        failures, 1 hand edit), eleven safety invariants
Model_live.cfg          the same without the hand edit, with the liveness properties RunEnds and CommandAnswered
Model_2draft_fail.cfg   two drafts, no duplicate, 2 crashes, 2 failures, safety and liveness
Model_2draft_crash.cfg  two drafts, a duplicate, 1 crash, no failure (symmetry on the two drafts)
Model_2draft_dupfail.cfg two drafts, a duplicate, 1 failure, no crash (symmetry on the two drafts)
alt_stamp_serial.cfg    the alternative design for a Linear that refuses a client-supplied issue id: search by stamp, the
                        runs of one draft never overlapping, a consistent filter
mut_*.cfg               one mutant per property or design point; each flips the knob named in its file and must violate
                        the property its config checks (see the table in VERDICT.txt)
haz_*.cfg               two hazards the design accepts and the review recorded
wit_*.cfg               ten witnesses: each says a state is unreachable and must be violated, which shows it is reachable
results/                one TLC output per config, formal/create_ticket_draft/results/SUMMARY.txt (sha256 of the model, each config and each output),
                        review_round1.txt and review_round2.txt (the reviewer's reports)
VERDICT.txt             the design verdict and the conditions the build must meet

Run
---
TLC 2.19 (tla2tools.jar v1.7.4, sha256 936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88) in the
eclipse-temurin:17-jre-alpine image on lab host h053, one process per config, from this directory:

  java -XX:+UseParallelGC -Xmx2g -cp tla2tools.jar tlc2.TLC -config <cfg> -workers 2 -metadir <scratch>/<cfg> -cleanup CreateTicketDraft.tla

(the plan adds no script, so this is the command line of tests/formal/*_tlc.py and of formal/lander_lease). TLC exits 0 when it
finds no error, 12 on an invariant violation and 13 on a temporal one. results/<cfg>.out ends with exit=<code>; the lines that
vary between runs (version banner, clock times, progress) are removed.

The model in one paragraph
--------------------------
One draft, identified by the draft's own correlation id (a field the build adds to the request; the bus envelope's id changes on a
retry and is not it). A run is one delivery of one command: find, create, (relook), (stamp), clear the inherited project, build
(memory only), write the description, read back what Linear returns, publish the written event, publish the terminal, ack.
A run may crash between any two steps; Linear and the bus keep what they hold. The bus is at least once: a command not
acknowledged is delivered again, the caller may retry with the same draft id, and the bus may deliver one command twice at the
same time. A Linear call may fail, a create or a write may take effect and still report failure, a lookup may miss, a person may
edit the issue, the terminal may fail to publish.

The design the model checks (Model.cfg)
---------------------------------------
D1 the issue id is derived from the draft id and sent with the create; a second create for the same draft is refused and the run
   looks the issue up by id (no search, no race). Assumes Linear accepts a client id: unmeasured, a build condition.
D2 the stamp is written in the create call and kept by the description write.
D3 after a refused create, one lookup; a miss ends the run with the typed reason id_unreadable.
D4 every run clears the inherited project before the write.
D5 a rerun that finds no block builds and writes; one that finds the block reads back and publishes again; one that finds an
   edited issue fails with hand_edited and writes nothing.
D6 readback must return the block built; the event hashes what readback returned, after the write; its id is the key, not the
   run; the fingerprint depends only on the draft and the issue.
D7 every failure ends the run with one typed failed terminal; completed follows the written event.
D8 the consumer acknowledges only after the terminal is published.

What TLC found, and what changed (the rule: change the design, not the property)
-------------------------------------------------------------------------------
- The ticket's own design (a stamp in the issue, a rerun finds it by search) makes two issues for one draft when the bus delivers
  a command twice in parallel (mut_stamp_search_concurrent.cfg, 7 states: start, find, duplicate, find, create, create), or when a
  search misses a just-made issue (mut_find_lag.cfg), or when the stamp is a second call after the create (mut_stamp_after_create.cfg).
  Design change D1 (and D2): the id carried in the create. The fallback when Linear refuses a client id is alt_stamp_serial.cfg.
- Round 1 of the review (FAIL) found the hand edit between the description write and the readback (completed reported for an
  issue with no block, and a rerun republishing an edit under a rebuilt fingerprint), an unbounded refused-create loop, a resume
  that skips the project clear, a fingerprint that may differ between runs, and the draft id that exists nowhere in the code.
  Design changes D3 to D6 and the property restated on what the readback returned; the draft id is a build condition.
  Round 2 returned PASS with conditions.

Properties
----------
AtMostOneIssuePerCid     one draft, one issue
OneTerminalPerRun        a run emits at most one terminal
DoneHasTerminal          a run that ends has emitted one
TerminalsTyped           completed carries the id and the fingerprint; failed carries linear_error, hand_edited or id_unreadable
RunEnds, CommandAnswered liveness: a run ends, and a command ends with a terminal in the log (given redelivery)
WrittenFollowsWrite      a written event exists only for an issue whose description write took effect
WrittenHashesReturned    the event's hash is one a readback returned
WrittenCertifiesBlock    that hash is the block's (an edit is never certified)
OneWrittenPerCid         one written event per draft (the event id is the key; consumers dedupe on it)
CompletedHasBlock        completed only when the read held the block and the write happened
CompletedHasWritten      completed only after the written event
CompletedNoProject       completed only for an issue whose inherited project was cleared

Controls
--------
Each mutant checks the one property named in its cfg. Two mutants flip two knobs and say so: mut_publish_before_write also drops the
readback check (the readback of an unwritten issue would otherwise fail it), and mut_resume_trusts_issue skips both the block check
and the readback and publish. CompletedHasBlock follows from CompletedHasWritten plus WrittenFollowsWrite, so only
mut_resume_trusts_issue breaks it. mut_find_lag and mut_stamp_search_concurrent also break OneWrittenPerCid (a consequence of two
issues). haz_fail_after_complete: a later run of a completed draft may fail on a Linear error, so a consumer takes the first
completed. haz_block_without_event: a write that took effect with a failed run after it leaves a block and no event until the
caller retries.

Bounds and what is not covered
------------------------------
The one-draft config has the full bound; the two-draft configs trade one budget each for size (the state counts are in
results/SUMMARY.txt). Not modelled: the contract's content, the guard and the lane stamp, the title hash apart from the description,
the bus front's in-process replay cache, an edit landing between a run's find and its write (the write wins), the consumer commit
mode in the deployed runtime, time. Linear's behaviour (a client id, a duplicate refusal, the markdown normalisation the readback
comparator must tolerate) is unmeasured and is condition B3 and B12 of the verdict.
