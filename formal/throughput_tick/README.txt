ThroughputTick: the hourly check's send decision and its dedupe store (OMN-23732, task H0)
==========================================================================================

Files
-----
ThroughputTick.tla   the model; its header says what it models and what it leaves out
base1.cfg            the design without a crash: lock, reread under the lock, locked writers, one
                     store; checks TypeOK, SendAttemptOnce and StartedOnce
base2.cfg            the design with crashes: adds the consumer that starts one lane per key;
                     checks StartedOnce
M1 .. M7, T1         one mutant per design knob (below); each must violate the one invariant its
                     config checks
wit_*.cfg            witnesses: each checks a "not reached" invariant that TLC must report violated,
                     so the passing runs are not vacuous
results/             one TLC output per config (the last line is exit=<code>) and SUMMARY.txt with
                     the sha256 of the model, each config and each output
MAPPING.txt          the model's send keys against the keys the live tick writes
VERDICT.txt          the design verdict, the requirements H3 builds to, the reviewer's verdict
Followed: formal/lander_lease (README, base.cfg and one config per mutant, results/ with SUMMARY.txt,
VERDICT.txt, tests/formal/test_*_model.py). docs/formal/remote_lane_stop was read for its witness
configs (wit_*), which this model adopts.

Run
---
TLC 2.19 (tla2tools.jar v1.7.4, sha256 936a2620...50e88), Temurin 17 JRE, on the lab host h053:

  java -XX:+UseParallelGC -Xmx2g -cp tla2tools.jar tlc2.TLC -config <cfg> -workers 2 \
       -metadir <tmp>/<cfg> -cleanup ThroughputTick.tla

TLC exits 0 when it finds no error and 12 on an invariant violation. Run from this directory.
The runs are under 3 s and 2 GB each, so they were not placed through onex_lab_run (see SUMMARY.txt).

The model as written
--------------------
Two checks t1 and t2 and one foreign writer f1 share one store (a set of send keys) and one flock.
A check runs Decide (read the store, list the keys it does not hold), Lock, Reread (read the store
under the lock, drop what it holds), then per key Send (call the runner) and Record (write its
private copy back as the whole store). A check may die between any two steps (Crash); the flock
dies with it. The foreign writer reads the store and later replaces it whole from that read. The
runner starts one lane per key (ConsumerDedupe) or one per call. Keys are the live tick's send keys
(MAPPING.txt). The dedupe window is one window.

Properties
----------
SendAttemptOnce   the runner is called (a call it accepted) at most once per key
StartedOnce       at most one lane is started per key

Mutants and witnesses (each flips one knob of base1 or base2)
-------------------------------------------------------------
Config                    Knob flipped                                   Result
base1                     (design, no crash)                             PASS, 190 states
base2                     (design, crash, consumer dedupes by key)       PASS, 1011 states
M1_no_lock                LockedRMW=FALSE                                violates SendAttemptOnce
M2_no_reread              ReReadUnderLock=FALSE                          violates SendAttemptOnce
M3_writer_unlocked        LockedWriters=FALSE                            violates SendAttemptOnce
M4_lock_fail_open         LockFailOpen=TRUE                              violates SendAttemptOnce
M5_crash_no_dedupe        base2 with ConsumerDedupe=FALSE                violates StartedOnce
M6_crash_lock_only        the same config as M5, the call-level property violates SendAttemptOnce
M7_unreadable_store_empty ReadFailOpen=TRUE                              violates SendAttemptOnce
T1_unlocked_stores_today  LockedRMW=FALSE and ReReadUnderLock=FALSE      violates SendAttemptOnce
                          (the relaunch, green and floor stores as the source has them)
wit_skipped_all           t2 finds every key recorded and sends none     NotSkippedAll violated (reached)
wit_refused_retry         a retried call the runner refuses to restart   NotRefusedRetry violated (reached)
wit_writer_erased         the foreign writer erases a recorded key       NotWriterErased violated (reached)

Receipts: formal/throughput_tick/results/SUMMARY.txt

Verdict
-------
See VERDICT.txt.
