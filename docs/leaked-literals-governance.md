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
| `# onex-allow-exposed-identifier` | Denylisted customer identifier in prose narrating the incident itself (OMN-17320 — see below) |

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
- `scripts/audit/raw_env_usage_audit.py` — the audit-only companion scanner (exit 0
  always; CSV is the deliverable, not a gate); its own regex catalog would otherwise
  self-trigger the blocking gate
- `.github/workflows/reject-leaked-literals.yml` — CI workflow referencing the gate
- `docs/leaked-literals-governance.md` — this document
- `.leaked-literals-allowlist.yaml` — the allowlist file itself, which necessarily
  lists the literals it exempts
- `docs/audits/2026-05-05-*.csv` / `docs/audits/2026-05-05-*.md` — generated audit reports
- `docs/tracking/delegation-cost-projection-lane.md` — tracking doc with config examples
- `docs/adr-canary/ground_truth_manifest.yaml` — embeds real merged ADRs' verbatim
  text as a benchmark fixture; editing embedded ADR text to annotate a literal would
  corrupt the ground-truth fidelity the file exists to provide (OMN-16156)

Two path patterns are exempted outside `SELF_EXEMPT_FILES`, matched by glob directly
in the script: `docs/audits/*-raw-env-usage.csv` (any dated raw-env-usage report, not
just the 2026-05-05 one), and `docs/evidence/*/*.generation.json` (durable
generation-evidence JSON that must record the live endpoint verbatim as proof; JSON
cannot carry a `# onex-allow-file` comment marker, so this evidence-file class is
path-exempt on the same precedent as the raw-env-usage CSVs, OMN-13294).

To add a new self-exempt file, update `SELF_EXEMPT_FILES` (or the glob checks) in the
script and document the reason here.

## OMN-17320 — the delegated exposed-identifier class

`check_leaked_literals.sh` delegates one class to
`scripts/validation/check_exposed_identifiers.py`, which uses a **different
mechanism**: a salted-SHA-256 denylist
(`scripts/validation/exposed_identifiers_denylist.json`) rather than the plaintext
regex catalog above.

### Why it is a separate mechanism

The regex catalog works because every literal in it is something we are happy to
publish: a LAN prefix, a mount name, a public HF org. A **customer identifier** is
not. Writing one into a pattern list in a PUBLIC repo would create exactly the
fresh, greppable, current-tree occurrence the class exists to prevent, and would
force the pattern file itself to be self-exempt — a special file, containing the
forbidden value, that nobody scans and that people copy from. That is the shape of
the original incident, not a fix for it.

So the denylist stores digests, a class label, and the owning ticket. Nothing in
either public repo restates a forbidden value. The plaintext lives in the owning
Linear ticket, which is private.

**The salt is committed, so this is obfuscation and not secrecy.** Anyone holding
the repo can brute-force a short slug. That is stated plainly because it is not the
property being bought. The OMN-17288 values are already public in git history and
the operator ruled document-and-accept on that history. What the digest format buys
is *forward* safety: the next entry added may be a live identifier that has **not**
leaked, and a plaintext denylist would be an active disclosure in that case. The
mechanism has to be correct for the case it is actually built for.

### Why it exists at all

OMN-17288 scrubbed a live tenant slug and registry UUID out of omnimarket (5 files)
and omnibase_infra (5 files) and established a synthetic-identifier convention.
**Three hours later**, omnimarket#2239 reintroduced the slug into two files, and the
rebase carried it onto omnimarket#2241 — the PR whose acceptance criterion was
"zero grep hits" — with every enforced gate green. The convention was documentation,
and documentation lost a race to an unrelated lane. Operating Rule #5.

### Differences from the classes above

| | regex classes | exposed-identifier |
|---|---|---|
| Storage | plaintext pattern | salted SHA-256 digest |
| Matching | ERE over the line | L-wide windows inside each identifier token |
| Finding output | prints the offending line | prints `path:line:col` + length; **never** the value |
| Per-line annotation | yes | yes |
| File-level `# onex-allow-file` | yes | **no — deliberately** |
| Self-exempt files | yes (`SELF_EXEMPT_FILES`) | **none — the gate is subject to its own rule** |

There is no file-level waiver and no self-exemption for this class. A whole-file
waiver is how a forbidden value survives in a corner nobody reads.

### When to use the annotation

Almost never. The intended case is the one the OMN-17288 finisher actually hit:
prose *narrating the incident*, where substituting a synthetic stand-in would assert
a false fact about what happened. Even there, the preferred fix is the one that was
used — **drop the literal and keep the claim**: "a real, active, externally-owned
customer was in the unmapped set" is exactly as true unnamed. Reach for the
annotation only when that genuinely does not work.

### Adding an entry

```bash
printf '%s' '<literal>' | python3 scripts/validation/gen_exposed_identifier_entry.py \
    --id omn-XXXXX-what-it-is --kind tenant-slug --ticket OMN-XXXXX \
    --notes "where it leaked and what replaced it"
```

The minter reads stdin, never argv — an argv value lands in shell history, process
listings, and any session transcript, which for a not-yet-leaked identifier would be
a fresh disclosure in three more places. It never echoes the literal. Paste the
emitted object into the `entries` array, record the plaintext in the owning Linear
ticket, and **apply the same edit to `omnibase_infra`** — the two copies are pinned
byte-identical by `test_cross_repo_fingerprint_pin` in both repos.

### Where it is enforced

| Repo | Pre-commit | CI |
|---|---|---|
| omnimarket | `leaked-literals-gate` (delegates) | `Leaked Literals Gate` — in `required_status_checks` on `dev` |
| omnibase_infra | `exposed-identifier-gate` | `Exposed Identifier Gate (OMN-17320)` in `ci.yml`, registered in `scripts/ci/ci_summary_gate.py::STRICT_GATE_JOBS` under the required `CI Summary` umbrella |

Rolling this to the other nine public repos is the G1-FULL shape OMN-16156
deferred post-beta; it is not wired there today.

## Adding new literal patterns

1. Add the pattern to `LEAK_REGEX` in `check_leaked_literals.sh`.
2. Annotate or exempt all existing occurrences.
3. Run `bash scripts/validation/check_leaked_literals.sh blocking all` — must exit 0.
4. Update this doc.
