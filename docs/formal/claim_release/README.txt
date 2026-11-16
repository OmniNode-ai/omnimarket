ClaimRelease: who may delete a PR claim when its lane ends
==========================================================

Files
-----
ClaimRelease.tla    the model; its header says what it models, the properties and the knobs
Model.cfg           the design: four safety invariants (and TypeOK) and one liveness property
mut_*.cfg           one mutation per property; each flips ONE knob of Model.cfg to FALSE and
                    checks the one property it must violate (LanderLease style)
wit_*.cfg           reachability witnesses: each checks an invariant that says a state is
                    unreachable and must violate it, which shows the state is reachable
results/            one TLC output per config (the last line is exit=<code>), SUMMARY.txt with
                    the sha256 of the model, each config and each output, and
                    lab_reading_h053.txt (the model read against one lab host)
VERDICT.txt         the design-review verdict
tests/formal/test_claim_release_model.py reads SUMMARY.txt and fails when the model or a
                    config changed without a re-run, when a mutation flips more than one knob
                    or violates another property, and when the verdict or the lab reading is
                    missing

Run
---
TLC 2.19 (tla2tools.jar v1.7.4, sha256 936a2620...50e88), on lab host h053, inside the
eclipse-temurin:17-jre-alpine image, one process per config, from this directory:

  java -XX:+UseParallelGC -Xmx2g -cp tla2tools.jar tlc2.TLC -config <cfg> -workers 2 \
       -metadir /tmp/states -cleanup ClaimRelease.tla

TLC exits 0 when it finds no error, 12 on an invariant violation and 13 on a temporal one.
There is no runner script (the plan adds none): the same command is the receipt.

What is modelled
----------------
A PR claim is one JSON file per PR under the claims directory, naming its holder by an exact
identity: lane_id, claimed_by_run and claimed_by_session together (an "instance"). Instances
i1 and i2 carry the same lane name: i2 is the replacement launched for i1. The model has
three writers of one claim file:
  the lane      Start, Acquire, Heartbeat, SelfRelease (operation release by the holder), End
  the closeout  Observe (final receipt or gone process tree), Inspect (open, fstat, read: the
                snapshot) and Delete (rename to a tomb, compare inode and bytes, restore a
                replacement), for the exact identity of the ended instance only
  the reaper    Age (the heartbeat lapses) and Expire (acquire's reap of an inactive claim)

Properties
----------
NeverReleaseLiveHolder      a release by a writer other than the holder (closeout or expiry)
                            never takes the claim of a holder whose lane is live
ReleasedOnlyAfterExitSeen   a closeout release carries the observed exit of its holder
ClosesOwnOnly               a closeout deletes only the exact identity it closes; a
                            replacement that reuses the lane name is not that identity
DeletesWhatItInspected      a closeout deletes the file it inspected; a claim that replaced
                            it between inspection and delete is preserved
NoStrandedClaim (liveness)  a claim whose holder ended, with its exit observed and the
                            closeout running, is eventually released

Results (TLC on h053, all in results/)
--------------------------------------
Model.cfg                              No error has been found     exit=0
mut_no_exit_check          RequireExit=FALSE         violates NeverReleaseLiveHolder      exit=12
mut_no_exit_evidence       RequireExit=FALSE         violates ReleasedOnlyAfterExitSeen   exit=12
mut_lane_name_match        ExactIdentity=FALSE       violates ClosesOwnOnly               exit=12
mut_delete_without_compare CompareBeforeDelete=FALSE violates DeletesWhatItInspected      exit=12
mut_no_live_heartbeat      LiveHeartbeats=FALSE      violates NeverReleaseLiveHolder      exit=12
mut_closeout_never_runs    CloseoutRuns=FALSE        violates NoStrandedClaim             exit=13
wit_closeout_releases / wit_expiry_reaps_ended / wit_replacement_holds_after_closeout
  each violate their NeverX invariant (exit=12): a closeout release, an expiry of an ended
  holder's claim, and a replacement that holds the PR after its predecessor's closeout are
  all reachable in the model.

The planted release of a live lane's claim is mut_no_exit_check: Start i1, Acquire p1,
Inspect, Delete with no exit observed; results/mut_no_exit_check.out shows the release record
with life = "live".

Mapping: the model against one lab host (h053)
----------------------------------------------
Read on h053 at 2026-11-11T13:56Z; the full listing is results/lab_reading_h053.txt.

 model                       on the host
 --------------------------- ----------------------------------------------------------------
 instance i                  (lane_id, claimed_by_run, claimed_by_session) of a claim file;
                             run equals session in 180 of 184 files; this id is NOT the runner's
                             rlane-<lane>-<id> run directory name
 life[i] = live              the lane's run directory (<lab-run root>/runs/rlane-<lane>-<id>.ctx)
                             with a run.pid whose /proc entry exists; the lane's ledger CLAIM
                             with no later TERMINAL
 life[i] = ended             the run directory removed or its pid gone; the lane's TERMINAL row
 claim[p] = i                the file <org>--<repo>--<n>.json with those three fields
 claim[p] = none             no file (a tomb file is not a claim)
 stale[p]                    is_active() false: heartbeat older than 30 minutes AND claim older
                             than 2 hours
 seen[i]                     a receipt with final true (or a status not in the running set) in
                             the stop node; engine_ended true on the closeout request
 Inspect / Delete            release_ended_own_claims: open, fstat, read, _exact_owner, then
                             _delete_snapshot (rename, inode and bytes compared, os.link back)
 ExactIdentity               _exact_owner: run, lane and session all equal
 SelfRelease / Heartbeat     operations release and heartbeat of the registry
 Expire                      acquire's reaping of an inactive claim; cleanup_stale_own_claims

Readings on h053's claim directory (184 files):
  held by a live lane on this host            0 of 184 (live lanes: c0-model-4220,
                                              deleg11-4190, h0-model-4210; none names a claim)
  claim whose lane has a TERMINAL on the ledger  184 of 184 (the lane ended, the claim stands)
  older than 2 hours                           182 of 184
  heartbeat ever written (last_heartbeat_at differs from claimed_at)   0 of 184
  selectable by release_ended_own_claims       167 of 184
  not selectable                               17 of 184: 13 whose file name is not
                                               filesystem_key(pr_key) (for example a pr_key with
                                               no org part), 4 with no claimed_by_session
So the old runner's non-release is measured: every claim on this host belongs to a lane that
ended, and the closeout of this design could release 167 of them by exact identity.

What is NOT measured (named, not guessed)
-----------------------------------------
- The lane receipts (status, final, session_id, claim_run_id) live on the launching host, not
  on h053. No file in the omni plugin, the runner or omnimarket/src writes claim_run_id
  into a receipt (grep: only the stop and closeout nodes and the tick decision read it), so
  the stop node's exact release has no producer on the receipt side in the code read here.
  Not read from a live receipt.
- The model has no clock: Age and Expire are abstract. Whether a lane that runs longer than 2
  hours without a heartbeat is reaped is read from the code (is_active), not run on the host.
- The closeout nodes' release was not run against h053's claim directory (dry run included):
  the claims there belong to other lanes' history and are not this lane's to touch.
