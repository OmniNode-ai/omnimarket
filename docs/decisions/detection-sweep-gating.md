# Detection-sweep gating decisions (omnimarket)

A detection sweep that is not a pre-merge gate is advisory and gets ignored.
This note records, per high-value sweep, whether it is wired as a required
pre-merge gate or kept on-demand, with the rationale for each.

## Enforcement model

omnimarket uses a single required aggregate rollup context, `CI Summary`, whose
`needs` set includes every required sub-job and whose failure loop checks each
sub-job result, so that ANY failed required sub-job turns the rollup red. Gating
a sweep means adding its job to that rollup; it does not change the rollup model.

## Decisions

| Sweep | Decision | Mechanism / rationale |
|-------|----------|-----------------------|
| aislop sweep | **GATE** | An `aislop-sweep` job runs the canonical `check_ai_slop.py --strict` scanner over the **PR diff** (changed `.py`/`.md` vs base) and feeds the `CI Summary` rollup `needs` + failure loop. Diff-scoped (not whole-tree) because the tree carries pre-existing aislop debt — a whole-tree hard gate would block every PR on that backlog. New slop blocks; existing debt does not. This matches the diff-scoped strict gate used in the sibling Python repos. |
| golden chain sweep | **GATE** (already) | Already gated via the `golden-chains` job (Golden Chain Suite, in-memory bus), a member of the `CI Summary` rollup `needs`. No change required. |
| compliance sweep | **KEEP ON-DEMAND** | The compliance sweep node scans the **whole tree** and exits non-zero on any finding; the current tree is not clean, so a whole-tree hard gate would be red on every PR. The node CLI has no PR-diff mode, so the diff-scoped model used for the aislop gate is not yet available. Promote to a gate after the handler-compliance debt is burned down or a diff-scoped CLI mode is added. |
| contract sweep | **KEEP ON-DEMAND** | The contract sweep node scans the whole tree and exits non-zero on any drift; the current tree is not clean. Same constraint as the compliance sweep: no diff-scoped CLI mode. Note that contract-YAML validation for the change-control contracts a PR touches is already gated via the separate `contract-compliance` rollup job. |
| duplication sweep | **KEEP ON-DEMAND** | The duplication sweep is **cross-repo by design** — it detects duplicate table / topic / migration / model definitions ACROSS every repo. A single-repo PR gate cannot see cross-repo duplicates (sibling repos are not checked out in a per-repo CI run), so gating it on one repo's PR would be meaningless. It belongs as a scheduled cross-repo sweep, not a per-PR gate. |

## Planted-violation proof

`check_ai_slop.py --strict` over a file containing a sycophantic docstring opener
and reST-style docstring markers exits non-zero, which fails the `aislop-sweep`
job, which fails the `CI Summary` rollup. This proves the gate has teeth: a new
AI-slop violation introduced in a changed file blocks merge.
