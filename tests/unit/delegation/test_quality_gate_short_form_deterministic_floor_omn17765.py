# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17765: a declared short-form shape must not carry a test-artifact floor.

Measured on the `.201` dev lane 2026-09-04: 52 terminal FAILED correlations on
`task_type: test` were rejected for `missing @pytest.mark.unit`, and 0 of the 52
asked for test code. Every one is a liveness probe -- 49 of them the literal
string "Reply with the single word: alive. This is an automated liveness probe".

`resolve_task_class_dod_checks` already read the prompt and already replaced the
HEURISTIC band when the prompt declared a response shape (OMN-16932), but returned
the deterministic band unchanged on every path -- so a prompt saying "reply with
one word" still resolved `uses_pytest_mark_unit`, a marker the requested answer
cannot carry by construction. The gate then reported "TASK_MISMATCH: missing
@pytest.mark.unit", which is not merely strict but false: a one-word reply to a
one-word request does not mismatch its task.

The fix is a contract-declared `shape_overrides.<shape>.deterministic` sibling to
the existing `heuristic` key. Keying on the prompt is legitimate here: the prompt
is the CALLER's, and the constraint at `handler_quality_gate.py:733` forbids
selecting an override from the RESPONSE's shape, which the model controls. The
gate cannot see the prompt at all.

The five guard tests matter as much as the failing one, and each blocks a
different wrong fix:

- a genuine test ask still resolves the marker -- blocks a blanket removal
- the short-form heuristic override still resolves -- pins OMN-16932 untouched,
  and pins the fallthrough to `default_shape_overrides` for the heuristic band
- `prompt=None` keeps the class band -- pins the no-prompt caller path
- a shape directive cannot drop `code_generation`'s floor -- blocks the sibling
  becoming a general caller-controlled bypass, and goes red if the key is ever
  moved into `default_shape_overrides`
- both resolution sites agree -- the deterministic band was chosen in TWO places,
  and a fix landing only in this one would have gone green while the bus reducer
  (the path that produced the measured rows) stayed unchanged
"""

from __future__ import annotations

import textwrap
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.enums.enum_dod_band_source import EnumDodBandSource
from omnimarket.enums.enum_requested_response_shape import EnumRequestedResponseShape
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    delta,
    resolve_task_class_dod_checks,
    resolve_task_class_dod_resolution,
)

_SHORT_FORM_PROMPT = (
    "Reply with the single word: alive. This is an automated liveness probe"
)
_TEST_CODE_PROMPT = "Write a pytest unit test asserting that an empty list is falsy."

# A self-contained routable contract, so `delta()` reaches a decision in CI.
# Both `test` and `code_generation` are declared because the parity check below
# is parametrised over both -- a contract covering only one would make the other
# case unroutable and silently skip the very comparison it exists to make.
_BIFROST_ROUTABLE = textwrap.dedent(
    """\
    config_version: "2.0.0"
    schema_version: "bifrost_delegation.v1"
    backends:
      - backend_id: local-coder
        endpoint_url: "http://local.test:8000/v1/chat/completions"
        model_name: qwen-coder
        tier: local
        timeout_ms: 30000
        max_tokens: 8192
        capabilities: [code_generation, test]
    routing_rules:
      - rule_id: "c0ffee00-0011-4000-8000-000000000901"
        priority: 10
        task_class: code_generation
        task_class_contract_version: "1.0.0"
        backend_policy_version: "2.0.0"
        match_operation_types: [chat_completion]
        match_capabilities: [code_generation]
        backend_ids: [local-coder]
        fallback_policy:
          action: escalate_to_next_tier
          max_retries: 1
          on_exhaust: return_error
        shadow_policy_id: "c0ffee00-0012-4000-8000-000000000901"
      - rule_id: "c0ffee00-0011-4000-8000-000000000902"
        priority: 10
        task_class: test
        task_class_contract_version: "1.0.0"
        backend_policy_version: "2.0.0"
        match_operation_types: [chat_completion]
        match_capabilities: [test]
        backend_ids: [local-coder]
        fallback_policy:
          action: escalate_to_next_tier
          max_retries: 1
          on_exhaust: return_error
        shadow_policy_id: "c0ffee00-0012-4000-8000-000000000902"
    default_backends:
      - local-coder
    circuit_breaker:
      failure_threshold: 5
      window_seconds: 30
    failover:
      max_attempts: 3
      backoff_base_ms: 500
    shadow_mode:
      enabled: false
      policy_version: "test"
      log_sample_rate: 1.0
      comparison_logging_enabled: true
      max_shadow_latency_ms: 5.0
    """
)


@pytest.fixture
def routable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Point routing at a contract where both parametrised task types route."""
    contract_path = tmp_path / "bifrost_delegation.yaml"
    contract_path.write_text(_BIFROST_ROUTABLE)
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(contract_path))
    monkeypatch.delenv("BIFROST_OVERLAY_PATH", raising=False)
    routing._load_bifrost_endpoints.cache_clear()
    try:
        yield
    finally:
        routing._load_bifrost_endpoints.cache_clear()


@pytest.mark.unit
def test_short_form_prompt_does_not_resolve_the_pytest_marker_floor() -> None:
    """A prompt declaring a one-word answer must not demand a pytest marker."""
    deterministic, _ = resolve_task_class_dod_checks("test", prompt=_SHORT_FORM_PROMPT)
    assert "uses_pytest_mark_unit" not in deterministic


@pytest.mark.unit
def test_genuine_test_ask_still_resolves_the_pytest_marker_floor() -> None:
    """The floor is a property of a test artifact and must survive for one."""
    deterministic, _ = resolve_task_class_dod_checks("test", prompt=_TEST_CODE_PROMPT)
    assert "uses_pytest_mark_unit" in deterministic


@pytest.mark.unit
def test_short_form_prompt_still_resolves_the_heuristic_override() -> None:
    """OMN-16932's heuristic override must be unaffected by this change."""
    _, heuristic = resolve_task_class_dod_checks("test", prompt=_SHORT_FORM_PROMPT)
    assert "short_form_adequacy" in heuristic


@pytest.mark.unit
def test_prompt_none_leaves_the_class_definition_of_done_unchanged() -> None:
    """A caller with no prompt keeps the declared band, per OMN-16932."""
    deterministic, _ = resolve_task_class_dod_checks("test", prompt=None)
    assert "uses_pytest_mark_unit" in deterministic


@pytest.mark.unit
def test_a_shape_directive_cannot_drop_the_code_generation_floor() -> None:
    """The sibling narrows a declared class; it is not a general bypass.

    `code_generation` declares `compiles_without_errors` and no
    `shape_overrides`. A caller who prefixes a shape directive to a code request
    must not thereby drop that floor -- if they could, the deterministic band
    would be caller-controlled rather than contract-declared, which is the whole
    thing the per-class limit exists to prevent.

    This goes red if anyone later moves the `deterministic` key up into
    `default_shape_overrides`, which applies to every class at once.
    """
    deterministic, _ = resolve_task_class_dod_checks(
        "code_generation",
        prompt="Reply with the single word: alive. This is an automated liveness probe",
    )
    assert "compiles_without_errors" in deterministic


@pytest.mark.unit
@pytest.mark.parametrize(
    ("task_type", "prompt"),
    [
        ("test", _SHORT_FORM_PROMPT),
        ("test", _TEST_CODE_PROMPT),
        ("code_generation", _SHORT_FORM_PROMPT),
    ],
)
@pytest.mark.usefixtures("routable")
def test_both_resolution_sites_agree_on_the_same_input(
    task_type: str, prompt: str
) -> None:
    """The bus reducer and the local dispatch path must resolve identically.

    The deterministic band used to be chosen in two places: this function, whose
    only production caller is the bus-LESS local dispatch path, and the bus
    routing reducer, which re-derived it and applied the shape override to the
    heuristic band only. The reducer is the path that produced the measured
    rows, so a fix landing only here would have gone green while the lane was
    unchanged -- a false green on the ticket filed to catch false greens.

    **This calls the reducer rather than reconstructing it (OMN-17907).** The
    first version of this test composed `_definition_of_done_checks` and the two
    override helpers by hand and compared the shared resolver against that
    composition. That asserts the test's own re-implementation, not production:
    with `delta()` reverted to the pre-fix band, this file reported 13 passed
    and the whole delegation+routing suite reported 933 passed. A guard that
    cannot see the revert it names is not a guard.

    `dod_deterministic` on the decision is the value the quality gate actually
    receives, so reading it off `delta()`'s output is the property itself rather
    than a proxy for it.
    """
    shared_deterministic, shared_heuristic = resolve_task_class_dod_checks(
        task_type, prompt
    )

    decision = delta(
        ModelDelegationRequest(
            correlation_id=uuid4(),
            task_type=task_type,
            prompt=prompt,
            emitted_at=datetime.now(tz=UTC),
        )
    )

    assert shared_deterministic == decision.dod_deterministic
    assert shared_heuristic == decision.dod_heuristic

    # The bands alone would still match if both sites regressed together. The
    # provenance is what pins WHICH contract key answered, so a reducer that
    # reached the right tuple by the wrong route is still caught.
    resolution = resolve_task_class_dod_resolution(task_type, prompt)
    assert resolution.deterministic_source == decision.dod_deterministic_source
    assert resolution.heuristic_source == decision.dod_heuristic_source


@pytest.mark.unit
@pytest.mark.parametrize(
    ("task_type", "prompt", "shape", "det_source", "heur_source"),
    [
        (
            "test",
            _SHORT_FORM_PROMPT,
            EnumRequestedResponseShape.SINGLE_WORD,
            EnumDodBandSource.CLASS_SHAPE_OVERRIDES,
            EnumDodBandSource.DEFAULT_SHAPE_OVERRIDES,
        ),
        (
            "test",
            _TEST_CODE_PROMPT,
            EnumRequestedResponseShape.UNCONSTRAINED,
            EnumDodBandSource.CLASS_DEFINITION_OF_DONE,
            EnumDodBandSource.CLASS_DEFINITION_OF_DONE,
        ),
        (
            "code_generation",
            _SHORT_FORM_PROMPT,
            EnumRequestedResponseShape.SINGLE_WORD,
            EnumDodBandSource.CLASS_DEFINITION_OF_DONE,
            EnumDodBandSource.DEFAULT_SHAPE_OVERRIDES,
        ),
    ],
)
def test_the_resolution_records_which_contract_key_supplied_each_band(
    task_type: str,
    prompt: str,
    shape: EnumRequestedResponseShape,
    det_source: EnumDodBandSource,
    heur_source: EnumDodBandSource,
) -> None:
    """A band that was overridden must be distinguishable from one that was not.

    Reporting only the resulting tuple makes those two identical, which is why
    this ticket's own 28/24 split had to be inferred from which heuristic band
    appeared instead of read off a field, and why OMN-17879 has to date its rows
    against a deploy boundary. Same reasoning as the `skipped` list in
    `_evaluate_deterministic_checks` (OMN-13850): a check that was not run and
    one that ran and passed are different facts.

    The `code_generation` row is the one worth reading twice. Its deterministic
    source stays `class_definition_of_done` even though the prompt DID resolve a
    shape and DID override the heuristic band -- so "no deterministic override
    was applied here" is now a readable fact rather than an inference.
    """
    resolution = resolve_task_class_dod_resolution(task_type, prompt)
    assert resolution.requested_shape is shape
    assert resolution.deterministic_source is det_source
    assert resolution.heuristic_source is heur_source


@pytest.mark.unit
def test_prompt_none_records_unconstrained_and_no_override() -> None:
    """The no-prompt path must record that nothing was overridden, not stay blank."""
    resolution = resolve_task_class_dod_resolution("test", prompt=None)
    assert resolution.requested_shape is EnumRequestedResponseShape.UNCONSTRAINED
    assert resolution.deterministic_source is EnumDodBandSource.CLASS_DEFINITION_OF_DONE
    assert resolution.heuristic_source is EnumDodBandSource.CLASS_DEFINITION_OF_DONE


@pytest.mark.unit
def test_the_tuple_api_and_the_resolution_api_agree() -> None:
    """`resolve_task_class_dod_checks` is a wrapper and must not drift from it."""
    for task_type, prompt in (
        ("test", _SHORT_FORM_PROMPT),
        ("test", _TEST_CODE_PROMPT),
        ("code_generation", _SHORT_FORM_PROMPT),
    ):
        deterministic, heuristic = resolve_task_class_dod_checks(task_type, prompt)
        resolution = resolve_task_class_dod_resolution(task_type, prompt)
        assert deterministic == resolution.deterministic
        assert heuristic == resolution.heuristic
