LanderLease: the permit, the fence and the kept tree of the lane lander
=======================================================================

Files
-----
LanderLease.tla    the model; its header says what it models and what it leaves out
base.cfg           the design: four safety invariants, one liveness property
M1.cfg .. M5.cfg   one mutant per property; each flips one design knob and must violate
                   the one property its config checks. M4b is the second way to break
                   SingleOwnerOfTree (the lock file inside the tree directory).
draft/             the test-first draft: LanderLease.tla with a cold dispatch that reads
                   only an unchanged head and a cold push with no lease. base.cfg must
                   fail on it; results/draft_base.out is that failing run.
results/           one TLC output per config, draft_base.out, and SUMMARY.txt with the
                   sha256 of the model, each config and each output, and the lab-run ids
VERDICT.txt        the design verdict

Run
---
TLC 2.19 (tla2tools.jar v1.7.4, sha256 936a2620...50e88) on the lab host h201, inside the
eclipse-temurin:17-jre-alpine image, with the command line the tests/formal/*_tlc.py
runners build:

  java -XX:+UseParallelGC -Xmx4g -cp tla2tools.jar tlc2.TLC -config <cfg> -workers 2 \
       -metadir states/<cfg> -cleanup LanderLease.tla

There is no model-independent runner (each runner names its own model directory and
matrix), and the plan adds no script, so the same command was placed on h201 through
onex_lab_run.py --via bus at the pushed branch, one TLC process per config. The exit code
and the output of each run are in results/<cfg>.out (the last line is exit=<code>); TLC
exits 0 when it finds no error, 12 on an invariant violation and 13 on a temporal one.
Rerun with the command above from formal/lander_lease (the draft from formal/lander_lease/draft
with -config ../base.cfg).

The model as written
--------------------
One pull request, one kept tree, one permit (one dispatch_id). The controller grants a lease
on a kept tree whose record is ready, issues a permit stamped with issued_at on its own
clock (cutoff_at = issued_at + HoldS), sends offer, cancel, status and release, ends the
permit when status shows it terminal or at cutoff_at, and then falls back to the cold
worker. The host job runs offer, START, engine, push, done; it may die between any two
steps and status then finishes it (done failed, or done unresolved if it had reached
pushing); nothing is replayed. A message may be lost (never delivered), late (it stays in
the network until delivered, even after cutoff_at) or reordered (the network is a set); the
host may pause (its steps interleave freely with the controller's ticks). now, the
controller's tick, is the only absolute time; the host has no clock. Two heads divergent from
the base are pushed from the same base: the permit's commit and the cold worker's commit.

Properties
----------
AtMostOneAuthorizedPublisher  no cold dispatch inside [issued_at, cutoff_at) while the
                              permit is not observed terminal
NoLostUpdate                  every accepted commit stays reachable from the branch head; two
                              divergent heads from one base never both land, in either
                              acceptance order, including a push accepted after cutoff_at
ExactlyOnceChild              one dispatch_id, at most one engine child
SingleOwnerOfTree             only the lock holder mutates a kept tree; no remover other than
                              a release job holding the tree's lock grant deletes a tree
                              that a record names
NoStrand (liveness)           under fair ticks, an ended or expired permit on a pull request
                              that still needs a fix is followed by a cold dispatch, a named
                              worker gate or the head-cap escalation on the first decision
                              tick at or after cutoff_at (unless the head has moved)

Fair ticks: Tick (a tick completes only when its decisions are made), EndPermit and Decide
are weakly fair; no host or network step is assumed fair, so a lost message or a paused host
never helps a property hold.

Action table: model action, node transition
--------------------------------------------
GrantLease                      node_lander_lease_compute: grant, on a ready record
IssuePermit                     node_lander_lease_compute: permit, through the worker gate; stamps issued_at
SendOffer                       node_lander_host_effect: offer
SendCancel                      node_lander_host_effect: cancel
ReadStatus, RecvStatus          node_lander_host_effect: status (typed answer, may be late or lost)
EndPermit                       node_lander_lease_compute: the permit's probe goes dead (terminal or cutoff_at)
ObserveHead, Merge              the watcher feed and the merge of the verified head
SendRelease                     node_lander_lease_compute: release
ColdDispatch                    node_landing_worker_dispatch_effect: the cold worker
WorkerGate, HeadCapEscalate     node_landing_worker_gate_compute: a named gate or the head-cap escalation
ColdPushLands, ColdPushRefused  the cold worker's push, with its lease on the head it started from
OfferNew, OfferCancelled,       node_lander_fix_orchestrator: the permit record by create-without-
OfferRepeat                     overwrite; a repeated offer finds the record; a tombstone declines
HostCancel                      node_lander_fix_orchestrator: the cancel tombstone
HostStart, HostDecline          node_lander_fix_orchestrator: START (lock without waiting, tree kept, head)
HostAbort                       the tombstone checks before START, before the engine, before the push
HostEngine, HostEngineCommits,  node_lander_engine_effect: the engine child, commits only
HostEngineNoChange
HostWritePushing, HostPush      node_fenced_push_effect: pushing{expected_head,new_head}, then the push
                                with --force-with-lease on expected_head
HostDone                        node_lander_fix_orchestrator: done(pushed | failed)
JobDies                         the fault: the job process ends; the operating system drops its lock
HostStatus                      node_lander_tree_effect: status finishes a dead job (fail closed)
ReleaseLock, ReleaseCloseout,   node_lander_tree_effect: release (node_tree_lock_effect acquire, the
ReleaseRecord                   closeout under the grant, the record removed last)
RemoverRuns                     node_lane_closeout_effect and every other remover or writer (item 6)

Mutation matrix
---------------
Mutant  Knob (config)                       Mutation                                   Violates
M1      PermitLease = FALSE                 drop the lease from the push               NoLostUpdate
M2      ColdGuard = "head_only"             a cold dispatch during a live permit       AtMostOneAuthorizedPublisher
M3      OfferRule = "respawn"               respawn the engine on a repeated offer     ExactlyOnceChild
M4      RemoverRule = "ignore_record"       a remover deletes a tree a record names    SingleOwnerOfTree
M4b     LockPlace = "inside"                the lock file lives in the tree directory  SingleOwnerOfTree
M5      FallbackAfterCutoff = FALSE         suppress the cold fallback after cutoff_at NoStrand

Counterexample shapes (results/M*.out):
M1  grant, permit, cutoff passes, cold dispatch, the offer is late and the host starts at the
    old head, the cold push lands, then the unleased permit push overwrites it.
M2  grant, permit, cold dispatch at once: a second publisher inside the live permit.
M3  one engine run ends with no change, the offer is repeated and the job starts again.
M4  a remover takes the free lock and deletes the tree while its record is ready.
M4b the release job's closeout deletes the tree directory and its lock file with it, so the
    release job is mutating with no lock; the lock file must live outside every path a tree
    removal deletes.
M5  the permit lives to cutoff_at unobserved, the permit ends, and the pull request that
    still needs a fix is never decided.

Verdict
-------
TLC receipt: formal/lander_lease/results/SUMMARY.txt (outputs in formal/lander_lease/results/,
run on h201 at branch omn-22186-lander-lease-model, head b226b60ce55f430cd5955f9856b5fdb204c22eba;
lab-run ids lw-9a412d53b9ae4893, lw-66487e8a35b243d1, lw-860041ad27654c5b).

Test first: base.cfg on the draft ends in a TLC violation of AtMostOneAuthorizedPublisher,
exit=12, at state 4 (grant, permit, cold dispatch); the run is results/draft_base.out
(lab-run id lw-785d06e6dd20464e).
Base: TLC exits 0 on base.cfg, "No error has been found", 6530359 states generated, 1006785
distinct, depth 36, the four safety properties checked as invariants and NoStrand as the
temporal property (results/base.out).
M1  violates NoLostUpdate                    exit=12  results/M1.out
M2  violates AtMostOneAuthorizedPublisher    exit=12  results/M2.out
M3  violates ExactlyOnceChild                exit=12  results/M3.out
M4  violates SingleOwnerOfTree               exit=12  results/M4.out
M4b violates SingleOwnerOfTree               exit=12  results/M4b.out
M5  violates NoStrand                        exit=13  results/M5.out

Limits
------
The model has one author and no independent reviewer. It checks one pull request, one tree
and one permit; Horizon is 4 ticks and HoldS is 2. Left out: the lane unit and the engine
scope drain and the fenced tree state, the WIP push, the amended-head refusal (every commit
descends from the base), the controller's budget and ladder, and any write by an engine
orphaned by a dead job (status and the release job stop its scope first; the node fixtures
of the host-side task cover that). The model does not prove the lock file's real location,
the push flags or the sandbox; it fixes the rules the build must satisfy: the permit is a
lease on the pull request, the push is fenced by the head it started from, a cold dispatch
waits for the permit to end, a repeated offer finds the record, every remover takes the tree
lock and skips a recorded tree, and the lock file lives outside the tree.
