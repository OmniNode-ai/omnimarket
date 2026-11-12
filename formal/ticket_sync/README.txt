TicketSync: two branches and a person on one ticket, with the ticket node's sync and pull (OMN-23828, a child of OMN-23761, plan task S4)
=====================================================================================================================================

Files
-----
TicketSync.tla       the model; its header says what it models, every design knob, every property and
                     what it leaves out
base1 .. base7_fedit2 the design (every knob on): base1 one command per branch; base2 two commands on b1,
                     one on b2; base3_crash and base4_crash2 the same with one crash; base5_wide and
                     base6_wide2 two commands on each branch (one and two hand edits). Each checks
                     TypeOK, OneTerm, DoneHasTerm, P1, P3, P6, NoopNoWrite, SyncedMatch,
                     EventsAtMostOne and EventsExact
quiesce.cfg          the design with crashes and TLC's deadlock check on: a command that never ends
                     in a terminal is a deadlock
residual.cfg         the design checked against the strict property P1s: TLC finds the residual race
                     (VERDICT.txt states it and its bound)
M1 .. M13            one mutant per design knob (below); each must violate the one invariant its
                     config checks (M12 ends in a deadlock)
wit_*.cfg            witnesses: each checks a "not reached" invariant that TLC must report violated,
                     so the passing runs are not vacuous
results/             one TLC output per config (the last line is exit=<code>) and SUMMARY.txt
                     (formal/ticket_sync/results/SUMMARY.txt) with the sha256 of the model, each
                     config and each output
VERDICT.txt          the design verdict, the requirements the S4 code is built to, the residual race
                     and the reviewer's verdict
Followed: formal/throughput_tick (README, VERDICT, base and mutant and witness configs, results/ with
SUMMARY.txt, tests/formal/test_*_model.py). The S3 model (OMN-23760, formal/create_ticket_draft in that
lane's worktree, not yet committed when this lane read it) was read for the written event's key and for
hashing what Linear returns; this model does not depend on it.

Run
---
TLC 2.19 (tla2tools.jar v1.7.4, sha256 936a2620...50e88), Temurin 17 JRE (the docker image
eclipse-temurin:17-jre-alpine) on the lab host h053:

  java -XX:+UseParallelGC -Xmx2g -cp tla2tools.jar tlc2.TLC -config <cfg> -workers 6 \
       -metadir <tmp>/<cfg> -cleanup TicketSync.tla

TLC exits 0 when it finds no error, 12 on an invariant violation and 11 on a deadlock. Run from this
directory. The base6_wide2 run is 146 million states generated (70.6 million distinct), 8 min 30 s with
16 workers and 4 GB; every other run is under 3 minutes and 2 GB. results/SUMMARY.txt lists each
config's sha256, its output's sha256 and its distinct states. tests/formal/test_ticket_sync_model.py
fails when the model or a config changes without a re-run, when a recorded result is not the expected
one, or when VERDICT.txt does not name the model's digest.

The model as written
--------------------
One ticket (the block fingerprint blk, the hash h of the live title and description, txt for a hand
edit of the text outside the block, by for who wrote last), two branches each with one file (base, the
fingerprint recorded by the last sync or pull; cur, the contract as the branch has edited it), a person
who edits the text or the block at any time, and the event log. A command is a sync, a sync --replace X
or a pull. A sync is the steps Begin (read the ticket, the newest event and the file, then decide),
Render, Recheck (read the ticket again), Write, File (write the new fingerprint into the file),
Publish (the written event) and Term (the one terminal); a pull is Begin, PullFile, Publish, Term and
writes no Linear. Any other action may run between two steps. A run may crash between steps; the
command is delivered again.

The knobs (the design has all on; each mutant turns one off)
------------------------------------------------------------
M1  Reread off: no second read just before the write: violates P1
M2  Serial off: two runs on the ticket at once: violates P1
M2b the same, a write from an older fingerprint: violates P3
M3  HashCheck off: a live hash that differs from the newest event does not refuse: violates P1
M4  DriftCheck off: a block that is not the file's recorded fingerprint does not refuse: violates P3
M5  PullFlagsText off: the adoption event carries the hash alone (the brief's design): violates P1
M6  ReadbackHash on: the written event hashes a later read of the ticket, not the write's response: violates EventsExact
M6b the same, the later sync that overwrites the edit that read blessed: violates P1
M7  FileWrite off: a written sync does not record the new fingerprint in the file: violates SyncedMatch
M8  EmitOnRefuse off: a refusal ends without a terminal: violates DoneHasTerm
M9  NoopSkip off: a sync whose file, block and hash all agree writes anyway: violates NoopNoWrite
M10 ReplaceChecksName off: sync --replace X writes whatever the block is: violates P1
M11 EventKeyed off: a redelivered command publishes a second event: violates EventsAtMostOne
M12 Redeliver off: a command whose run crashed is lost: ends in a deadlock
M13 Recover off: a redelivered sync refuses its own earlier write: violates P6

Mapping to the plan's code (what S4 builds; the knobs are its requirements, see VERDICT.txt)
-------------------------------------------------------------------------------------------
Begin           the read of the ticket (title, description, block fingerprint), of the newest written
                event on onex.evt.omnimarket.ticket-contract-written.v1 and of contracts/<id>.yaml
Recheck         the read of the ticket just before the write (the narrowest fix of the read-then-write gap)
Write           the Linear write call; its response gives the stored description whose hash the event carries
File, Publish   the new fingerprint into the file, then the written event
PullFile        contracts/<id>.yaml written from the ticket's block, with the adoption event carrying the
                hash as read and whether the text outside the block was carried (cap)
The seam: the gap between sync's last read and its write. A model has no bus: the seam is measured by
TLC (residual.cfg finds the one open window; P1 holds with it excused and no other gap). The live
reading of the same seam (a hand edit in Linear after a sync's read, one sync refused ticket_changed)
is S4's integrated criterion on the dev lane and is not made here.
