# Leaked Literals Gate — Governance

Enforced by `scripts/validation/check_leaked_literals.sh` (blocking mode) against
this repo. Wired as pre-commit hook and required GHA status check
(`Leaked Literals Gate`) — both **omnimarket-only**; see the OMN-16156 note
below for the org-wide advisory rollout.

## What the gate blocks

Any committed file that contains:

- Private LAN IP prefixes, any `192.168.x` subnet (configure via `ONEX_HOST` or
  equivalent env var)
- Tailscale CGNAT addresses (`100.64.0.0/10`) and MagicDNS/tailnet hostnames
  (`*.tail<id>.ts.net`)
- Personal filesystem paths: `/Users/<user>`, generic `/home/<user>/...`,
  `/Volumes/<mount>` (any mount name)
- k8s internal service FQDNs (`*.svc.cluster.local`)
- The known-real external cluster IP (`18.209.126.195`, onex-dev)
- Operator attribution (`installed_by:<user>`)
- Private HuggingFace model identifiers: `cyankiwi/`, `Corianas/`, `mlx-community/`
- Personal git handles: `jonahgabriel`

### OMN-16156 (W0-GATE / G1) — org-wide advisory rollout

The topology classes above (LAN/CGNAT/MagicDNS/home-path/Volumes-mount/k8s
FQDN/external-IP/installed_by) generalize the catalog to the Tier-1 class from
`docs/plans/2026-08-17-public-docs-kb-consolidation-plan.md` §3. The gate
script itself stays resident only in omnimarket — it already resolves its scan
root via `git rev-parse --show-toplevel`, so it needs no copy to scan another
repo's tree. `scripts/validation/run_leaked_literals_org_wide.sh` runs it, in
**advisory mode only** (never blocking, never exits non-zero), against every
sibling repo clone under `$OMNI_HOME`, reporting findings without failing
anything or wiring any CI. Flipping other repos to *blocking* mode with their
own CI/pre-commit wiring is **G1-FULL** (post-beta) — not done by this rollout.

## Escape hatches

### Per-line annotation (source code)

Add the annotation on the **same line** as the literal:

```python
host = os.environ.get(
    "POSTGRES_HOST",
    "<onex-host>",  # onex-allow-internal-ip OMN-XXXXX reason="env-var fallback; override via POSTGRES_HOST"
)
```

Accepted annotation types:

| Annotation | Use case |
|---|---|
| `# onex-allow-internal-ip` | LAN IP in env-var fallback or config default |
| `# onex-allow-local-path` | Filesystem path in env-var fallback |
| `# onex-allow-model-id` | Private HuggingFace model ID in config default |
| `# onex-allow-raw-env` | Raw `os.environ` access that cannot use Settings |
| `# onex-allow-test-fixture` | Test fixture value not used as runtime default |

All annotations require `OMN-XXXXX reason="..."` suffix.

### File-level exemption (test fixtures)

For test files with many deliberate occurrences (e.g. synthetic actor names, pattern catalogs),
add this comment anywhere in the file (conventionally after the SPDX header):

```python
# onex-allow-file OMN-XXXXX reason="test fixture — <one-line explanation>"
```

This skips the entire file. Use sparingly — prefer per-line annotations for source files.

### Self-exempt files

The following files are unconditionally exempt (listed in `SELF_EXEMPT_FILES` in the script):

- `scripts/validation/check_leaked_literals.sh` — pattern catalog (self-referential)
- `.github/workflows/reject-leaked-literals.yml` — CI workflow referencing the gate
- `docs/leaked-literals-governance.md` — this document
- `docs/audits/2026-05-05-*.csv` / `docs/audits/2026-05-05-*.md` — generated audit reports
- `docs/tracking/delegation-cost-projection-lane.md` — tracking doc with config examples
- `docs/adr-canary/ground_truth_manifest.yaml` — embeds real merged ADRs' verbatim
  text as a benchmark fixture; editing embedded ADR text to annotate a literal would
  corrupt the ground-truth fidelity the file exists to provide (OMN-16156)

To add a new self-exempt file, update `SELF_EXEMPT_FILES` in the script and document the
reason here.

## Adding new literal patterns

1. Add the pattern to `LEAK_REGEX` in `check_leaked_literals.sh`.
2. Annotate or exempt all existing occurrences.
3. Run `bash scripts/validation/check_leaked_literals.sh blocking all` — must exit 0.
4. Update this doc.
