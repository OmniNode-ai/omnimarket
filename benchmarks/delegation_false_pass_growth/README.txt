Delegation gate false-pass growth scripts (OMN-19793, EV.4 follow-up)

Lab one-shot scripts, kept as run, that grew the EV.4 labelled set to 115 accepted items in each of nine task classes: candidate query, manifest draw, top-up generation, blind rater batches, label merge, in-memory readout with the EV.4 run handler, third-rater sample and agreement. They read the lab database read-only and write nothing to it. Content (prompts, answers, rater reasons) never leaves the lab hosts.

The data they produced (manifest, labels, readouts, agreement) and the write-up live in the private planning repository under "reports/delegation-evals/2026-09-30-false-pass-100/" and "reports/2026-09-30-delegation-gate-false-pass-first-run.md".

"readout.py" needs the run handler of omnimarket#3127 and is a positive control of it: over the first run's 270 items and labels it returns eval run id "735b18b8-734e-5c7d-bc00-47926ac70963".

"gen_runner_persisted_path.py" is the first form of the generation runner; its routing guard did not bind on the Kafka bus. "gen_runner_inmemory.py" is the local-only form.
