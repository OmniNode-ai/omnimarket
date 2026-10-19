# CompactionLifecycle: TLA+ model of the compaction lifecycle (OMN-21104, plan task TA.1)

Model-before-build for the compaction orchestrator (TA.11) and install confirmation (TA.13). Both
cite this directory by path. Design source: the context service plan, section 6 (identities, the
compaction transaction, the boundary algorithm, facts never merged, staleness, topics and ordering)
and task TA.1.

## What is modelled

One session. The transcript is a sequence of records, each a turn or a compaction boundary that
carries an activation line (an activation id, or none for native compaction). A candidate is named
by its version vector: the transcript length it was built on and the version of the other sources
(ledger rows, checkpoint), which can change while a trigger holds the turn. An activation is named
by the invocation that minted it.

Actors:

- Triggers: `precompute` does not hold the turn; `manual` and `auto` do. Concurrent invocations on
  the same inputs build the same candidate.
- Orchestrator: waits for the session barrier (every input record captured gap-free and untruncated),
  builds and publishes `prepared` (with an assembly hash; a second hash value is a determinism bug
  the fold must alarm), mints the activation and publishes `activated` (carrying the session
  high-water of its request, the barrier the adapter journalled), then sends the package. It reads
  nothing of the harness transcript beyond that barrier. Any step before the send may fail: `fallback`
  is published and the edge gets `native`.
- Edge: returns `install` for a sent package, or misses its deadline (`native`) while the
  orchestrator still completes. A precompute's installed package is kept by the harness.
- Harness: writes the boundary for the trigger holding the turn (its own activation, a kept
  precompute that passes revalidation, or native). It may write a list other than the one returned
  (the observed hash then differs: `install_mismatch`). A boundary drops every kept precompute.
- Capture: journals each record late, in any order, or drops it (bounded), and may record a
  boundary truncated. A duplicate capture is the same record and changes nothing.
- Install confirmation: derives facts from captured records alone. A captured boundary's own
  activation line gives `installed`, `install_mismatch`, or `uncertain` (truncated). Every other
  activation is judged by the first boundary after its request's high-water, read only through a
  gap-free captured run. That boundary is the only one that can install it: a boundary drops every
  kept precompute, and revalidation refuses a precompute built on older inputs. In it, none in the line gives `not_installed(native_compaction)`, another activation gives
  `not_installed(not_selected)`. A captured item after the activation with no such readable
  boundary gives `uncertain(capture_gap)`.
- Fold: receipt events arrive in any order and any number of times. Per activation the state is the
  join over `activated < uncertain < not_selected < native < {installed, install_mismatch} <
  conflict`, where `installed` with `install_mismatch` is `conflict`. Per candidate it is the
  assembly hash seen, or `conflict` for two. Per invocation it is `fallback` when its own fallback
  arrived.

## Properties

| Id | Property | Mutation (one knob) | Config |
| -- | -- | -- | -- |
| P1 | `NoInstallWithoutActivation`: no boundary installs an activation whose `activated` was never published | `ActivateFirst=FALSE` (the package may leave first and the orchestrator die before `activated`) | `M1_send_before_activated` |
| P2 | `OneStatePerIdentityUnderAnyArrivalOrder`: per activation, candidate and invocation, the fold's state equals the fold of the set of events received | `FoldMode="overwrite"` (last arrival wins) | `M2_last_arrival_wins` |
| P3 | `StaleCandidateNeverInstalled`: for every trigger, a boundary installs only a candidate whose whole version vector equals the one its trigger fixed, and that replaces the records the boundary follows | `RevalidateOn=FALSE` | `M3_no_revalidation` |
| P4 | `FallbackNeverConflictsWithAnotherInvocation`: the fold records `fallback` only for the invocation that fell back | `FallbackKey="candidate"` | `M4_fallback_by_candidate` |
| P5 | `NoCompleteBeforeInstalled`: nothing is reported complete without an `installed` fact | `CompleteOn="sent"` (complete when the orchestrator sends) | `M5_complete_on_send` |
| P6 | `EveryActivationResolves` (temporal): an activation whose captured stream advances past its request's session high-water reaches installed, install_mismatch, not_installed or uncertain | `GapRule=FALSE` | `M6_no_gap_rule` |
| P7 | `NoBuildOverIncompleteSources`: no candidate is built before its inputs are captured gap-free | `BarrierOn=FALSE` | `M7_no_barrier` |

`CompactionLifecycle_design` checks `TypeOK` and P1 to P5 and P7 over every trigger and must pass.
`CompactionLifecycle_design_live` checks P6 and must pass; it differs only in exploration bounds that
do not reach resolution (no auto trigger, which is the manual transaction; no source-version change;
one assembly hash), to keep the temporal check small. Each mutant config checks only the property it
targets and must produce a counterexample. Bounds: two invocations, transcript length 2, one capture
drop.

## Findings for the build tasks

- TA.13, the capture-gap rule. The plan's rule (a later captured item with no compact record for a
  boundary after the activation resolves it to `uncertain(capture_gap)`) also fires for a precompute
  whose session moved on by a turn before any boundary. The model keeps that rule, since a turn is no
  evidence of `not_selected`; a later readable boundary then raises the state to `not_selected` or
  `installed` by precedence. TA.13 should alarm on `capture_gap` only when the captured sequence
  shows a gap, and report the precompute case as pending evidence.
- TA.11 and TA.13, ordering an activation against the transcript. The receipt topics and the
  transcript are different streams with no comparable offsets, so the activated event must carry
  the session high-water of its request (the barrier) and install confirmation must judge it by the
  first boundary after that high-water, never by publication time. The edge that missed its
  deadline while the orchestrator completed is then judged by the native boundary that followed.
- TA.1's "version vector changed", reconciled. The version vector a trigger fixes is the one its
  request carried (for a trigger that builds its own candidate, read under the barrier) or the one
  current at revalidation (for a kept precompute, P7c). A ledger row or checkpoint that lands while
  a manual or auto trigger's build runs is newer than that request and belongs to the next
  compaction; the model lets such a change happen and P3 compares against the trigger's vector, so
  for a trigger's own candidate P3 holds by construction and its content is the kept-precompute
  case, which M3 shows fails without revalidation. A design that must reject such a mid-build
  change would revalidate at activation for every trigger; the plan does not ask for it.
- TA.11, the version vector. A boundary changes the inputs, so it changes the version vector, and
  revalidation of a kept precompute must compare both the messages and the other sources.
- Activation `conflict` is unreachable in the design: one boundary yields one fact. It stays in the
  fold as the alarm for two contradictory facts.

## Out of scope

The record-level boundary algorithm (parent links, preserved messages, trailer extraction and the
observed hash over replacement records) is TA.13's and is abstracted here to one boundary record
with its activation line and whether its observed hash matches. The current-context projection
(newest boundary and retirement) is not modelled. Model outputs and `final_pack_hash` are not
modelled; `assembly_hash` is.

## Run

`tests/formal/compaction_lifecycle_tlc.sh` runs the matrix and writes `results/SUMMARY.txt` with the
sha256 of the model, each config and each config's output. It fetches the pinned `tla2tools.jar`
(TLC 2.19, release v1.7.4, sha256 checked, not committed) and runs Java from `JAVA`, `PATH`, or the
`eclipse-temurin:17-jre-alpine` image. `tests/formal/test_compaction_lifecycle_model.py` fails when
the model or a config changes without a re-run, when a recorded result is not the expected one, or
when `VERDICT.txt` does not approve the model's digest.
