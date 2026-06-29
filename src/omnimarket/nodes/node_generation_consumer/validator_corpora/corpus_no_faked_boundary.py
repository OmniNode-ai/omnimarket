# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Acceptance corpus for the no-faked-boundary mechanical scanner (OMN-13497).

Ground truth invariant: a hand-written class/object standing in for one of the
platform's OWN inference / routing / dispatch boundaries — the inference bridge
(``ModelInferenceAdapter`` / ``inference_bridge``), the model router, routing-
contract resolution, the registry, or the dispatch surface — that returns
canned / prompt-echoed output or accepts ANY ``model_key`` is a FAKE of our
architecture and must never enter the codebase. This is especially corrosive
inside tests that CLAIM real-dispatch / golden-chain / real-bus coverage:
``feedback_real_dispatch_path_tests`` ("handler-isolation tests pass while live
fails") is exactly the failure this gate bans. The aislop / compliance sweeps
detect adjacent smells advisorily; this corpus turns the fake-boundary invariant
into the acceptance authority for a generated COMPUTE validator that BLOCKS.

The corpus is the acceptance authority — NOT the LLM. A generated scanner is
accepted iff it flags every ``violation_fixtures`` entry (>=1 finding) and
produces zero findings on every ``clean_fixtures`` entry, by deterministic
execution in the hardened sandbox.

The mechanically-decidable boundary the scanner must hold (line-oriented text):

FLAG (a fake of OUR inference/routing/dispatch boundary):
  * a ``Fake`` / ``Stub`` / ``Mock`` class that SUBCLASSES one of the platform's
    own boundary protocols — ``*InferenceAdapter`` / ``*Bridge`` / ``*Router`` /
    ``*RoutingResolver`` / ``*Dispatch*`` — e.g.
    ``class _FakeBridge(ModelInferenceAdapter):``;
  * ``patch("httpx.Client")`` / ``patch("httpx.AsyncClient")`` (or the
    ``mock.patch`` / ``@patch`` forms) — substituting the HTTP egress that backs
    real inference inside a dispatch test;
  * a ``MagicMock`` / ``AsyncMock`` assigned to an inference / router / dispatch
    attribute (``inference_bridge=MagicMock()``, ``router = AsyncMock()``);
  * an echo / prompt-derived fixture completion: ``completion=prompt`` or
    ``completion=f"[recorded] {prompt}"`` — a completion DERIVED from the prompt
    rather than RECORDED from a real call.

CLEAN (do NOT flag):
  * a fixture adapter whose ``completion`` is a RECORDED-FROM-REAL literal string
    (golden-chain replay) AND that hard-rejects a tier name as ``model_key``;
  * a real adapter usage such as ``RoutingResolvedJudgeInferenceAdapter(...)``;
  * a real-bus ``EventBusInmemory`` test that performs real routing resolution;
  * a legitimate mock / patch of a genuinely-EXTERNAL third-party service
    (Slack, S3, GitHub, Stripe) that is NOT the platform's own inference /
    routing / dispatch surface;
  * a ``Mock`` class subclassing a NON-boundary base (e.g. a service-registry
    mixin) — a test harness, not a fake of the inference boundary.

Mutation cases (``mutation_of``) perturb a base fixture — a renamed fake
(``_FakeBridge`` -> ``_StubInferenceRouter``), the async patch variant, and the
f-string echo form — so the gate generalises the fake-boundary invariant rather
than memorising a curated set (OMN-13289 DoD).
"""

from __future__ import annotations

from omnimarket.nodes.node_generation_consumer.models.model_generation import (
    ModelCorpusFixture,
    ModelValidatorCorpus,
)

__all__ = ["NO_FAKED_BOUNDARY_CORPUS"]


NO_FAKED_BOUNDARY_CORPUS = ModelValidatorCorpus(
    source_field="source",
    findings_keys=("findings", "violations", "errors", "matches"),
    violation_fixtures=[
        # --- base case: a hand-written Fake of the inference bridge ---
        ModelCorpusFixture(
            fixture_id="v-base-fake-bridge",
            source="class _FakeBridge(ModelInferenceAdapter):",
            description=(
                "a Fake class subclassing the inference-bridge protocol "
                "(ModelInferenceAdapter) — a fake of our own boundary, must flag"
            ),
        ),
        # --- base case: patching the HTTP egress that backs real inference ---
        ModelCorpusFixture(
            fixture_id="v-base-patch-httpx",
            source='    with patch("httpx.Client") as mock_client:',
            description=(
                "patching httpx.Client around an inference call in a dispatch "
                "test — substitutes the real egress, must flag"
            ),
        ),
        # --- base case: echo completion derived from the prompt ---
        ModelCorpusFixture(
            fixture_id="v-base-echo-completion",
            source="    adapter = RecordedFixtureInferenceAdapter(completion=prompt)",
            description=(
                "fixture adapter whose completion IS the prompt (echo, not "
                "recorded-from-real) — must flag"
            ),
        ),
        # --- base case: MagicMock substituting the dispatch/router surface ---
        ModelCorpusFixture(
            fixture_id="v-base-magicmock-router",
            source="    inference_bridge = MagicMock()",
            description=(
                "a MagicMock assigned to the inference bridge — fakes the real "
                "resolution/dispatch surface, must flag"
            ),
        ),
        # --- adversarial mutation: renamed Stub of the router boundary ---
        ModelCorpusFixture(
            fixture_id="v-mut-stub-router",
            source="class _StubInferenceRouter(ModelInferenceAdapter):",
            description=(
                "renamed Stub still subclassing the inference boundary — must "
                "still flag"
            ),
            mutation_of="v-base-fake-bridge",
        ),
        # --- adversarial mutation: the async-client patch variant ---
        ModelCorpusFixture(
            fixture_id="v-mut-patch-async",
            source='    @patch("httpx.AsyncClient")',
            description=("decorator-form patch of httpx.AsyncClient — must still flag"),
            mutation_of="v-base-patch-httpx",
        ),
        # --- adversarial mutation: f-string echo completion ---
        ModelCorpusFixture(
            fixture_id="v-mut-echo-fstring",
            source='    adapter = RecordedFixtureInferenceAdapter(completion=f"[recorded] {prompt}")',
            description=(
                "completion is an f-string DERIVED from the prompt (not a "
                "recorded literal) — must still flag"
            ),
            mutation_of="v-base-echo-completion",
        ),
        # --- adversarial mutation: AsyncMock on a router attribute ---
        ModelCorpusFixture(
            fixture_id="v-mut-asyncmock-router",
            source="    self.router = AsyncMock()",
            description=(
                "AsyncMock assigned to a router attribute — fakes our dispatch "
                "boundary, must still flag"
            ),
            mutation_of="v-base-magicmock-router",
        ),
    ],
    clean_fixtures=[
        # --- recorded-from-real replay adapter that hard-rejects a tier name ---
        ModelCorpusFixture(
            fixture_id="c-base-recorded-replay",
            source='    adapter = RecordedFixtureInferenceAdapter(completion="The capital of France is Paris.")',
            description=(
                "completion is a RECORDED-FROM-REAL literal string (golden-chain "
                "replay), not a prompt echo — must stay clean"
            ),
        ),
        # --- a real adapter usage ---
        ModelCorpusFixture(
            fixture_id="c-base-real-adapter",
            source="    adapter = RoutingResolvedJudgeInferenceAdapter(routing=resolved)",
            description=(
                "a real routing-resolved inference adapter — the genuine "
                "boundary, must stay clean"
            ),
        ),
        # --- a real-bus EventBusInmemory real-resolution test ---
        ModelCorpusFixture(
            fixture_id="c-base-real-bus",
            source="    bus = EventBusInmemory()",
            description=(
                "real in-memory event bus with real routing resolution — not a "
                "fake of the boundary, must stay clean"
            ),
        ),
        # --- adversarial clean mutation: legit mock of an EXTERNAL service ---
        ModelCorpusFixture(
            fixture_id="c-mut-external-mock",
            source='    with patch("slack_sdk.WebClient") as mock_slack:',
            description=(
                "mocking a genuinely-external third-party SDK (Slack) — NOT the "
                "platform's own inference/dispatch surface, must stay clean"
            ),
            mutation_of="c-base-real-bus",
        ),
        # --- adversarial clean mutation: external S3 client patch ---
        ModelCorpusFixture(
            fixture_id="c-mut-external-s3",
            source='    @patch("boto3.client")',
            description=(
                "patching boto3 (external object storage egress) — a legitimate "
                "external mock, not our resolution/dispatch boundary, must stay "
                "clean"
            ),
            mutation_of="c-base-real-bus",
        ),
        # --- adversarial clean mutation: a Mock class subclassing a NON-boundary
        # base. A test harness that subclasses a service-registry mixin (or any
        # base that is NOT one of the inference/routing/dispatch boundary
        # protocols) is NOT a fake of our inference boundary — it does not return
        # canned LLM completions or accept any model_key. Must stay clean. (This
        # pins the real FP found in the OMN-13497 shadow run:
        # `class MockServiceHub(MixinServiceRegistry):`.) ---
        ModelCorpusFixture(
            fixture_id="c-mut-mock-nonboundary-base",
            source="class MockServiceHub(MixinServiceRegistry):",
            description=(
                "a Mock class subclassing a service-registry MIXIN (not an "
                "inference/routing/dispatch boundary protocol) — a test harness, "
                "not a fake of the inference boundary, must stay clean"
            ),
            mutation_of="c-base-real-bus",
        ),
        # --- adversarial clean mutation: a recorded completion that mentions the
        # word 'prompt' as ordinary recorded prose (NOT echoing the prompt var) ---
        ModelCorpusFixture(
            fixture_id="c-mut-recorded-mentions-prompt",
            source='    adapter = RecordedFixtureInferenceAdapter(completion="Answer the prompt carefully.")',
            description=(
                "a recorded literal completion whose TEXT happens to contain the "
                "word 'prompt' — it is a string literal, not the prompt variable "
                "echoed, so must stay clean"
            ),
            mutation_of="c-base-recorded-replay",
        ),
    ],
)
