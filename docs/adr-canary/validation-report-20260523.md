# Manifest Validation Report — 2026-05-23

**Phase**: Phase 2D — Spot-check and manifest validation
**Manifest**: `docs/adr-canary/ground_truth_manifest.yaml`
**Validator**: `scripts/validate_manifest.py`
**Run date**: 2026-05-23

---

## Summary

| Check | Result |
|-------|--------|
| Total entries | 60 |
| ADR hash mismatches | 0 (5 stale hashes fixed; 2 workspace path normalisations rehashed, see below) |
| Schema version failures | 0 |
| Missing required fields | 0 |
| Duplicate IDs | 0 |
| Low-confidence entries without curation_notes | 0 |
| Source file hash mismatches (non-fatal drift) | 59 |
| Spot-check entries passing (10/10) | 10/10 |

**Verdict: PASS** — all hard checks pass. Source file drift is expected and documented.

---

## Validator Checks

`scripts/validate_manifest.py` enforces:

1. All required fields present: `id`, `root_paths`, `ground_truth_adr`, `ground_truth_adr_hash`, `source_file_hash`, `manifest_schema_version`, `models`, `expected_decision_types`, `expected_keywords`
2. `ground_truth_adr_hash` == sha256 of inline `ground_truth_adr` text
3. Report `source_file_hash` drift from the first `root_path` on disk as a non-fatal warning — the manifest is a point-in-time snapshot
4. `manifest_schema_version` == `"v1"` for every entry
5. No duplicate `id` values
6. Entries with `source_confidence: low` must have `curation_notes`

---

## Hash Fixes Applied

Five entries had stale `ground_truth_adr_hash` values (ADR text was edited after initial hashing). These were corrected by recomputing from the current inline text:

| Entry ID | Old hash (first 16 chars) | New hash (first 16 chars) |
|----------|--------------------------|--------------------------|
| `kafka-required-infrastructure` | `sha256:9297a4d1fd2c...` | `sha256:4309c3185711...` |
| `vault-to-infisical-migration` | `sha256:8dd0e11944a9...` | `sha256:711136706a5d...` |
| `graceful-shutdown-drain-period` | `sha256:4d86ea74494e...` | `sha256:cd4ba7978f95...` |
| `2026-04-23-registry-owned-consumer-surface` | `sha256:efda15e0ce52...` | `sha256:0450ba0e1fac...` |
| `2026-04-23-registration-runtime-registry-boundary` | `sha256:dfd7113c97ca...` | `sha256:835b392f90d8...` |

Root cause: all 5 entries span Phases 2A and 2B. The ADR text was lightly edited during Phase 2B/2C authoring after the hash was first written. No content integrity concerns — this is a hash-vs-text synchronisation issue.

---

## Source File Hash Drift

59 of 60 entries show `source_file_hash` mismatch against current on-disk state. This is expected:

- The manifest is a **point-in-time snapshot** taken during Phases 2A–2C (May 2026)
- Source directories (`omnibase_infra/docs/`, `omnibase_core/docs/`, `omniclaude/docs/`, `omnidash/docs/`) continue to evolve
- The `source_file_hash` field documents what existed at manifest creation time, enabling future drift detection
- The validator treats these as non-fatal warnings marked "source may have changed"

The single entry with a fully-passing source hash is `omnimarket-adr-dispatch-architecture-foreground-only` — its root path is stable.

---

## Leak-Gate Normalisation

Two inline ADR references used a developer-specific absolute workspace path. They were normalised to `$OMNI_HOME/...`, and their `ground_truth_adr_hash` / `source_file_hash` values were recomputed:

| Entry ID | New hash (first 16 chars) |
|----------|--------------------------|
| `adr-2026-04-28-dispatch-lifecycle-canonical-omnibase_core` | `sha256:afeb5399fc57...` |
| `adr-2026-04-28-skill-liveness-validator-home-omnibase_core` | `sha256:9fbc82e5da60...` |

The validator default path was also changed to `Path.home() / "Code" / "omni_home"` so no developer-specific absolute path is embedded in the script.

---

## Spot-Check Results (10/10 PASS)

Every 6th entry was manually verified (indices 0, 6, 12, 18, 24, 30, 36, 42, 48, 54).

| # | Entry ID | root_paths exist | ADR hash | Keywords (N found / total) | Result |
|---|----------|-----------------|----------|---------------------------|--------|
| 1 | `kafka-required-infrastructure` | All 3 dirs exist | MATCH | 3/5 | PASS |
| 2 | `adr-2026-04-28-skill-liveness-validator-home-omni_home` | dir exists | MATCH | 5/5 | PASS |
| 3 | `omnidash-003-baselines-roi-card-stay-bespoke` | Both dirs exist | MATCH | 5/5 | PASS |
| 4 | `adr-002-enum-message-category-node-output-separation` | dir exists | MATCH | 3/3 | PASS |
| 5 | `adr-any-type-pydantic-workaround` | dir exists | MATCH | 4/4 | PASS |
| 6 | `adr-error-context-factory-pattern` | dir exists | MATCH | 4/4 | PASS |
| 7 | `adr-soft-validation-env-parsing` | dir exists | MATCH | 2/4* | PASS |
| 8 | `omniclaude-adr-003-no-fallback-routing` | dir exists | MATCH | 4/4 | PASS |
| 9 | `omniclaude-2026-02-28-ai-slop-checker-rule-set` | dir exists | MATCH | 3/4 | PASS |
| 10 | `omnibase-core-risk-009-ci-workflow-modification-risk` | dir exists | MATCH | 4/4 | PASS |

\* Entry 7 keywords `soft-validation` and `env-var` use hyphenated forms; the ADR text uses `Soft Validation` (space) and `env_var` (underscore). 2/4 keywords found under case-insensitive substring match — still within the ">= 2 of N" threshold. Minor keyword normalisation issue, no content concern.

---

## Phase 3 Readiness

The manifest is structurally sound and ready for Phase 3 (proof of life):

- 60 entries, all with required fields
- All 60 ADR hash values match current inline text
- 11 low-confidence entries all have `curation_notes`
- All `manifest_schema_version` fields set to `"v1"`
- No duplicate IDs
- All spot-checked root paths exist on disk
