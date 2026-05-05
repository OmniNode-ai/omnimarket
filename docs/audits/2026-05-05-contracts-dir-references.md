# Cross-Repo Audit: References to `omnimarket/contracts/`

## Scope

Walk every repo under `$OMNI_HOME` and grep for two patterns:

1. The literal string `omnimarket/contracts/`.
2. The shape `contracts/OMN-[0-9]+` — catches scripts that `cd` into a repo
   first and reference relative paths.

Output: `docs/audits/2026-05-05-contracts-dir-references.csv`.

## Result classification

| Category                                                                 | Count |
|--------------------------------------------------------------------------|------:|
| References inside a repo's *own* `contracts/OMN-*.yaml` work-tracking files (other repos: omnibase_compat, omnibase_infra, etc.) — those are self-references in their own dod_evidence; out of scope for this PR | ~600 |
| Self-references inside the moved omnimarket work-tracking YAMLs (each YAML names its own filename in dod_evidence commands) | 31 |
| `tests/unit/nodes/node_dod_verify/test_durable_evidence_gate.py` — uses `contracts/OMN-9855.yaml` as a deliberate **fake** `contract_rel_path` argument; the gate's `load_contract_on_ref` is stubbed and never reads disk; refers to OCC's `<occ>/contracts/`, not omnimarket's | 9 |
| `src/omnimarket/nodes/node_dod_verify/services/durable_evidence_gate.py` docstring example — references the OCC contract path shape, not omnimarket's | 1 |

**Conclusion:** zero live runtime consumers of `omnimarket/contracts/`. The
84 `OMN-XXXXX.yaml` files are pure dod_evidence work-tracking artifacts.
The move is safe: nothing breaks at the source-tree level. The
`durable_evidence_gate` consumers refer to *OCC* contract paths
(`<onex_change_control>/contracts/OMN-XXXX.yaml`), which are unrelated.

## Files modified during the move

The move scrubbed absolute user and volume paths from work-tracking evidence
commands by replacing them with `${OMNI_HOME}/...` placeholders.

The affected files are work-tracking artifacts. Their dod_evidence commands
are now portable.

## Acceptance

- `omnimarket/contracts/` directory removed.
- 84 `OMN-XXXXX.yaml` files at `omnimarket/docs/work-tracking/contracts/`.
- `tests/unit/structure/test_contracts_dir_runtime_only.py` enforces the
  invariant: legacy path empty, new path populated, no `/Users/`,
  `/Volumes/` literals remain.
- Audit script + CSV checked in for future re-verification.
