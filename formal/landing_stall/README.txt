LandingStall: the repository-wide stall rule (OMN-21880)
========================================================

Files
-----
LandingStall.tla        the model; its header says what it models and what it leaves out
LandingStall_design.cfg   safety: every invariant, two contexts, one calm
LandingStall_design_live.cfg   liveness: both temporal properties, one context
M1 .. M18               one mutant per design knob; each must violate one property
W1 .. W3                witnesses: the named state is reachable
results/                one TLC output per config and SUMMARY.txt with the sha256 of
                        the model, each config and each output
VERDICT.txt             the design verdict

Run
---
tests/formal/landing_stall_tlc.py runs the whole matrix and rewrites results/. TLC needs
Java 17: JAVA, then java on PATH, then the eclipse-temurin:17-jre-alpine image through
docker. tests/formal/test_landing_stall_model.py fails when the model or a config changes
without a re-run.

The rule as modelled
--------------------
A stall opens when one required context is red on at least Pct percent of at least MinPrs
open pull requests. At ExcAfter ticks one exception row, one cause lane, and no further
repair workers at that context. At QAfter ticks, with no eligible repair, one question.
The stall closes after ClearReads consecutive clear snapshots and lifts the block.
A tick is 30 minutes: 2 and 4 ticks are the ruling's 60 and 120 minutes.

Where the model lives
---------------------
This repository's formal/ directory, beside andon_stop and ci_red_triage, because the
rule's only code is a node here. The omnibase_infra formal/ directory (generic_lease,
declared_state_reconcile) is the pattern, not the home.
