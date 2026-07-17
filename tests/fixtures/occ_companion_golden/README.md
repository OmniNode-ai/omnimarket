# OCC companion golden fixtures (OMN-14710)

Vendored, self-contained fixtures for the OCC-emitter golden-fixture regression
suite (`tests/unit/nodes/node_occ_companion_compute/test_occ_emitter_golden_fixtures_omn_14710.py`).

They are copied into this repo so the tests are portable — CI checks out only
`omnimarket`, so the tests must not read files from sibling repos or the
workspace root.

## Files

- `occ_4284_companion.diff` — the full PR diff (git transport) of
  `OmniNode-ai/onex_change_control#4284`, a machine-generated OCC companion the
  producer emitted **with real generator defects** (hardcoded integer PR numbers
  in `contracts/OMN-14695.yaml` `check_value`s, existence-only L0 checks). Used
  as the NEGATIVE fixture for F-02 (hardcoded PR ints) and F-15 (existence-only
  substance). Provenance: `docs/evidence/2026-07-17-occ-4284-failed-generator-fixture/`
  in the workspace (omni_home).
- `occ_4284_pr-meta.json` — the captured PR number/title/mergeable state
  (`[WS4 ...]`-style title, `CONFLICTING`/`DIRTY`).
- `occ.yamlfmt` — a byte-copy of `onex_change_control/.yamlfmt` (the yamlfmt
  config the hosted OCC pre-commit runs). The F-03 formatter-clean test runs
  `yamlfmt` with THIS config, because a companion is only formatter-clean under
  the config of the repo it lands in.

Keep these in sync only if the upstream sources are re-captured; they are frozen
negative evidence otherwise.
