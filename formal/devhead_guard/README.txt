# DevheadGuard: TLA+ model of the dev-head guard reducer (OMN-19934, plan task B3)

Model-before-build for `node_devhead_guard_reducer` (B4) and the open-revert verb (B5). Both cite this
directory by path. Design source: the verification-before-and-after-merge plan, task B3.

## What is modelled

One red episode for one (repository, branch): `RED_DETECTED`, `BISECTING`, `CULPRIT_CONFIRMED`,
`REVERT_OPEN`, ending in `GREEN` or `ESCALATED`. The world has merges 1..N, a ground truth (the first
breaking merge, or `none` for a flaky red), one runner slot, probe crashes, redelivered revert commands,
a human fix PR racing the revert, a post-landing check, and a deadline.

## Properties

| Id | Invariant | Mutation (one knob) | Config |
| -- | -- | -- | -- |
| P1 | `AtMostOneRevertPerCulprit` | `IdemKey=FALSE` | `M1_p1_no_idempotency` |
| P2 | `RevertOnlyForBrokenMerge`, `RevertTargetsTruth`: a revert PR exists only for a merge recorded failing at itself and passing at its parent | `ConfirmRule="self_only"` | `M2_p2_self_only` |
| P3 | `NoFixAndRevertBothLandWithoutCheck` | `GateOn=FALSE` | `M3_p3_no_gate` |
| P4 | `SlotReleasedOnExit` (and `SlotEventuallyReleased`, temporal) | `ReleaseOnCrash=FALSE` | `M4_p4_no_release_on_crash` |
| P5 | `RedReachesGreenOrEscalated`: at the bound the episode is terminal | `DeadlineOn=FALSE` | `M5_p5_no_deadline` |

The design configs `DevheadGuard_design` (truthful probes) and `DevheadGuard_design_flaky` (probes may
return either result) check every property and must pass. Each mutant cfg checks only the property it
targets and must produce a counterexample.

## Modelling notes

- The deadline and the boundary tick are one atomic transition (an urgent timer). Otherwise an open
  state visible at `now = Bound` would violate P5 before a separately scheduled deadline could run.
- After the crash budget is exhausted a crash may abandon the bisect and escalate. That exit is what
  lets a crash-only mutation reach P4 at all.
- No fairness is assumed for runner responses, human fixes, landings or checks. Tick and deadline are
  fair for the temporal slot property.
- Queue scheduling is abstracted to a nondeterministic landing.

## Run

`./run_all.sh` runs the matrix and writes `results/SUMMARY.txt`, which records the model's content
digest and each cfg's digest beside its result. `./run_tlc.sh <cfg>` runs one. The first run fetches the
pinned `tla2tools.jar` (TLC 2.19, release v1.7.4, sha256 checked, not committed) and needs a Java
runtime. `tests/formal/test_devhead_guard_model.py` fails when the model or a cfg changes without a
re-run, or when a recorded result is not the expected one.

Raw checker output for every run is in `results/`.
