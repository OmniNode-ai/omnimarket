EvalTrackingSchedule: the weekly coding test's window trigger (OMN-24177, plan task T13)
=========================================================================================

Files
-----
EvalTrackingSchedule.tla   the model; its header says what it models and what it leaves out
base.cfg                   the design; checks TypeOK, StartedOnce, OneRunPerWindow, NeverEarly, NoMakeUp
M1 .. M6                   one mutant per design knob; each must violate the one invariant its config checks
wit_*.cfg                  witnesses: each checks a "not reached" invariant TLC must report violated
results/                   one TLC output per config (the last line is exit=<code>) and SUMMARY.txt with the
                           sha256 of the model, each config and each output
VERDICT.txt                the design verdict and the requirements the build answers to

Followed: formal/throughput_tick (README, base config, one config per mutant, witnesses, results/SUMMARY.txt,
VERDICT.txt, tests/formal/test_*_model.py).

Run
---
TLC 2.19 (tla2tools.jar v1.7.4, sha256 936a2620...50e88) with a JRE, or docker with eclipse-temurin:17-jre-alpine:
  java -XX:+UseParallelGC -Xmx2g -cp tla2tools.jar tlc2.TLC -config <cfg-basename>.cfg -workers 2 -metadir states/<cfg-basename> -cleanup EvalTrackingSchedule.tla > results/<cfg-basename>.out
TLC exits 0 when it finds no error and 12 on an invariant violation. Run from this directory.

Mutants and results (also in VERDICT.txt, the summary path is formal/eval_tracking_schedule/results/SUMMARY.txt)
-------------------------------------------------------------------------------------------------------------
base  PASS
M1_fresh_ids           violates OneRunPerWindow
M2_no_driver_noop      violates StartedOnce
M3_split_start_crash   violates StartedOnce
M4_round_up_window     violates NeverEarly
M5_make_up_missed      violates NoMakeUp
M6_shared_cadence      violates NeverEarly

Mapping to the code
-------------------
Tick(m)       HandlerEvalTrackingSchedule.handle: names the current window's run of the arm whose turn the
              tick is; the model lets any model be named on any tick (a superset of the rotation)
Deliver(id)   leg.run_leg and the reducer's _run: a ModelCodeEvalRunRequest for a run id whose row exists is dropped
Lose(id)      a send lost in transit or by a crash before the driver recorded it
Win/SendWin   eval_tracking_window.window_index: floor((now - epoch) / days), counted from the record's epoch
Ids           eval_tracking_window.tracking_run_id: uuid5 of record sha256, model, cadence days, window
