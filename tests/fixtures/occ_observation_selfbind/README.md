# `occ_observation_selfbind` fixture

A trimmed but **verbatim** slice of `OmniNode-ai/onex_change_control@dev`
(commit `5c8188b9337737a2f3a6b50474458bb1bddd5869`, read 2026-07-28), carrying
the two directories `occ-preflight / eligibility` reads:

```
contracts/OMN-14888.yaml
drift/dod_receipts/OMN-14888/dod-OmniNode-ai-omnimarket-pr-1850-ci/command.yaml
drift/dod_receipts/OMN-14888/occ-observation-pr-5059/command.yaml
drift/dod_receipts/OMN-14888/occ-observation-pr-5059/command.supersede.0001.yaml
```

The contract keeps its real header (`ticket_id` + `schema_version` are the only
fields folded into `compute_contract_entry_sha256`, so the retained entries hash
exactly as they do on OCC) and exactly the two `dod_evidence` entries whose
receipts are copied. Nothing was rewritten: every retained byte is the byte that
is on `dev`, which is what makes a green run here evidence about the real gate
rather than about a hand-tuned mock.

Why a committed fixture rather than a live checkout: an OCC clone is not
available to CI, and a test that skips when a path is missing is a test that
does not exist. The trade-off is drift — if OCC's receipt/contract schema
changes, refresh this fixture from `dev` rather than editing it in place.

Used by `tests/unit/nodes/node_occ_observation_effect/` (see `conftest.py`).
