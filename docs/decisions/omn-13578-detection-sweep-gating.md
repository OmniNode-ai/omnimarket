# OMN-13578 — Detection-sweep gating decisions (omnimarket)

Operating Rule 5: a detection sweep that is not a pre-merge gate is advisory and
gets ignored. OMN-13578 audits omnimarket's high-value sweeps and decides, per
sweep, **gate** (wire as a required failing-rollup sub-job, per the Model B
enforcement established in OMN-13574) vs **keep on-demand** (with a recorded
rationale). Each "gate" decision below is proven by a planted-violation test and
by the sweep's membership in the required `CI Summary` rollup.

## Enforcement model

Model B (OMN-13574, Done): a single required rollup context per repo
(`CI Summary`) whose `needs` set includes every required sub-job, and whose
failure loop checks each sub-job result so that ANY failed required sub-job turns
the rollup red. omnimarket already uses this model
(`.github/workflows/ci.yml` → `ci-summary`). This ticket extends the rollup, it
does not change the model.

## Decisions

| Sweep | Decision | Mechanism / rationale |
|-------|----------|-----------------------|
| `aislop_sweep` | **GATE** (new) | Added the `aislop-sweep` job to `ci.yml`, wired into `ci-summary` `needs` + failure loop. Runs the canonical `check_ai_slop.py --strict` scanner over the **PR diff** (changed `.py`/`.md` vs base). Mirrors omnibase_core's `ai-slop-check` and omniclaude's `ai-slop-check (strict, PR diff)`. Diff-scoped because the omnimarket tree carries ~250 pre-existing aislop findings (180 ERROR / 5 CRITICAL as of 2026-06-25) — a whole-tree hard gate would block every PR on that backlog. New slop blocks; existing debt does not. |
| `golden_chain_sweep` | **GATE** (already) | Already gated via the `golden-chains` job (Golden Chain Suite, inmemory bus), a member of the `ci-summary` rollup `needs`. No change required. |
| `compliance_sweep` | **KEEP ON-DEMAND** | `node_compliance_sweep` scans the **whole tree** and exits non-zero on any finding. The current omnimarket tree has 165 imperative/hardcoded-topic findings across 2842 handlers, so a whole-tree hard gate is red today. There is no PR-diff mode in the node CLI, so a diff-scoped gate (the model used for aislop) is not yet available. Promote to a gate after the existing handler-compliance debt is burned down or a diff-scoped CLI mode is added (separate ticket). |
| `contract_sweep` | **KEEP ON-DEMAND** | `node_contract_sweep` scans the whole tree and exits non-zero on any drift. The current tree has 99 violations (33 major / 66 minor) across 1586 contracts, so a whole-tree hard gate is red today. Same constraint as `compliance_sweep`: no diff-scoped CLI mode. Note: contract **YAML** validation for the change-control contracts touched by a PR is already gated via the separate `contract-compliance` job in the rollup. |
| `duplication_sweep` | **KEEP ON-DEMAND** | `node_duplication_sweep` is **cross-repo by design** — it detects duplicate Drizzle tables / Kafka topic registrations / migration prefixes / Python model names ACROSS every repo under `$OMNI_HOME`. A single-repo PR gate cannot see cross-repo duplicates (the sibling repos are not checked out in a per-repo CI run), so gating it on an omnimarket PR would be meaningless. It belongs as a scheduled cross-repo sweep, not a per-PR gate. |

## omnibase_core aislop rollup membership (DoD confirmation)

OMN-13574 already moved omnibase_core's `aislop-patterns` job out of the
non-blocking `omni-standards-compliance.yml` and into `ci.yml`, where it is a
member of the Quality Gate rollup `needs` set and is checked in the rollup's
failure loop (`aislop="${{ needs.aislop-patterns.result }}"` →
`[[ "$aislop" == "success" ]]`). No further change is required in omnibase_core
for this ticket; the audit item is satisfied by OMN-13574.

## Planted-violation proof

`check_ai_slop.py --strict` over a file containing a sycophantic opener and
reST-style docstring markers exits non-zero (3 ERROR findings), which fails the
`aislop-sweep` job, which fails the `CI Summary` rollup. See the PR body for the
captured command output.
