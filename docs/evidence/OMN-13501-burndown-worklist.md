# OMN-13501 — omnimarket no-faked-boundary burndown worklist

Detector: `env -u PYTHONPATH uv run python -m omnibase_core.validation.no_faked_boundary.runtime_no_faked_boundary .`
(same handler the `.pre-commit-config.yaml` `check-no-faked-boundary` hook calls with `--report-only`;
`--report-only` only changes the exit code / trailer message, not the finding set, so running without it
surfaces the identical 69 findings.)

Run date: 2026-07-03. Worktree: `jonah/omn-13501-boundary-burndown` @ `origin/dev` (125aa6fa).

## Discovery correction (read before executing)

The dispatch brief assumed ~50 total findings (35 patch_httpx_egress / 10 mock_assigned_to_boundary /
5 class_subclassing_boundary) and stated the detector's own acceptance-corpus file already carries a
file-level exemption. **Live output is 69 findings, and no file in omnimarket currently carries
`onex-allow-file-faked-boundary`.** The extra 19 findings are two files whose SUBJECT is
literally fake-boundary source strings used as fixtures for an LLM-generated-validator acceptance
corpus (`node_generation_consumer`'s own corpus, not omnibase_core's):

- `src/omnimarket/nodes/node_generation_consumer/validator_corpora/corpus_no_faked_boundary.py` (13 findings)
- `scripts/generation/drive_validator_generation.py` (6 findings)

These are genuine false-positives of the same "subject IS the pattern" shape the
`onex-allow-file-faked-boundary` marker exists for (see
`omnibase_core/validation/no_faked_boundary/handler.py` docstring, which self-exempts using that exact
marker). They need the marker added — a mechanical one-line-per-file fix, folded into Cluster 4 below.
50 (real product/test fixes) + 19 (missing file-level marker) = 69 total live findings.

## Findings (69 rows)

| # | File:Line | Rule | Cluster | Planned disposition |
|---|-----------|------|---------|----------------------|
| 1 | tests/unit/delegation/test_handler_inference_intent.py:120 | patch_httpx_egress | C1 | DONE — per-line `# onex-allow-faked-boundary` (reason above line): effect-handler unit test asserts request construction (verbatim URL / auth header / payload) at the egress; `transport.calls` records only model/url/request_hash |
| 2 | tests/unit/delegation/test_handler_inference_intent.py:155 | patch_httpx_egress | C1 | DONE — per-line `# onex-allow-faked-boundary` (reason above line): effect-handler unit test asserts request construction (verbatim URL / auth header / payload) at the egress; `transport.calls` records only model/url/request_hash |
| 3 | tests/unit/delegation/test_handler_inference_intent.py:171 | patch_httpx_egress | C1 | DONE — per-line `# onex-allow-faked-boundary`: effect-handler unit test injects a transport exception (ConnectionRefused/Timeout); `RecordedReplayInferenceTransport` has no exception-injection hook |
| 4 | tests/unit/delegation/test_handler_inference_intent.py:265 | patch_httpx_egress | C1 | DONE — per-line `# onex-allow-faked-boundary` (reason above line): effect-handler unit test asserts request construction (verbatim URL / auth header / payload) at the egress; `transport.calls` records only model/url/request_hash |
| 5 | tests/unit/delegation/test_handler_inference_intent.py:311 | patch_httpx_egress | C1 | DONE — per-line `# onex-allow-faked-boundary`: effect-handler unit test crafts an edge response (empty content / finish_reason=length / HTTP 4xx); `load_fixture` rejects empty completions and `ReplayResponse.raise_for_status` is a no-op |
| 6 | tests/unit/delegation/test_handler_inference_intent.py:361 | patch_httpx_egress | C1 | DONE — per-line `# onex-allow-faked-boundary`: effect-handler unit test injects a transport exception (ConnectionRefused/Timeout); `RecordedReplayInferenceTransport` has no exception-injection hook |
| 7 | tests/unit/delegation/test_handler_inference_intent.py:429 | patch_httpx_egress | C1 | DONE — per-line `# onex-allow-faked-boundary`: effect-handler unit test crafts an edge response (empty content / finish_reason=length / HTTP 4xx); `load_fixture` rejects empty completions and `ReplayResponse.raise_for_status` is a no-op |
| 8 | tests/unit/delegation/test_handler_inference_intent.py:468 | patch_httpx_egress | C1 | DONE — per-line `# onex-allow-faked-boundary`: effect-handler unit test crafts an edge response (empty content / finish_reason=length / HTTP 4xx); `load_fixture` rejects empty completions and `ReplayResponse.raise_for_status` is a no-op |
| 9 | tests/unit/delegation/test_handler_inference_intent.py:498 | patch_httpx_egress | C1 | DONE — per-line `# onex-allow-faked-boundary` (reason above line): effect-handler unit test asserts request construction (verbatim URL / auth header / payload) at the egress; `transport.calls` records only model/url/request_hash |
| 10 | tests/unit/delegation/test_handler_inference_intent.py:520 | patch_httpx_egress | C1 | DONE — per-line `# onex-allow-faked-boundary` (reason above line): effect-handler unit test asserts request construction (verbatim URL / auth header / payload) at the egress; `transport.calls` records only model/url/request_hash |
| 11 | tests/unit/delegation/test_handler_inference_intent.py:573 | patch_httpx_egress | C1 | DONE — per-line `# onex-allow-faked-boundary` (reason above line): effect-handler unit test asserts request construction (verbatim URL / auth header / payload) at the egress; `transport.calls` records only model/url/request_hash |
| 12 | tests/unit/delegation/test_handler_inference_intent.py:614 | patch_httpx_egress | C1 | DONE — per-line `# onex-allow-faked-boundary` (reason above line): effect-handler unit test asserts request construction (verbatim URL / auth header / payload) at the egress; `transport.calls` records only model/url/request_hash |
| 13 | tests/unit/delegation/test_handler_inference_intent.py:644 | patch_httpx_egress | C1 | DONE — per-line `# onex-allow-faked-boundary` (reason above line): effect-handler unit test asserts request construction (verbatim URL / auth header / payload) at the egress; `transport.calls` records only model/url/request_hash |
| 14 | tests/unit/delegation/test_handler_inference_intent.py:689 | patch_httpx_egress | C1 | DONE — per-line `# onex-allow-faked-boundary` (reason above line): effect-handler unit test asserts request construction (verbatim URL / auth header / payload) at the egress; `transport.calls` records only model/url/request_hash |
| 15 | tests/unit/delegation/test_delegation_chain_e2e.py:219 | patch_httpx_egress | C1 | DONE — per-line `# onex-allow-faked-boundary`: synthetic completion is a TEST INPUT driving downstream quality-gate / refusal / escalation / veto logic; not recordable-from-real, transport forbids echo/empty |
| 16 | tests/unit/delegation/test_delegation_chain_e2e.py:336 | patch_httpx_egress | C1 | DONE — per-line `# onex-allow-faked-boundary`: effect-handler unit test injects a transport exception (ConnectionRefused/Timeout); `RecordedReplayInferenceTransport` has no exception-injection hook |
| 17 | tests/unit/delegation/test_cloud_escalation_omn13140.py:570 | patch_httpx_egress | C1 | DONE — per-line `# onex-allow-faked-boundary`: synthetic completion is a TEST INPUT driving downstream quality-gate / refusal / escalation / veto logic; not recordable-from-real, transport forbids echo/empty |
| 18 | tests/unit/delegation/test_judge_verdict_vetoes_acceptance_omn13642.py:393 | patch_httpx_egress | C1 | DONE — per-line `# onex-allow-faked-boundary`: synthetic completion is a TEST INPUT driving downstream quality-gate / refusal / escalation / veto logic; not recordable-from-real, transport forbids echo/empty |
| 19 | tests/unit/delegation/test_refusal_not_pass_codegen_research_omn13479.py:470 | patch_httpx_egress | C1 | DONE — per-line `# onex-allow-faked-boundary`: synthetic completion is a TEST INPUT driving downstream quality-gate / refusal / escalation / veto logic; not recordable-from-real, transport forbids echo/empty |
| 20 | tests/unit/delegation/test_refusal_not_pass_codegen_research_omn13479.py:596 | patch_httpx_egress | C1 | DONE — per-line `# onex-allow-faked-boundary`: synthetic completion is a TEST INPUT driving downstream quality-gate / refusal / escalation / veto logic; not recordable-from-real, transport forbids echo/empty |
| 21 | tests/unit/delegation/test_refusal_not_pass_omn13409.py:331 | patch_httpx_egress | C1 | DONE — per-line `# onex-allow-faked-boundary`: synthetic completion is a TEST INPUT driving downstream quality-gate / refusal / escalation / veto logic; not recordable-from-real, transport forbids echo/empty |
| 22 | tests/unit/delegation/test_served_usage_upstream_omn13408.py:105 | patch_httpx_egress | C1 | DONE — per-line `# onex-allow-faked-boundary`: effect-handler unit test crafts an edge response (empty content / finish_reason=length / HTTP 4xx); `load_fixture` rejects empty completions and `ReplayResponse.raise_for_status` is a no-op |
| 23 | tests/unit/delegation/test_served_usage_upstream_omn13408.py:184 | patch_httpx_egress | C1 | DONE — per-line `# onex-allow-faked-boundary`: effect-handler unit test crafts an edge response (empty content / finish_reason=length / HTTP 4xx); `load_fixture` rejects empty completions and `ReplayResponse.raise_for_status` is a no-op |
| 24 | tests/test_model_router_authorization_and_ttl.py:93 | patch_httpx_egress | C1 | DONE — per-line `# onex-allow-faked-boundary`: non-inference egress — model-router `/health` GET probe (health-liveness boundary, not inference) |
| 25 | tests/integration/golden_chain/test_sea_acceptance_chain.py:350 | patch_httpx_egress | C1 | DONE — per-line `# onex-allow-faked-boundary`: SEA minimum-proof diagnostic asserts verbatim endpoint_url (OMN-12815) + exact served tokens vs a synthetic test-hostname response |
| 26 | tests/integration/golden_chain/test_golden_chain_delegation_useful_artifact_chain.py:7 | patch_httpx_egress | C1 | DONE — docstring reworded to drop the literal `patch("httpx.Client")` string; already-migrated reference file, no code change |
| 27 | tests/unit/nodes/node_baseline_capture/test_probe_unit.py:121 | patch_httpx_egress | C2 | justified-allow-non-inference |
| 28 | tests/unit/nodes/node_baseline_capture/test_probe_unit.py:154 | patch_httpx_egress | C2 | justified-allow-non-inference |
| 29 | tests/unit/nodes/node_baseline_capture/test_probe_unit.py:188 | patch_httpx_egress | C2 | justified-allow-non-inference |
| 30 | tests/unit/nodes/node_baseline_capture/test_probe_unit.py:263 | patch_httpx_egress | C2 | justified-allow-non-inference |
| 31 | tests/unit/nodes/node_baseline_capture/test_probe_unit.py:301 | patch_httpx_egress | C2 | justified-allow-non-inference |
| 32 | tests/unit/nodes/node_baseline_capture/test_probe_unit.py:320 | patch_httpx_egress | C2 | justified-allow-non-inference |
| 33 | tests/unit/nodes/node_baseline_capture/test_probe_unit.py:522 | patch_httpx_egress | C2 | justified-allow-non-inference |
| 34 | tests/unit/nodes/node_baseline_capture/test_probe_unit.py:557 | patch_httpx_egress | C2 | justified-allow-non-inference |
| 35 | tests/unit/nodes/node_baseline_capture/test_probe_unit.py:599 | patch_httpx_egress | C2 | justified-allow-non-inference |
| 36 | tests/unit/nodes/node_dispatch_queue_drainer/test_handler_dispatch_queue_drainer.py:165 | mock_assigned_to_boundary | C3 | contract-level-fake (`worker: ProtocolDispatchWorker = MagicMock(spec=ProtocolDispatchWorker)` — build a typed fake implementing `ProtocolDispatchWorker`, not a spec'd MagicMock) |
| 37 | tests/nodes/node_thread_reply_effect/test_handler_thread_reply.py:396 | mock_assigned_to_boundary | C3 | real-model-object / contract-level-fake for `mock_router` |
| 38 | tests/nodes/node_thread_reply_effect/test_handler_thread_reply.py:442 | mock_assigned_to_boundary | C3 | real-model-object / contract-level-fake for `mock_router` |
| 39 | tests/test_handler_skill_overseer_verify.py:60 | mock_assigned_to_boundary | C3 | contract-level-fake (`dispatcher` is the injectable `task_dispatcher` callable on `HandlerSkillRequested` — agent-dispatch, not the platform inference/routing boundary; replace bare `AsyncMock` with a small typed callable fake, or add a scoped `# onex-allow-faked-boundary` with a "not the platform inference boundary" justification if the protocol has no natural typed fake) |
| 40 | tests/test_handler_skill_overseer_verify.py:71 | mock_assigned_to_boundary | C3 | contract-level-fake (same `dispatcher` pattern) |
| 41 | tests/test_handler_skill_overseer_verify.py:83 | mock_assigned_to_boundary | C3 | contract-level-fake (same `dispatcher` pattern) |
| 42 | tests/unit/delegation/test_delegation_plugin_runtime_profile.py:79 | mock_assigned_to_boundary | C3 | real-model-object (`config.dispatch_engine = MagicMock()`) |
| 43 | tests/unit/delegation/test_delegation_wiring.py:135 | mock_assigned_to_boundary | C3 | real-model-object (`engine.register_dispatcher = MagicMock()`) |
| 44 | tests/unit/experiments/adk_eval/type_debt_scout_poc/test_handler_type_debt_scout.py:154 | mock_assigned_to_boundary | C3 | real-model-object / contract-level-fake for `fake_router` |
| 45 | tests/unit/experiments/adk_eval/type_debt_scout_poc/test_handler_type_debt_scout.py:184 | mock_assigned_to_boundary | C3 | real-model-object / contract-level-fake for `fake_router` |
| 46 | src/omnimarket/nodes/node_adr_decision_extraction_llm_effect/tests/test_handler_decision_extraction.py:140 | class_subclassing_boundary | C4 | contract-level-fake (`class _MockBridge(ModelInferenceAdapter)` -> compose a real/contract-level fake instead of subclassing) |
| 47 | src/omnimarket/nodes/node_adr_extraction_grader_llm_effect/tests/test_handler_extraction_grader.py:59 | class_subclassing_boundary | C4 | contract-level-fake |
| 48 | src/omnimarket/nodes/node_adr_segmentation_llm_effect/tests/test_handler_segmentation.py:110 | class_subclassing_boundary | C4 | contract-level-fake |
| 49 | src/omnimarket/nodes/node_pr_semantic_grader_llm_effect/tests/test_handler_pr_semantic_grader.py:87 | class_subclassing_boundary | C4 | contract-level-fake |
| 50 | tests/integration/_review_verify_mocks.py:55 | class_subclassing_boundary | C4 | contract-level-fake (`class _MockInferenceAdapter(ModelInferenceAdapter)`) |
| 51 | src/omnimarket/nodes/node_generation_consumer/validator_corpora/corpus_no_faked_boundary.py:28 | class_subclassing_boundary | C4 | justified-file-level-allow — add `onex-allow-file-faked-boundary` (corpus fixture text, subject IS the pattern) |
| 52 | src/omnimarket/nodes/node_generation_consumer/validator_corpora/corpus_no_faked_boundary.py:29 | patch_httpx_egress | C4 | justified-file-level-allow |
| 53 | src/omnimarket/nodes/node_generation_consumer/validator_corpora/corpus_no_faked_boundary.py:33 | mock_assigned_to_boundary | C4 | justified-file-level-allow |
| 54 | src/omnimarket/nodes/node_generation_consumer/validator_corpora/corpus_no_faked_boundary.py:34 | completion_echoes_prompt_var | C4 | justified-file-level-allow |
| 55 | src/omnimarket/nodes/node_generation_consumer/validator_corpora/corpus_no_faked_boundary.py:35 | completion_fstring_interpolates_prompt | C4 | justified-file-level-allow |
| 56 | src/omnimarket/nodes/node_generation_consumer/validator_corpora/corpus_no_faked_boundary.py:72 | class_subclassing_boundary | C4 | justified-file-level-allow |
| 57 | src/omnimarket/nodes/node_generation_consumer/validator_corpora/corpus_no_faked_boundary.py:81 | patch_httpx_egress | C4 | justified-file-level-allow |
| 58 | src/omnimarket/nodes/node_generation_consumer/validator_corpora/corpus_no_faked_boundary.py:90 | completion_echoes_prompt_var | C4 | justified-file-level-allow |
| 59 | src/omnimarket/nodes/node_generation_consumer/validator_corpora/corpus_no_faked_boundary.py:99 | mock_assigned_to_boundary | C4 | justified-file-level-allow |
| 60 | src/omnimarket/nodes/node_generation_consumer/validator_corpora/corpus_no_faked_boundary.py:108 | class_subclassing_boundary | C4 | justified-file-level-allow |
| 61 | src/omnimarket/nodes/node_generation_consumer/validator_corpora/corpus_no_faked_boundary.py:118 | patch_httpx_egress | C4 | justified-file-level-allow |
| 62 | src/omnimarket/nodes/node_generation_consumer/validator_corpora/corpus_no_faked_boundary.py:125 | completion_fstring_interpolates_prompt | C4 | justified-file-level-allow |
| 63 | src/omnimarket/nodes/node_generation_consumer/validator_corpora/corpus_no_faked_boundary.py:135 | mock_assigned_to_boundary | C4 | justified-file-level-allow |
| 64 | scripts/generation/drive_validator_generation.py:123 | class_subclassing_boundary | C4 | justified-file-level-allow (LLM prompt text, same shape) |
| 65 | scripts/generation/drive_validator_generation.py:124 | class_subclassing_boundary | C4 | justified-file-level-allow |
| 66 | scripts/generation/drive_validator_generation.py:138 | mock_assigned_to_boundary | C4 | justified-file-level-allow |
| 67 | scripts/generation/drive_validator_generation.py:139 | mock_assigned_to_boundary | C4 | justified-file-level-allow |
| 68 | scripts/generation/drive_validator_generation.py:141 | completion_echoes_prompt_var | C4 | justified-file-level-allow |
| 69 | scripts/generation/drive_validator_generation.py:142 | completion_fstring_interpolates_prompt | C4 | justified-file-level-allow |

## Cluster summary

- **C1 — inference `patch_httpx_egress` — RESOLVED via scoped per-line `# onex-allow-faked-boundary`,
  NOT recorded-replay (26 findings, 10 files). CLOSED 2026-07-03.**

  **Planned disposition was recorded-replay-fixture; actual disposition is a scoped per-line annotation
  with a concrete reason above each site.** The plan assumed these 26 sites were the "same seam repeated,
  mechanical after the first." Reading each test disproved that. The canonical
  `RecordedReplayInferenceTransport` (OMN-13499) is `httpx.Client`-shaped but structurally fits exactly ONE
  test shape: a happy-path chain that asserts a useful artifact from REAL recorded model bytes and that
  routing resolved the concrete model. That shape is already covered by the in-repo reference chain
  `tests/integration/golden_chain/test_golden_chain_delegation_useful_artifact_chain.py` (PR #1351). NONE
  of the 26 C1 sites in this cluster are that shape. They split into:

  - **Effect-handler unit tests** of `HandlerInferenceIntent` (whose job IS the HTTP egress) that inject a
    transport exception (ConnectionRefused/Timeout), craft an edge provider response (empty content /
    `finish_reason=length` truncation / HTTP 4xx), or capture the constructed request (verbatim URL / auth
    header / payload messages). The transport **cannot serve any of these**: it has no exception-injection
    hook; `load_fixture` fails closed on empty completions (`EMPTY_COMPLETION`); `ReplayResponse.raise_for_status`
    is a no-op; and `transport.calls` records only `model/url/request_hash` — not headers or full payload.
  - **Chain tests** that inject a **synthetic** model completion as a TEST INPUT to drive downstream
    quality-gate / refusal / escalation / veto logic (refusal strings, weak output, truncated-with-usage).
    A recorded-from-real fixture cannot produce these adversarial outputs on demand, and the transport
    forbids echo/empty completions.

  Force-fitting these to recorded-replay would require **fabricating provenance-stamped fixtures containing
  synthetic content** — a worse doctrine violation than the finding (recorded-replay demands
  *recorded-from-real* bytes), and would DELETE load-bearing error-path / edge-case / adversarial-input
  coverage (forbidden test-weakening). The honest, detector-sanctioned fix is the per-line
  `# onex-allow-faked-boundary` marker (the escape hatch the detector docstring blesses for
  "genuinely-approved boundary doubles"), each carrying a concrete category-specific reason on the lines
  directly above and stating that the integrated inference path itself is proven by the recorded-replay
  golden chain. Row 24 (`test_model_router_authorization_and_ttl.py:93`) is non-inference `/health`-probe
  egress (C2-style). Row 26 (`test_golden_chain_delegation_useful_artifact_chain.py:7`) was a docstring
  false-positive on the already-migrated reference file — reworded to drop the literal `patch("httpx.Client")`
  string (no code change). Annotations are format-stable under `ruff format` (short bare marker inline +
  reason comment above; verified idempotent). Detector delta after C1: 69 -> 43 (26 cleared).

- **C2 — `node_baseline_capture` health-probe egress -> justified-allow-non-inference (9 findings, 1 file).**
  `test_probe_unit.py` patches `httpx.AsyncClient` around container/service health probes — non-inference
  egress. Add per-line `# onex-allow-faked-boundary` with a concrete "health probe, not inference
  boundary" reason on each of the 9 sites. Verify each site first: confirm the probed endpoint is a
  liveness/health check, not a model completion call, before annotating.

- **C3 — `mock_assigned_to_boundary` -> real-model-object / contract-level-fake (10 findings, 7 files).**
  Replace bare `MagicMock`/`AsyncMock` assigned onto typed boundary attributes (`worker`, `mock_router`,
  `dispatcher`, `dispatch_engine`, `register_dispatcher`, `fake_router`) with real model objects or
  contract-level fakes that implement the relevant Protocol. `test_handler_dispatch_queue_drainer.py:165`
  already declares the typed protocol (`ProtocolDispatchWorker`) — build a small typed fake class instead
  of `MagicMock(spec=...)`. The three `test_handler_skill_overseer_verify.py` sites are agent-dispatch
  (`task_dispatcher` callable), not the platform's own inference/routing boundary — verify against the
  contract before choosing real-object vs. justified-allow.

- **C4 — `class_subclassing_boundary` (5) + missing file-level exemption cleanup (19) -> contract-level-fake
  / justified-file-level-allow.** Five real test files subclass `ModelInferenceAdapter` directly
  (`_MockBridge`, `_MockInferenceAdapter`) — restructure each to compose a real boundary object or a
  contract-level fake instead of subclassing. Separately, add the `onex-allow-file-faked-boundary`
  marker (per `omnibase_core.validation.no_faked_boundary.handler` docstring convention) to
  `src/omnimarket/nodes/node_generation_consumer/validator_corpora/corpus_no_faked_boundary.py` and
  `scripts/generation/drive_validator_generation.py` — both are fixture/prompt-text corpora whose SUBJECT
  is the fake-boundary pattern, not a real fake; this is the same self-exemption
  `omnibase_core`'s own detector docstring already uses. This second half of C4 is mechanical (one marker
  line per file) and can be split off first if the class-subclassing half needs more review time.

## Detector re-run command

```bash
cd /Users/jonah/Code/omni_worktrees/OMN-13501/omnimarket
env -u PYTHONPATH uv run python -m omnibase_core.validation.no_faked_boundary.runtime_no_faked_boundary .
```

(Add `--report-only` to match the exact pre-commit invocation; `--report-only` does not change the
finding set, only exit code and trailer text — verified by reading
`omnibase_core/validation/no_faked_boundary/runtime_no_faked_boundary.py::_run`.)
