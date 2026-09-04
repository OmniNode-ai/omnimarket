# OMN-11833 explicit-profile canary evidence

## Committed evidence scope

This bundle retains the successful, sanitized one-entry discovery run
`OMN-11833-local-qwen3.6-explicit-profile-20260903-c`: its hash-pinned
manifest, scorecard, extracted decisions, per-model result, and one generated
draft. That run completed extraction with three ADR candidates and did not run
the grader or publisher.

The committed historical candidate artifact predates the typed publication
provenance envelope. It intentionally has no source repository visibility or
publication classification, so the current KB publisher rejects it before any
subprocess. A future publication requires a newly generated candidate with
explicit source-owned provenance and hash-pinned, repository-relative source
documents; this record must not be retroactively inferred from a checkout or
remote URL.

The repository intentionally omits two earlier, failed local harness bundles:
the pre-profile `qwen3-coder` attempt and the intermediate structured-output
attempts. Their empty decision lists and duplicate per-model failure records
are ephemeral evidence rather than durable proof of the completed pipeline.
Their useful finding is preserved here: an earlier pre-dispatch invocation did
not reach the model, so it is not model or extraction success evidence. No raw
prompts or completions from either omitted bundle are retained.

## Historical pre-dispatch attempt

## Intended bounded run

- Run ID: `OMN-11833-local-qwen3.6-explicit-profile-20260903-a`
- Canonical workspace root: `$OMNI_HOME`
- Source path: `docs/plans/2026-04-19-omn-39-registry-resolve-impls.md`
- Source SHA-256: `f322ed9058723ecb67abd29fa001cfa1c57e92f4291cd36bbf404ee529f71d6e`
- Source size: 920 bytes, 26 lines
- Model: `Qwen3.6-35B-A3B` through the local `qwen3-coder` route
- External providers: disabled
- Transport: `EventBusInmemory`; no Kafka, grader, or KB publisher subscription
- Handler call cap: 45 seconds; JSON repair disabled

## Terminal result

The one authorized in-memory-bus invocation produced no canary report or
entry-level evidence artifact; the evidence directory contains only the
pre-run manifest. The terminal wrapper did not retain its process stderr, so
the pre-dispatch exception is not reconstructable from local output.

A read-only check of `vllm-gpu0.service` on `.201` over the bounded launch
window found zero access-log records matching a chat-completions POST. The
service continued to return health-check HTTP 200s. Therefore neither
segmentation nor extraction reached Qwen, there is no HTTP status/latency or
extracted ADR to report, and this result must not be treated as a model or
pipeline success.

No retry was performed. No raw prompt or completion was persisted.
