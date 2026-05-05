# Cross-Repo Audit: References to `omnimarket/contracts/`

**Ticket:** OMN-10552
**Plan:** docs/plans/2026-05-05-omnimarket-public-shippable.md (Task 6)

## Scope

Walk every repo under `$OMNI_HOME` and grep for two patterns:

1. The literal string `omnimarket/contracts/`.
2. The shape `contracts/OMN-[0-9]+` — catches scripts that `cd` into a repo
   first and reference relative paths.

Output: `docs/audits/2026-05-05-contracts-dir-references.csv` (641 rows on the
2026-05-05 baseline).

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

The plan flagged three files containing absolute user/volume paths inside
their dod_evidence commands. These were rewritten to `${OMNI_HOME}/...`
placeholders during the move:

| File (now under `docs/work-tracking/contracts/`) | Old literal               | New literal                       |
|--------------------------------------------------|---------------------------|------------------------------------|
| `OMN-10127.yaml` (×2 sites)                      | `/Users/jonah/Code/omni_home/omnibase_infra` | `${OMNI_HOME}/omnibase_infra` |
| `OMN-10166.yaml` (×1 site)                       | `/Volumes/PRO-G40/Code/omni_home/omnibase_infra` | `${OMNI_HOME}/omnibase_infra` |
| `OMN-10382.yaml` (×4 sites)                      | `/Users/jonah/Code/omni_home/omni_worktrees/OMN-10382/omnibase_core` | `${OMNI_HOME}/omni_worktrees/OMN-10382/omnibase_core` |

All three files are work-tracking artifacts (already merged in PRs that
shipped the actual changes). The dod_evidence commands are now portable.

## Acceptance

- `omnimarket/contracts/` directory removed.
- 84 `OMN-XXXXX.yaml` files at `omnimarket/docs/work-tracking/contracts/`.
- `tests/unit/structure/test_contracts_dir_runtime_only.py` enforces the
  invariant: legacy path empty, new path populated, no `/Users/`,
  `/Volumes/` literals remain.
- Audit script + CSV checked in for future re-verification.
