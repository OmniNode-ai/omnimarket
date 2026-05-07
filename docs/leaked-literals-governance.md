# Leaked Literals Gate — Governance

Enforced by `scripts/validation/check_leaked_literals.sh` (blocking mode).
Wired as pre-commit hook and required GHA status check (`Leaked Literals Gate`).

## What the gate blocks

Any committed file that contains:

- Private LAN IP prefixes: `192.168.86.`
- Personal filesystem paths: `/Users/jonah`, `/Volumes/PRO-G40`
- Private HuggingFace model identifiers: `cyankiwi/`, `Corianas/`, `mlx-community/`
- Personal git handles: `jonahgabriel`

## Escape hatches

### Per-line annotation (source code)

Add the annotation on the **same line** as the literal:

```python
host = os.environ.get(
    "POSTGRES_HOST",
    "192.168.86.201",  # onex-allow-internal-ip OMN-XXXXX reason="env-var fallback; override via POSTGRES_HOST"
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

To add a new self-exempt file, update `SELF_EXEMPT_FILES` in the script and document the
reason here.

## Adding new literal patterns

1. Add the pattern to `LEAK_REGEX` in `check_leaked_literals.sh`.
2. Annotate or exempt all existing occurrences.
3. Run `bash scripts/validation/check_leaked_literals.sh blocking all` — must exit 0.
4. Update this doc.
